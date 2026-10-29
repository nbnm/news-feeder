import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import Settings
from app.main import create_app
from app.models import Article, Delivery, TelegramDelivery, now
from app.schemas import Preferences
from app.telegram import deliver, format_articles
from app.telegram_format import MESSAGE_LIMIT, split_formatted_message, utf16_length


def story(**changes):
    values = {
        "source_id": "vendor",
        "source_name": "Vendor & Labs",
        "title": "New model 🚀 <preview> & _API_",
        "url": "https://example.com/release?version=1&preview=true",
        "text": "Original source text",
        "summary_state": "ready",
        "summary": {"tldr": "A faster model with tool calling.", "why_it_matters": "Build more capable agents."},
    }
    return Article(**(values | changes))


def entity_text(message, entity):
    text = message["text"].encode("utf-16-le")
    return text[entity["offset"] * 2 : (entity["offset"] + entity["length"]) * 2].decode("utf-16-le")


def check_entities(message):
    assert 0 < utf16_length(message["text"]) <= MESSAGE_LIMIT
    for entity in message["entities"]:
        assert entity["length"] > 0 and entity["offset"] >= 0
        assert entity["offset"] + entity["length"] <= utf16_length(message["text"])
        assert entity_text(message, entity)


def test_news_has_bold_structure_and_literal_source_text_with_clickable_citation():
    item = story()
    message, = format_articles([item], "News Feeder · Daily digest")
    check_entities(message)
    bold = [entity_text(message, entity) for entity in message["entities"] if entity["type"] == "bold"]
    assert bold == ["🗞 News Feeder · Daily digest", "1. " + item.title, "⚡ TLDR", "💡 Why it matters"]
    assert "<preview> & _API_" in message["text"]
    assert "Preview: source excerpt" not in message["text"]
    links = [entity for entity in message["entities"] if entity["type"] == "text_link"]
    assert len(links) == 1 and links[0]["url"] == item.url
    assert entity_text(message, links[0]) == "↗ Read source"
    assert "parse_mode" not in message


@pytest.mark.parametrize(("timestamp", "expected"), [
    (datetime(2026, 10, 29, 18, 30, tzinfo=UTC), "29 Oct 2026 · 14:30 EDT (UTC-0400)"),
    (datetime(2026, 11, 2, 18, 30, tzinfo=UTC), "02 Nov 2026 · 13:30 EST (UTC-0500)"),
])
def test_publication_timestamp_uses_profile_timezone_and_dst(timestamp, expected):
    item = story(published_at=timestamp, discovered_at=datetime(2026, 11, 3, tzinfo=UTC))
    message, = format_articles([item], "Feed", Preferences(timezone="America/Toronto"))
    assert "🕒 Published: " + expected in message["text"]
    assert "First seen" not in message["text"]
    check_entities(message)


def test_missing_publication_time_is_labeled_first_seen_and_naive_archive_dates_are_utc():
    item = story(published_at=None, discovered_at=datetime(2026, 10, 29, 18, 30))
    message, = format_articles([item], "Feed", Preferences(timezone="America/Toronto"))
    assert "🕒 First seen: 29 Oct 2026 · 14:30 EDT (UTC-0400)" in message["text"]
    assert "Published:" not in message["text"]
    check_entities(message)


def test_timestamp_respects_a_non_default_timezone_and_calendar_date():
    item = story(published_at=datetime(2026, 10, 29, 23, 30, tzinfo=UTC))
    message, = format_articles([item], "Feed", Preferences(timezone="Asia/Tokyo"))
    assert "🕒 Published: 30 Oct 2026 · 08:30 JST (UTC+0900)" in message["text"]


@pytest.mark.parametrize("url", ["javascript:alert(1)", "tg://unsafe", "https://user:password@example.com"])
def test_source_links_cannot_inject_other_protocols_or_credentials(url):
    message, = format_articles([story(url=url)], "Feed")
    assert not any(entity["type"] == "text_link" for entity in message["entities"])
    assert "Source link unavailable" in message["text"]


def test_excerpt_and_headline_fallbacks_and_empty_digest_stay_explicit():
    excerpt = story(summary=None, summary_state="pending")
    headline = story(summary=None, summary_state="pending", text="")
    message, = format_articles([excerpt, headline], "Feed")
    assert "Preview: source excerpt" in message["text"] and "Preview: headline only" in message["text"]
    empty, = format_articles([], "Feed\nSome sources could not refresh.")
    assert "0 stories" in empty["text"] and "No new stories in this period." in empty["text"]
    assert "Some sources could not refresh." in empty["text"]
    check_entities(empty)


def test_digest_keeps_normal_stories_whole_with_numbered_continuation_messages():
    articles = [
        story(title=f"Release {i}", url=f"https://example.com/{i}", summary={"tldr": "Summary " * 40, "why_it_matters": "Useful context. " * 50})
        for i in range(8)
    ]
    messages = format_articles(articles, "Daily digest")
    assert 1 < len(messages) < len(articles)
    for index, message in enumerate(messages, 1):
        check_entities(message)
        assert f"Part {index}/{len(messages)}" in message["text"]
    for article in articles:
        containing = [message for message in messages if article.title in message["text"]]
        assert len(containing) == 1
        assert article.url in [entity.get("url") for entity in containing[0]["entities"]]


