import json
from datetime import timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import Settings
from app.curation import curation_signals, github_update
from app.digests import build_digest, preferences, prepare_daily_digest
from app.main import create_app
from app.models import Article, DailyDigest, Delivery, Digest, Preference, TelegramDelivery, now
from app.relevance import prioritized_feed


def story(title, text="", url=None):
    return Article(
        source_id="news",
        source_name="Engineering news",
        title=title,
        url=url or "https://example.com/" + title.replace(" ", "-"),
        text=text,
        content_state="full",
        discovered_at=now() - timedelta(minutes=5),
        published_at=now() - timedelta(minutes=5),
        summary_state="pending",
        content_hash="source",
    )


@pytest.fixture
def application(tmp_path):
    return create_app(
        Settings(
            database_url=f"sqlite:///{tmp_path}/news.db",
            worker_enabled=False,
            telegram_bot_token="test-token",
            telegram_chat_id="-100123456",
            telegram_target_type="channel",
        )
    )


@pytest.mark.parametrize(
    "tag,notes,expected",
    [
        ("v2.0.0", "", "major"),
        ("v2.0.0rc1", "Introduces a new distributed query engine", "routine"),
        ("v1.4.1", "", "routine"),
        ("v1.4.1", "### Features\n* Bump dependencies and update documentation", "routine"),
        ("v1.4.1", "* feat: update docs for agents", "routine"),
        ("v1.4.1", "* feat(runtime): Add durable execution with checkpoint recovery", "major"),
        ("v1.4.1", "* Added support for distributed streaming joins", "major"),
        ("v1.4.1", "* Fix data corruption when recovering interrupted transactions", "major"),
        ("v1.4.1", "* Bump dependency to fix CVE-2026-12345", "major"),
        ("v1.4.1", "* Fix a typo in the error message", "routine"),
        ("v1.4.1", "### Features\n* Rename the test helper", "routine"),
        ("v1.4.1", "### Breaking Changes\nNone\n### Maintenance\nBump dependencies", "routine"),
        ("v1.4.1", "No breaking changes in this patch", "routine"),
        ("v1.4.1", "### Breaking Changes\n* Drop support for the legacy execution engine", "major"),
        ("v1.4.1", "* Fix security vulnerability in session verification", "major"),
    ],
)
def test_release_filter_requires_evidence_beyond_version_bumps(tag, notes, expected):
    article = story("Library " + tag, notes, "https://github.com/org/library/releases/tag/" + tag)
    assert github_update(article) == expected


def test_repositories_and_ordinary_articles_are_retained_without_a_summary():
    repo = story(
        "New public repository: org/data-engine", "Data pipelines", "https://github.com/org/data-engine"
    )
    assert github_update(repo) == "repository"
    assert curation_signals(repo)["data_engineering"] > 0
    assert github_update(story("How to build agents")) == "none"
    # Mentions of GitHub in an ordinary article do not turn it into a release post.
    assert github_update(story("GitHub developer news", "Bump dependencies")) == "none"
    signals = curation_signals(story("Databricks releases new AI agents"))
    assert signals["agentic_ai"] > signals["data_engineering"]
    assert (
        github_update(
            story(
                "Fix critical data corruption during checkpoint recovery",
                url="https://github.com/org/repo/pull/123",
            )
        )
        == "major"
    )
    assert (
        github_update(story("Bump dependencies and update docs", url="https://github.com/org/repo/pull/124"))
        == "routine"
    )


def add_stories(application):
    with application.state.sessions() as session:
        articles = [
            story("Data engineering lakehouse", "New data ingestion pipelines"),
            story("Agentic AI with LangGraph", "Multi-agent tools and MCP support"),
            story(
                "Library v1.4.1",
                "Bump dependencies and fix documentation",
                "https://github.com/org/library/releases/tag/v1.4.1",
            ),
            story(
                "New public repository: org/agents", "Agent orchestration", "https://github.com/org/agents"
            ),
        ]
        session.add_all(articles)
        session.commit()
        return [a.id for a in articles]


def test_mix_persists_preserves_other_settings_and_prepares_a_new_digest(application):
    data, agent, routine, repo = add_stories(application)
    with TestClient(application) as client:
        initial = client.get("/preferences").json()
        assert initial["news_focus"] == 50 and initial["major_github_only"] is True
        # Archives still sync every story so Saved and switching the filter work offline.
        archived = client.get("/articles").json()
        assert len(archived) == 4
        assert next(a for a in archived if a["id"] == routine)["curation"]["github_update"] == "routine"
        assert client.put("/news-mix", json={"news_focus": 0, "major_github_only": True}).status_code == 200
        first = client.post("/digests").json()
        assert first["articles"][0]["id"] == data
        assert routine not in [a["id"] for a in first["articles"]]
        response = client.put("/news-mix", json={"news_focus": 100, "major_github_only": True})
        assert response.status_code == 200
        assert response.json()["profile"] == initial["profile"]
        assert response.json()["delivery_hour"] == initial["delivery_hour"]
        editions = client.get("/digests").json()
        assert editions[0]["id"] != first["id"]
        assert editions[0]["articles"][0]["id"] in {agent, repo}
        assert next(d for d in editions if d["id"] == first["id"])["articles"][0]["id"] == data
        # A stale Digest settings window cannot overwrite the independently saved mix.
        initial["digest_size"] = 3
        assert client.put("/preferences", json=initial).json()["news_focus"] == 100
        assert client.put("/profile", json={"profile": "My priorities"}).json()["news_focus"] == 100
        for invalid in [-1, 101, 0.5, True, "50"]:
            assert client.put("/news-mix", json={"news_focus": invalid}).status_code == 422
    restarted = create_app(application.state.settings)
    with TestClient(restarted) as client:
        assert client.get("/preferences").json()["news_focus"] == 100


