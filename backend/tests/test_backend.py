import gzip
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.collectors import Candidate, matches_topics, parse_feed, parse_website
from app.config import Settings
from app.digests import build_digest, cutoff_at
from app.llm import summarize, validate_citations
from app.main import create_app
from app.models import Article, Source, aware, now
from app.network import Fetcher, canonical_url, public_url
from app.pipeline import record_signal, refresh
from app.schemas import Preferences, Summary
from app.service import Service
from app.telegram import deliver, poll_commands, split_messages


@pytest.fixture
def application(tmp_path):
    return create_app(
        Settings(
            database_url=f"sqlite:///{tmp_path}/test.db",
            demo_mode=True,
            worker_enabled=False,
            app_api_key="test-key",
        )
    )


@pytest.fixture
def client(application):
    with TestClient(application, headers={"Authorization": "Bearer test-key"}) as client:
        yield client


def test_auth_and_secret_redaction(client):
    assert client.get("/health").status_code == 200
    assert client.get("/articles", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert "test-key" not in client.get("/status").text


def test_source_crud_preserves_archive(client):
    original = client.get("/articles").json()
    source = client.post(
        "/sources", json={"name": "My feed", "kind": "rss", "url": "https://example.com/rss"}
    ).json()
    source["enabled"] = False
    assert client.put(f"/sources/{source['id']}", json=source).json()["enabled"] is False
    assert client.delete(f"/sources/{original[0]['source_id']}").status_code == 200
    assert len(client.get("/articles").json()) == len(original)
    assert client.delete(f"/sources/{source['id']}").status_code == 200
    assert not any(item["id"] == source["id"] for item in client.get("/sources").json())


def test_source_validation(client):
    assert (
        client.post("/sources", json={"name": " ", "kind": "rss", "url": "file:///etc/passwd"}).status_code
        == 422
    )
    assert (
        client.post(
            "/sources",
            json={"name": "A", "kind": "website", "url": "https://example.com", "article_pattern": "["},
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/sources",
            json={"name": "A", "kind": "website", "url": "https://example.com", "link_selector": "a["},
        ).status_code
        == 422
    )


def test_demo_disables_external_delivery_and_collection(client):
    assert client.get("/status").json()["demo"] is True
    assert client.post("/telegram/send", json={"kind": "feed"}).status_code == 409
    assert client.post(
        "/sources/preview", json={"name": "A", "kind": "rss", "url": "https://example.com"}
    ).json()["note"]


def test_feed_and_website_parsing():
    rss = b"""<rss version="2.0"><channel><title>Feed</title><item><title>A model release</title><link>https://example.com/story/?utm_source=feed</link><pubDate>Thu, 01 Oct 2026 12:00:00 GMT</pubDate><description>A new model.</description></item></channel></rss>"""
    atom = b"""<feed xmlns="http://www.w3.org/2005/Atom"><title>Feed</title><entry><id>1</id><title>Agent tools</title><link href="https://example.com/agents"/><updated>2026-10-01T12:00:00Z</updated><summary>Agent update.</summary></entry></feed>"""
    assert parse_feed(rss, "https://example.com/rss")[0].url == "https://example.com/story"
    assert parse_feed(atom, "https://example.com/atom")[0].excerpt == "Agent update."
    source = Source(url="https://example.com/news", link_selector="a[href]", article_pattern=r"^/news/[^/]+$")
    items = parse_website(
        '<a href="/news/new-model">A useful model announcement</a><a href="/about">About this organization</a>',
        source,
    )
    assert len(items) == 1
    cards = parse_website(
        '<div><h2>A useful model announcement</h2><a href="/news/new-model"></a></div>', source
    )
    assert cards[0].title == "A useful model announcement"
    assert matches_topics("A paid subscription", ["AI"]) is False
    assert matches_topics("AI model launch", ["AI"])
    assert canonical_url("https://example.com/a?utm_source=x&b=2#x") == "https://example.com/a?b=2"


async def test_private_source_urls_are_rejected():
    with pytest.raises(ValueError, match="private or local"):
        await public_url("http://127.0.0.1:8000/secret")


async def test_fetcher_decodes_compressed_content_once(monkeypatch):
    async def valid(url):
        pass

    monkeypatch.setattr("app.network.public_url", valid)
    content = b"A compressed article body"

    def respond(request):
        return httpx.Response(200, headers={"Content-Encoding": "gzip"}, content=gzip.compress(content))

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        response = await Fetcher(client).get("https://example.com/article")
    assert response.content == content


def summary_fixture():
    return {
        "tldr": "A new API enables a workflow.",
        "what_changed": "New API",
        "why_it_matters": "It enables a workflow.",
        "key_points": ["New API"],
        "topics": ["APIs"],
        "impact": "medium",
        "citations": [{"quote": "The new API is available."}],
    }


def summary_draft_fixture():
    value = summary_fixture()
    value.pop("citations")
    value["citation_ids"] = [1]
    return value


async def test_litellm_routing_and_citations():
    settings = Settings(
        litellm_base_url="https://secondstack.t1a.io/gateway/api/v1",
        litellm_api_key="virtual-key",
        litellm_model="summary-alias",
    )

    def respond(request):
        assert str(request.url) == "https://secondstack.t1a.io/gateway/api/v1/chat/completions"
        assert request.headers["Authorization"] == "Bearer virtual-key"
        payload = json.loads(request.content)
        assert payload["model"] == "summary-alias"
        assert payload["response_format"]["json_schema"]["strict"] is True
        assert "tldr" in payload["response_format"]["json_schema"]["schema"]["required"]
        assert payload["response_format"]["json_schema"]["schema"]["properties"]["citation_ids"]["items"][
            "enum"
        ] == [1]
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"finish_reason": "stop", "message": {"content": json.dumps(summary_draft_fixture())}}
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await summarize("API update", "The new API is available.", settings, client)
    assert result.impact == "medium"
    assert result.citations[0].quote == "The new API is available."
    with pytest.raises(ValueError, match="citation"):
        validate_citations(Summary.model_validate(summary_fixture()), "Different text")