@pytest.mark.parametrize("limit", [40, 71, MESSAGE_LIMIT])
def test_unicode_splitting_preserves_text_valid_entities_and_source_link(limit):
    text = "🧪 A <model> & _literal_\n\n" + ("Context 🚀 " * 1000) + "Read source"
    link_start = utf16_length(text.removesuffix("Read source"))
    original = {"text": text, "entities": [
        {"type": "bold", "offset": 0, "length": utf16_length("🧪 A <model> & _literal_")},
        {"type": "text_link", "offset": link_start, "length": utf16_length("Read source"), "url": "https://example.com"},
    ]}
    pieces = split_formatted_message(original, limit)
    assert "".join(piece["text"] for piece in pieces) == text
    for piece in pieces:
        check_entities(piece)
        assert utf16_length(piece["text"]) <= limit
    links = [(piece, entity) for piece in pieces for entity in piece["entities"] if entity["type"] == "text_link"]
    assert len(links) == 1 and entity_text(*links[0]) == "Read source"


def test_oversized_story_preserves_all_text_and_has_a_readable_continuation_heading():
    item = story(summary={"tldr": "Summary", "why_it_matters": "Context <safe> 🚀 " * 1000})
    messages = format_articles([item], "Daily digest")
    assert len(messages) > 1
    text = "".join(message["text"] for message in messages)
    assert text.count("Context") == 1000 and text.count("<safe>") == 1000 and text.count("🚀") == 1001
    assert "1. Continued" in text
    for message in messages:
        check_entities(message)
    assert sum(entity.get("url") == item.url for message in messages for entity in message["entities"]) == 1


@pytest.fixture
def application(tmp_path):
    return create_app(Settings(database_url=f"sqlite:///{tmp_path}/format.db", worker_enabled=False, telegram_bot_token="test", telegram_chat_id="123"))


async def test_rich_delivery_preserves_formatting_and_skips_acknowledged_parts_after_restart(application):
    messages = format_articles([
        story(title=str(i), summary={"tldr": "Summary", "why_it_matters": "Useful context " * 150}) for i in range(2)
    ], "Daily digest")
    assert len(messages) == 2
    posted = []

    def respond(request):
        payload = json.loads(request.content)
        posted.append(payload)
        assert payload["entities"] and "parse_mode" not in payload
        if len(posted) == 2:
            return httpx.Response(429, json={"ok": False, "parameters": {"retry_after": 30}})
        return httpx.Response(200, json={"ok": True, "result": {"message_id": len(posted)}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with application.state.sessions() as session:
            partial = await deliver(session, application.state.settings, messages, "rich:restart", client)
            assert partial.state == "failed" and partial.sent_count == 1
            session.get(TelegramDelivery, partial.id).retry_at = now() - timedelta(seconds=1)
            session.commit()
        with application.state.sessions() as session:
            receipt = session.scalar(select(Delivery).where(Delivery.dedupe_key == "rich:restart"))
            assert receipt.messages == messages
            final = await deliver(session, application.state.settings, ["replacement ignored"], "rich:restart", client)
            assert final.state == "sent" and final.sent_count == 2
    assert len(posted) == 3 and posted[1] == posted[2] and posted[0]["text"] != posted[2]["text"]


async def test_legacy_plain_delivery_is_not_reinterpreted_as_markup_after_upgrade(application):
    literal = "Saved <article> & **literal text** 🚀"
    with application.state.sessions() as session:
        record = Delivery(dedupe_key="legacy:pending", total_count=1, messages=[literal], state="failed")
        session.add(record)
        session.flush()
        session.add(TelegramDelivery(delivery_id=record.id, target="123"))
        session.commit()
    posted = []

    def respond(request):
        posted.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    with application.state.sessions() as session:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            result = await deliver(session, application.state.settings, format_articles([story()], "Feed"), "legacy:pending", client)
            assert result.state == "sent"
    assert posted[0]["text"] == literal and "entities" not in posted[0] and "parse_mode" not in posted[0]


def test_feed_digest_and_single_story_endpoints_all_send_formatted_news(application, monkeypatch):
    posted = []

    def respond(request):
        posted.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": len(posted)}})

    original = httpx.AsyncClient
    monkeypatch.setattr("app.telegram.httpx.AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(respond), **kwargs))
    with application.state.sessions() as session:
        item = story()
        session.add(item)
        session.commit()
        article_id = item.id
    with TestClient(application) as client:
        for body in ({"kind": "feed"}, {"kind": "digest"}, {"article_id": article_id}):
            result = client.post("/telegram/send", json=body)
            assert result.status_code == 200 and result.json()["state"] == "sent"
    assert len(posted) == 3
    for payload in posted:
        check_entities(payload)
        assert "⚡ TLDR" in payload["text"]
        assert any(entity["type"] == "bold" for entity in payload["entities"])
        assert item.url in [entity.get("url") for entity in payload["entities"]]