async def test_feed_digest_and_all_telegram_editions_use_the_mix(application, monkeypatch):
    data, agent, routine, repo = add_stories(application)
    posts = []

    def telegram(request):
        assert request.url.host == "api.telegram.org"
        body = json.loads(request.content)
        posts.append(body["text"])
        return httpx.Response(200, json={"ok": True, "result": {"message_id": len(posts)}})

    original = httpx.AsyncClient
    monkeypatch.setattr(
        "app.telegram.httpx.AsyncClient", lambda **kw: original(transport=httpx.MockTransport(telegram), **kw)
    )

    async def no_refresh(*args, **kwargs):
        return None

    monkeypatch.setattr(application.state.service, "refresh", no_refresh)
    with TestClient(application) as client:
        client.put("/news-mix", json={"news_focus": 0, "major_github_only": True}).raise_for_status()
        with application.state.sessions() as session:
            prefs = preferences(session)
            ordered = prioritized_feed(session, prefs)
            assert ordered[0].id == data
            assert routine not in [a.id for a in ordered]
            digest = build_digest(session)
            assert digest.article_ids == [a.id for a in ordered]
        for kind in ["feed", "digest"]:
            posts.clear()
            result = client.post("/telegram/send", json={"kind": kind})
            assert result.status_code == 200 and result.json()["state"] == "sent"
            text = "".join(posts)
            assert text.index("Data engineering lakehouse") < text.index("Agentic AI with LangGraph")
            assert "Library v1.4.1" not in text
        # A prepared daily edition changes before delivery, while the old digest remains archived.
        with application.state.sessions() as session:
            frozen = prepare_daily_digest(session)
            frozen_id = frozen.id
            assert frozen.article_ids[0] == data
        client.put("/news-mix", json={"news_focus": 100, "major_github_only": True}).raise_for_status()
        posts.clear()
        assert client.post("/telegram/daily", json={"scheduled": False}).json()["state"] == "sent"
        text = "".join(posts)
        assert text.index("Agentic AI with LangGraph") < text.index("Data engineering lakehouse")
        with application.state.sessions() as session:
            assert session.get(Digest, frozen_id).article_ids[0] == data
            assert session.scalar(select(DailyDigest)).digest_id != frozen_id
        # Sent daily editions remain fixed and are never resent on a preference change.
        client.put("/news-mix", json={"news_focus": 0, "major_github_only": False}).raise_for_status()
        posts.clear()
        assert client.post("/telegram/daily", json={"scheduled": False}).json()["reason"] == "already_claimed"
        assert posts == []
        with application.state.sessions() as session:
            assert routine in [a.id for a in prioritized_feed(session, preferences(session))]
            assert (
                session.scalar(select(Delivery).where(Delivery.dedupe_key.startswith("daily:"))).state
                == "sent"
            )


def test_focus_works_without_gateway_or_relevance_profile(application):
    data, agent, routine, repo = add_stories(application)
    with application.state.sessions() as session:
        owner = session.get(Preference, "owner")
        owner.value = {**owner.value, "profile": "", "relevance_enabled": False, "news_focus": 0}
        session.commit()
        assert prioritized_feed(session, preferences(session))[0].id == data
        owner.value = {**owner.value, "news_focus": 100}
        session.commit()
        assert prioritized_feed(session, preferences(session))[0].id in {agent, repo}


def test_failed_daily_retry_preserves_edition_cutoff_after_news_mix_changes(application, monkeypatch):
    add_stories(application)
    posts = []

    def telegram(request):
        posts.append(json.loads(request.content))
        if len(posts) == 1:
            return httpx.Response(429, json={"ok": False, "parameters": {"retry_after": 30}})
        return httpx.Response(200, json={"ok": True, "result": {"message_id": len(posts)}})

    original = httpx.AsyncClient
    monkeypatch.setattr(
        "app.telegram.httpx.AsyncClient", lambda **kw: original(transport=httpx.MockTransport(telegram), **kw)
    )

    async def no_refresh(*args, **kwargs):
        return None

    monkeypatch.setattr(application.state.service, "refresh", no_refresh)
    with TestClient(application) as client:
        first = client.post("/telegram/daily", json={"scheduled": False})
        assert first.status_code == 200 and first.json()["state"] == "failed"
        with application.state.sessions() as session:
            edition_id = session.scalar(select(DailyDigest)).digest_id
            cutoff = session.get(Digest, edition_id).end_at
            delivery_id = session.scalar(select(Delivery)).id
            session.get(TelegramDelivery, delivery_id).retry_at = now() - timedelta(seconds=1)
            session.commit()
        client.put("/news-mix", json={"news_focus": 100, "major_github_only": True}).raise_for_status()
        retried = client.post("/telegram/daily", json={"scheduled": False})
        assert retried.status_code == 200 and retried.json()["state"] == "sent"
        assert posts == [posts[0], posts[0]]
        with application.state.sessions() as session:
            assert session.scalar(select(DailyDigest)).digest_id == edition_id
            assert session.get(Digest, edition_id).end_at == cutoff
            assert session.scalar(select(Delivery)).id == delivery_id
