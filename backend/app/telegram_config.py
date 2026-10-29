import json
import re
from datetime import timedelta
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select

from .digests import preferences
from .models import Delivery, TelegramDelivery, WorkerState, aware, now

CONFIG_KEY = "telegram:configuration"


class TelegramSetupError(ValueError):
    pass


class TelegramUpdate(BaseModel):
    bot_token: str | None = Field(default=None, max_length=512)
    chat_id: str = Field(min_length=1, max_length=200)
    target_type: str = Field(default="channel", pattern="^(channel|private)$")

    @field_validator("bot_token")
    @classmethod
    def clean_token(cls, value):
        if value is None:
            return None
        value = value.strip()
        if not re.fullmatch(r"[0-9]{5,20}:[A-Za-z0-9_-]{20,}", value):
            raise ValueError("Enter the bot token from BotFather")
        return value

    @field_validator("chat_id")
    @classmethod
    def clean_chat(cls, value):
        value = value.strip()
        if value.startswith("https://t.me/"):
            parsed = urlsplit(value)
            if parsed.query or parsed.fragment or "/" in parsed.path.strip("/"):
                raise ValueError("Enter a public channel link or a numeric chat ID")
            value = "@" + parsed.path.strip("/")
        if not re.fullmatch(r"@[A-Za-z][A-Za-z0-9_]{4,31}|-?[0-9]{1,20}", value):
            raise ValueError("Enter @channel_name, https://t.me/channel_name, or a numeric chat ID")
        return value


class TelegramBotUpdate(BaseModel):
    bot_token: str = Field(max_length=512)

    @field_validator("bot_token")
    @classmethod
    def clean_token(cls, value):
        return TelegramUpdate.clean_token(value)


def saved_configuration(session):
    record = session.get(WorkerState, CONFIG_KEY)
    return json.loads(record.value) if record else {}


def effective_settings(settings, session):
    saved = saved_configuration(session)
    if not saved:
        return settings
    return settings.model_copy(
        update={
            "telegram_bot_token": saved["bot_token"],
            "telegram_chat_id": saved.get("chat_id", ""),
            "telegram_target_type": saved.get("target_type", "channel"),
        }
    )


async def telegram_request(client, token, method, payload=None):
    try:
        response = await client.post(f"https://api.telegram.org/bot{token}/{method}", json=payload or {})
        value = response.json()
    except Exception:
        raise TelegramSetupError("Could not reach Telegram. Check the connection and try again.") from None
    if not isinstance(value, dict) or not value.get("ok"):
        if response.status_code == 401:
            raise TelegramSetupError("Telegram rejected this bot token")
        if method == "getChatMember" and response.status_code in {400, 403}:
            raise TelegramSetupError(
                "Add the bot as a channel administrator with permission to post messages"
            )
        raise TelegramSetupError(
            "Telegram could not access this chat. Check its ID and the bot's permissions."
        )
    result = value.get("result")
    if (method == "getUpdates" and not isinstance(result, list)) or (
        method != "getUpdates" and not isinstance(result, dict)
    ):
        raise TelegramSetupError("Telegram returned an unexpected response. Try again later.")
    return result


async def configure(settings, session, value, client):
    saved = saved_configuration(session)
    token = value.bot_token or saved.get("bot_token") or settings.telegram_bot_token
    if not token:
        raise TelegramSetupError("Add the bot token from BotFather")
    bot = await telegram_request(client, token, "getMe")
    if not bot.get("is_bot"):
        raise TelegramSetupError("This token does not identify a Telegram bot")
    if value.chat_id.lower() == "@" + bot.get("username", "").lower():
        raise TelegramSetupError(
            "That is the bot's profile. Enter the destination channel or your private chat ID."
        )
    chat = await telegram_request(client, token, "getChat", {"chat_id": value.chat_id})
    if chat.get("type") != value.target_type:
        raise TelegramSetupError(
            "Choose a channel for channel delivery, or a private chat for personal delivery"
        )
    if value.target_type == "channel":
        member = await telegram_request(
            client, token, "getChatMember", {"chat_id": chat["id"], "user_id": bot["id"]}
        )
        if member.get("status") != "creator" and not (
            member.get("status") == "administrator" and member.get("can_post_messages") is True
        ):
            raise TelegramSetupError(
                "Add the bot as a channel administrator with permission to post messages"
            )
    result = {
        "bot_token": token,
        "bot_username": bot.get("username", ""),
        "chat_id": str(chat["id"]),
        "target_type": value.target_type,
        "title": chat.get("title") or chat.get("first_name") or "Private chat",
        "verified_at": now().isoformat(),
    }
    record = session.get(WorkerState, CONFIG_KEY)
    if record:
        record.value = json.dumps(result)
    else:
        session.add(WorkerState(key=CONFIG_KEY, value=json.dumps(result)))
    session.commit()
    return public_configuration(settings, session)


