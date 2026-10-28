from uuid import uuid4

import httpx
from sqlalchemy import select

from .config import Settings
from .digests import build_digest, preferences
from .models import Article, Delivery, Digest, WorkerState
from .relevance import current_relevance, prioritized_feed, relevance_level


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


def format_articles(articles: list[Article], title: str, prefs=None) -> list[str]:
    blocks = [title]
    for index, article in enumerate(articles, 1):
        summary = article.summary or {}
        block = [
            f"{index}. {article.title}",
            article.source_name,
            "TLDR: " + article.tldr,
        ]
        if summary.get("why_it_matters"):
            block.append("Why it matters: " + summary["why_it_matters"])
        relevance = current_relevance(article, prefs) if prefs else None
        if relevance:
            block.append(f"Relevance to you: {relevance_level(relevance['score'])} · Jev assessment")
        if article.repository:
            repo = article.repository
            block.append(f"GitHub daily rank: #{repo['rank']} · {repo['language'] or 'Language unspecified'}")
            if repo["stars_today"] is not None:
                block.append(f"Stars gained today: {repo['stars_today']:,}")
            if repo["total_stars"] is not None:
                block.append(f"Total stars: {repo['total_stars']:,}")
            block.append(f"Trending snapshot: {repo['observed_at']}")
            block.append("Trending source: " + repo["trending_url"])
        if article.buzz:
            buzz = article.buzz
            block.append(f"HN buzz: {buzz['points']} points · {buzz['comments']} comments")
        block.append("Source: " + article.url)
        blocks.append("\n".join(block))
    if not articles:
        blocks.append("No new stories in this period.")
    return split_messages("\n\n".join(blocks))


async def deliver(
    session,
    settings: Settings,
    messages: list[str],
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
        session.commit()
    if client is None:
        async with httpx.AsyncClient(timeout=20) as owned_client:
            return await _send(session, settings, delivery, owned_client)
    return await _send(session, settings, delivery, client)


async def _send(session, settings, delivery, client):
    for message in delivery.messages[delivery.sent_count :]:
        # Persist before sending. A restart in this state is ambiguous and must not blindly resend.
        delivery.state, delivery.error = "sending", None
        session.commit()
        try:
            response = await client.post(
                f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage",
                json={
                    "chat_id": settings.telegram_chat_id,
                    "text": message,
                    "link_preview_options": {"is_disabled": True},
                },
            )
            payload = response.json()
        except Exception:
            delivery.state = "uncertain"
            delivery.error = "Delivery outcome unknown. Check the Telegram chat before sending again."
            session.commit()
            return delivery
        if not payload.get("ok"):
            delivery.state = "failed"
            delivery.error = (
                f"Telegram rejected the message (HTTP {response.status_code}). Check bot/chat setup."
            )
            session.commit()
            return delivery
        delivery.sent_count += 1
        delivery.state = "sent" if delivery.sent_count == delivery.total_count else "pending"
        session.commit()
    return delivery


async def poll_commands(session, settings: Settings):
    if not settings.telegram_ready:
        return
    state = session.get(WorkerState, "telegram_offset")
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
                await deliver(session, settings, messages, f"command:{update['update_id']}", client)
            if state is None:
                state = WorkerState(key="telegram_offset", value="0")
                session.add(state)
            state.value = str(update["update_id"] + 1)
            session.commit()