async def test_collection_deduplicates_and_isolates_errors(application, monkeypatch):
    async def candidates(source, fetcher):
        if source.name == "Broken":
            raise ValueError("Invalid feed")
        return [
            Candidate(
                "Real article",
                "https://example.com/live?utm_source=feed",
                excerpt="Stored release text",
                content_state="full",
            )
        ]

    monkeypatch.setattr("app.pipeline.collect", candidates)
    with application.state.sessions() as session:
        session.add_all(
            [
                Source(name="Working", kind="rss", url="https://example.com/rss"),
                Source(name="Broken", kind="rss", url="https://example.com/broken"),
            ]
        )
        session.commit()
        settings = Settings(demo_mode=False)
        first = await refresh(session, settings, force=True)
        second = await refresh(session, settings, force=True)
        assert first["added"] == 1
        assert second["added"] == 0
        assert first["errors"][0]["source"] == "Broken"
        assert (
            session.scalar(select(Article).where(Article.url == "https://example.com/live")).text
            == "Stored release text"
        )


def test_digest_windows_and_membership(application):
    with application.state.sessions() as session:
        end = now()
        first = build_digest(session, end)
        assert len(first.article_ids) == 3
        assert build_digest(session, end).id == first.id
        second = build_digest(session, end + timedelta(hours=1))
        assert aware(second.start_at) == aware(first.end_at)
        assert second.article_ids == []
        assert build_digest(session, end).article_ids == first.article_ids


def test_timezone_cutoff_and_dst():
    prefs = Preferences(timezone="America/Toronto", delivery_hour=8)
    assert cutoff_at(datetime(2026, 10, 1, 11, 0, tzinfo=UTC), prefs) == datetime(
        2026, 9, 30, 12, 0, tzinfo=UTC
    )
    assert cutoff_at(datetime(2026, 11, 1, 14, 0, tzinfo=UTC), prefs) == datetime(
        2026, 11, 1, 13, 0, tzinfo=UTC
    )