async def configure_bot(settings, session, value, client):
    saved = saved_configuration(session)
    if saved.get("chat_id"):
        return await configure(
            settings,
            session,
            TelegramUpdate(
                bot_token=value.bot_token, chat_id=saved["chat_id"], target_type=saved["target_type"]
            ),
            client,
        )
    bot = await telegram_request(client, value.bot_token, "getMe")
    if not bot.get("is_bot"):
        raise TelegramSetupError("This token does not identify a Telegram bot")
    result = {
        "bot_token": value.bot_token,
        "bot_username": bot.get("username", ""),
        "chat_id": "",
        "target_type": "channel",
    }
    record = session.get(WorkerState, CONFIG_KEY)
    if record:
        record.value = json.dumps(result)
    else:
        session.add(WorkerState(key=CONFIG_KEY, value=json.dumps(result)))
    session.commit()
    return public_configuration(settings, session)


def public_configuration(settings, session):
    current = effective_settings(settings, session)
    saved = saved_configuration(session)
    prefs = preferences(session)
    local = now().astimezone(ZoneInfo(prefs.timezone))
    next_at = local.replace(hour=prefs.delivery_hour, minute=0, second=0, microsecond=0)
    if next_at <= local:
        next_at += timedelta(days=1)
    last = session.scalar(
        select(Delivery)
        .join(TelegramDelivery, TelegramDelivery.delivery_id == Delivery.id)
        .where(Delivery.dedupe_key.startswith("daily:"), TelegramDelivery.target == current.telegram_chat_id)
        .order_by(Delivery.created_at.desc())
        .limit(1)
    )
    return {
        "configured": current.telegram_ready,
        "bot_token_configured": bool(current.telegram_bot_token),
        "chat_id": current.telegram_chat_id,
        "target_type": saved.get("target_type", ""),
        "title": saved.get("title", ""),
        "bot_username": saved.get("bot_username", ""),
        "verified": bool(saved.get("verified_at")),
        "daily_enabled": prefs.telegram_enabled,
        "next_delivery_at": aware(next_at).isoformat() if prefs.telegram_enabled else None,
        "last_delivery": {
            "id": last.id,
            "state": last.state,
            "sent_count": last.sent_count,
            "total_count": last.total_count,
            "error": last.error,
            "created_at": aware(last.created_at).isoformat(),
        }
        if last
        else None,
    }


async def recent_targets(settings, session, client):
    token = saved_configuration(session).get("bot_token") or settings.telegram_bot_token
    if not token:
        raise TelegramSetupError("Save the bot token before finding your chat")
    updates = await telegram_request(
        client,
        token,
        "getUpdates",
        {
            "offset": 0,
            "timeout": 0,
            "limit": 100,
            "allowed_updates": ["message", "my_chat_member", "channel_post"],
        },
    )
    chats = {}
    for update in updates:
        event = update.get("my_chat_member") or update.get("channel_post") or update.get("message") or {}
        chat = event.get("chat", {})
        if chat.get("type") in {"channel", "private"}:
            chats[str(chat["id"])] = {
                "id": str(chat["id"]),
                "title": chat.get("title") or chat.get("first_name") or "Private chat",
                "type": chat["type"],
            }
    return list(chats.values())
