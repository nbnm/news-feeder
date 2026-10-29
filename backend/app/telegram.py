import asyncio
import hashlib
from datetime import timedelta
from uuid import uuid4

import httpx
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from .config import Settings
from .digests import build_digest, preferences
from .models import Article, Delivery, Digest, TelegramDelivery, WorkerState, aware, now
from .relevance import prioritized_feed
from .telegram_format import FormattedMessage
from .telegram_format import format_articles as format_articles


def split_messages(text: str, limit: int = 3800) -> list[str]:
    # Conservative UTF-16 accounting also keeps emoji-heavy text within Telegram limits.
    messages, current, size = [], [], 0
    for character in text:
        width = len(character.encode("utf-16-le")) // 2
        if size + width > limit:
            messages.append("".join(current))
            current, size = [], 0
        current.append(character)
        size += width
    if current:
        messages.append("".join(current))
    return messages or ["No new stories in this period."]


async def deliver(
    session,
    settings: Settings,
    messages: list[str | FormattedMessage],
    dedupe_key: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> Delivery:
    if not settings.telegram_ready:
        raise ValueError(
            "Configure TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID; delivery is disabled in demo mode"
        )
    key = dedupe_key or "manual:" + str(uuid4())
    delivery = session.scalar(select(Delivery).where(Delivery.dedupe_key == key))
    if delivery and delivery.state in {"sent", "uncertain", "sending"}:
        return delivery
    if delivery is None:
        delivery = Delivery(dedupe_key=key, total_count=len(messages), messages=messages)
        session.add(delivery)
        try:
            session.flush()
            session.add(TelegramDelivery(delivery_id=delivery.id, target=settings.telegram_chat_id))
            session.commit()
        except IntegrityError:
            session.rollback()
            # Another worker claimed this daily key. Let that worker finish;
            # never race it to publish the same digest.
            return session.scalar(select(Delivery).where(Delivery.dedupe_key == key))
    record = session.get(TelegramDelivery, delivery.id)
    if record is None:
        record = TelegramDelivery(delivery_id=delivery.id, target=settings.telegram_chat_id)
        session.add(record)
        session.commit()
    if record.target != settings.telegram_chat_id:
        raise ValueError("This delivery belongs to a different Telegram destination")
    if record.retry_at and aware(record.retry_at) > now():
        return delivery
    if client is None:
        async with httpx.AsyncClient(timeout=20) as owned_client:
            return await _send(session, settings, delivery, owned_client)
    return await _send(session, settings, delivery, client)


async def _send(session, settings, delivery, client):
    record = session.get(TelegramDelivery, delivery.id)
    for message in delivery.messages[delivery.sent_count :]:
        # Persist before sending. A restart in this state is ambiguous and must not blindly resend.
        claimed = session.execute(
            update(Delivery)
            .where(
                Delivery.id == delivery.id,
                Delivery.state.in_(["pending", "failed"]),
                Delivery.sent_count == delivery.sent_count,
            )
            .values(state="sending", error=None)
        )
        if claimed.rowcount == 1:
            record.attempted_at = now()
        session.commit()
        if claimed.rowcount != 1:
            session.refresh(delivery)
            return delivery
        if delivery.sent_count:
            await asyncio.sleep(1.05)
        # Old persisted deliveries contain strings. New messages retain explicit
        # entities alongside their text, so retries preserve the original layout.
        content = {"text": message} if isinstance(message, str) else message
        request = {
            "chat_id": settings.telegram_chat_id,
            "text": content["text"],
            "link_preview_options": {"is_disabled": True},
        }
        if content.get("entities"):
            request["entities"] = content["entities"]
        try:
            response = await client.post(
                f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage",
                json=request,
            )
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError("Invalid Telegram response")
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
            delivery.state = "failed"
            delivery.error = "Could not connect to Telegram. Delivery will be retried."
            record.retry_at = now() + timedelta(minutes=1)
            session.commit()
            return delivery
        except Exception:
            delivery.state = "uncertain"
            delivery.error = "Delivery outcome unknown. Check the Telegram chat before sending again."
            session.commit()
            return delivery
        if not payload.get("ok"):
            delivery.state = "failed"
            wait = payload.get("parameters", {}).get("retry_after", 900)
            wait = min(max(wait, 1), 86400) if isinstance(wait, int) else 900
            record.retry_at = now() + timedelta(seconds=wait)
            delivery.error = (
                f"Telegram rejected this post (HTTP {response.status_code}). "
                "Check bot permissions; retry is scheduled."
            )
            session.commit()
            return delivery
        record.retry_at = None
        result = payload.get("result")
        message_id = result.get("message_id") if isinstance(result, dict) else None
        if message_id is not None:
            record.message_ids = [*record.message_ids, message_id]
        delivery.sent_count += 1
        delivery.state = "sent" if delivery.sent_count == delivery.total_count else "pending"
        session.commit()
    return delivery


async def poll_commands(session, settings: Settings):
    if not settings.telegram_ready or settings.telegram_target_type == "channel":
        return
    bot_key = hashlib.sha256(settings.telegram_bot_token.encode()).hexdigest()[:16]
    state = session.get(WorkerState, "telegram_offset:" + bot_key)
    offset = int(state.value) if state else 0
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(
            f"https://api.telegram.org/bot{settings.telegram_bot_token}/getUpdates",
            json={"offset": offset, "timeout": 0, "allowed_updates": ["message"]},
        )
        if response.status_code != 200 or not response.json().get("ok"):
            return
        for update in response.json()["result"]:
            message = update.get("message", {})
            if str(message.get("chat", {}).get("id", "")) == settings.telegram_chat_id:
                parts = message.get("text", "").split()
                command = parts[0].split("@")[0] if parts else ""
                if command == "/feed":
                    articles = prioritized_feed(session, preferences(session))
                    messages = format_articles(
                        articles, "News Feeder · Prioritized stories", preferences(session)
                    )
                elif command == "/digest":
                    digest = session.scalar(
                        select(Digest).order_by(Digest.end_at.desc()).limit(1)
                    ) or build_digest(session)
                    articles = [session.get(Article, article_id) for article_id in digest.article_ids]
                    messages = format_articles(
                        [article for article in articles if article],
                        "News Feeder · Daily digest",
                        preferences(session),
                    )
                else:
                    messages = [
                        "News Feeder\n/feed — latest stories\n/digest — latest digest\n/help — commands"
                    ]
                await deliver(session, settings, messages, f"command:{bot_key}:{update['update_id']}", client)
            if state is None:
                state = WorkerState(key="telegram_offset:" + bot_key, value="0")
                session.add(state)
            state.value = str(update["update_id"] + 1)
            session.commit()