def test_signal_growth_requires_history(application):
    with application.state.sessions() as session:
        article = session.scalar(select(Article).limit(1))
        record_signal(session, article, {"id": 123, "score": 42, "descendants": 8})
        session.commit()
        assert article.buzz["points_per_hour"] is None
        assert article.buzz["points"] == 42


async def test_telegram_delivery_idempotency_and_splitting(application):
    text = "A <model> & news\n" + "🤖" * 5000
    chunks = split_messages(text)
    assert "".join(chunks) == text
    assert all(len(part.encode("utf-16-le")) // 2 <= 3800 for part in chunks)
    settings = Settings(telegram_bot_token="token", telegram_chat_id="123")
    sent = []

    def respond(request):
        payload = json.loads(request.content)
        assert payload["chat_id"] == "123"
        assert "parse_mode" not in payload
        sent.append(payload["text"])
        return httpx.Response(200, json={"ok": True, "result": {"message_id": len(sent)}})

    with application.state.sessions() as session:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            result = await deliver(session, settings, chunks, "daily:one", client)
            assert result.state == "sent"
            await deliver(session, settings, chunks, "daily:one", client)
    assert len(sent) == len(chunks)


async def test_telegram_ambiguous_send_is_not_retried(application):
    settings = Settings(telegram_bot_token="token", telegram_chat_id="123")
    calls = []

    def timeout(request):
        calls.append(request)
        raise httpx.ReadTimeout("timeout", request=request)

    with application.state.sessions() as session:
        async with httpx.AsyncClient(transport=httpx.MockTransport(timeout)) as client:
            result = await deliver(session, settings, ["Story"], "daily:uncertain", client)
            assert result.state == "uncertain"
            await deliver(session, settings, ["Story"], "daily:uncertain", client)
    assert len(calls) == 1


async def test_bot_rejects_other_chats(application, monkeypatch):
    calls = []

    def respond(request):
        if request.url.path.endswith("getUpdates"):
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": [
                        {"update_id": 1, "message": {"chat": {"id": 999}, "text": "/feed"}},
                        {"update_id": 2, "message": {"chat": {"id": 123}, "text": "/help"}},
                    ],
                },
            )
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True})

    original = httpx.AsyncClient
    monkeypatch.setattr(
        "app.telegram.httpx.AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(respond), **kwargs),
    )
    with application.state.sessions() as session:
        await poll_commands(session, Settings(telegram_bot_token="token", telegram_chat_id="123"))
    assert len(calls) == 1
    assert calls[0]["chat_id"] == "123"


def test_preference_validation(client):
    assert client.put("/preferences", json={"timezone": "Not/AZone"}).status_code == 422
    result = client.put(
        "/preferences", json={"topics": ["Agents"], "digest_size": 5, "timezone": "America/Toronto"}
    )
    assert result.status_code == 200
    assert client.get("/preferences").json()["digest_size"] == 5


def test_production_requires_auth():
    with pytest.raises(RuntimeError, match="APP_API_KEY"):
        create_app(Settings(environment="production", app_api_key=""))


async def test_daily_delivery_survives_worker_restart(application, monkeypatch):
    from app.models import Preference

    timestamp = datetime(2026, 10, 29, 13, tzinfo=UTC)
    monkeypatch.setattr("app.service.now", lambda: timestamp)
    monkeypatch.setattr("app.digests.now", lambda: timestamp)

    settings = Settings(telegram_bot_token="token", telegram_chat_id="123")
    sends = []
    original = httpx.AsyncClient

    def respond(request):
        sends.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr(
        "app.telegram.httpx.AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(respond), **kwargs),
    )

    async def no_collection(*args, **kwargs):
        return {}

    async def no_poll(*args, **kwargs):
        pass

    monkeypatch.setattr("app.service.refresh", no_collection)
    monkeypatch.setattr("app.service.poll_commands", no_poll)
    with application.state.sessions() as session:
        prefs = Preferences(telegram_enabled=True)
        session.get(Preference, "owner").value = prefs.model_dump()
        session.commit()
    await Service(settings, application.state.sessions).tick()
    await Service(settings, application.state.sessions).tick()
    assert len(sends) == 1
