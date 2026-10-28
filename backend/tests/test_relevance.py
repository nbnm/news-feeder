import asyncio
import json
from datetime import timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import Settings
from app.digests import build_digest, preferences
from app.main import create_app
from app.models import Article, Preference, now
from app.relevance import (
    JevError,
    article_output,
    current_relevance,
    prioritized_feed,
    process_relevance,
    score_article,
)
from app.schemas import Preferences
from app.service import Service
from app.telegram import format_articles


@pytest.fixture
def application(tmp_path):
    return create_app(Settings(database_url=f"sqlite:///{tmp_path}/news.db", worker_enabled=False))


def story(title="Agent API update", topics=None):
    return Article(
        source_id="one",
        source_name="Example",
        title=title,
        url="https://example.com/" + title,
        discovered_at=now(),
        published_at=now(),
        text="A new agent API is available.",
        content_state="full",
        content_hash="one",
        summary_state="ready",
        summary={
            "tldr": "New agent API.",
            "what_changed": "API release",
            "why_it_matters": "New tools",
            "key_points": [],
            "topics": topics if topics is not None else ["Agents", "APIs"],
            "impact": "medium",
            "citations": [{"quote": "A new agent API is available."}],
        },
    )


def answer(score=3.2, confidence=0.8):
    return {"type": "score", "score": score, "confidence": confidence}


def response(request):
    body = json.loads(request.content)
    return httpx.Response(
        200, json={"model": "jev-1.13.0", "answers": {key: answer() for key in body["questions"]}}
    )


async def test_jev_contract_topic_scores_and_normalization():
    def respond(request):
        assert str(request.url) == "https://secondstack.t1a.io/gateway/api/v1/systemone"
        assert request.headers["Authorization"] == "Bearer virtual-key"
        body = json.loads(request.content)
        assert body["model"] == "typesafe/jev-1.13"
        assert body["state"]["reader_profile"] == "I develop agents and APIs."
        assert list(body["questions"]) == ["story", "topic_0", "topic_1"]
        assert all(q["type"] == "score" and len(q["criteria"]) == 5 for q in body["questions"].values())
        result = response(request).json()
        result["answers"]["topic_1"] = answer(1, 0.6)
        return httpx.Response(200, json=result)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await score_article(
            story(),
            Preferences(profile="I develop agents and APIs."),
            Settings(litellm_api_key="virtual-key"),
            client,
        )
    assert result.score == 80
    assert result.confidence == 0.8
    assert [(t.topic, t.score) for t in result.topics] == [("Agents", 80), ("APIs", 25)]
    assert result.model == "jev-1.13.0"


async def test_profile_and_evidence_change_invalidate_cached_scores(application):
    calls = []

    def respond(request):
        calls.append(request)
        return response(request)

    with application.state.sessions() as session:
        article = story()
        session.add(article)
        session.commit()
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            settings = Settings(litellm_api_key="key")
            assert await process_relevance(session, settings, client) == 1
            assert current_relevance(article, preferences(session))["score"] == 80
            assert await process_relevance(session, settings, client, force=True) == 0
            owner = session.get(Preference, "owner")
            owner.value = {**owner.value, "profile": "Only database releases."}
            session.commit()
            output = article_output(article, preferences(session))
            assert output.relevance is None and output.relevance_state == "pending"
            assert await process_relevance(session, settings, client) == 1
            article.text = "The API was withdrawn."
            session.commit()
            assert current_relevance(article, preferences(session)) is None
            assert await process_relevance(session, settings, client) == 1
        assert len(calls) == 3


async def test_failure_is_sanitized_backed_off_and_retryable(application):
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(401, text="secret-key upstream credential failed")

    with application.state.sessions() as session:
        article = story()
        session.add(article)
        session.commit()
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            settings = Settings(litellm_api_key="secret-key")
            assert await process_relevance(session, settings, client) == 0
            output = article_output(article, preferences(session))
            assert output.relevance is None and output.relevance_state == "error"
            assert "401" in output.relevance_error and "secret-key" not in output.relevance_error
            assert await process_relevance(session, settings, client) == 0
            assert len(calls) == 1
            assert await process_relevance(session, settings, client, force=True) == 0
            assert len(calls) == 2


@pytest.mark.parametrize(
    "invalid",
    [
        answer(-1),
        answer(4.1),
        answer(float("nan")),
        answer(2, 1.1),
        answer(True),
        {"type": "noul", "noul": 0.9},
    ],
)
async def test_invalid_scores_never_become_relevance(invalid):
    def respond(request):
        body = json.loads(request.content)
        # json.dumps deliberately exercises non-finite inputs from an upstream response.
        return httpx.Response(
            200,
            content=json.dumps(
                {"model": "jev-1.13.0", "answers": {key: invalid for key in body["questions"]}}
            ),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(JevError, match="invalid relevance"):
            await score_article(story(), Preferences(), Settings(litellm_api_key="key"), client)


async def test_ranking_digest_telegram_and_unknown_vs_zero(application):
    def respond(request):
        body = json.loads(request.content)
        score = 0 if body["state"]["news"]["title"] == "Low interest" else 4
        return httpx.Response(
            200, json={"model": "jev-1.13.0", "answers": {key: answer(score) for key in body["questions"]}}
        )

    with application.state.sessions() as session:
        high, low = story("Older relevant"), story("Low interest")
        high.published_at = now() - timedelta(hours=4)
        session.add_all([high, low])
        session.commit()
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            await process_relevance(session, Settings(litellm_api_key="key"), client)
        unknown = story("Unscored")
        session.add(unknown)
        session.commit()
        prefs = preferences(session)
        assert [a.id for a in prioritized_feed(session, prefs)] == [high.id, low.id, unknown.id]
        assert current_relevance(low, prefs)["score"] == 0
        digest = build_digest(session)
        assert digest.article_ids == [high.id, low.id, unknown.id]
        formatted = "".join(format_articles([high, low], "Feed", prefs))
        assert "Relevance to you: High" in formatted
        assert "Relevance to you: Low" in formatted
        assert "/100" not in formatted
        owner = session.get(Preference, "owner")
        owner.value = {**owner.value, "relevance_enabled": False}
        session.commit()
        prefs = preferences(session)
        assert current_relevance(high, prefs) is None
        assert prioritized_feed(session, prefs)[0].id == unknown.id
        assert "Relevance to you" not in "".join(format_articles([high], "Feed", prefs))


def test_profile_persists_without_overwriting_digest_settings(application):
    with TestClient(application) as client:
        old = client.get("/preferences").json()
        changed = client.put(
            "/profile", json={"profile": "My AI engineering priorities.", "relevance_enabled": True}
        )
        assert changed.status_code == 200
        assert changed.json()["profile_hash"] != old["profile_hash"]
        old["digest_size"] = 4
        assert client.put("/preferences", json=old).json()["profile"] == "My AI engineering priorities."
        assert client.get("/preferences").json()["digest_size"] == 4
        assert client.put("/profile", json={"profile": "a" * 6001}).status_code == 422
        assert client.post("/relevance/refresh").status_code == 202
    restarted = create_app(application.state.settings)
    with TestClient(restarted) as client:
        assert client.get("/preferences").json()["profile"] == "My AI engineering priorities."


async def test_blank_disabled_or_unconfigured_profile_does_not_call_gateway(application):
    def unexpected(request):
        pytest.fail("An unconfigured/disabled profile must not call Jev")

    with application.state.sessions() as session:
        session.add(story())
        session.commit()
        async with httpx.AsyncClient(transport=httpx.MockTransport(unexpected)) as client:
            assert await process_relevance(session, Settings(), client) == 0
            for profile, enabled in [("", True), ("Agents", False)]:
                owner = session.get(Preference, "owner")
                owner.value = {**owner.value, "profile": profile, "relevance_enabled": enabled}
                session.commit()
                assert await process_relevance(session, Settings(litellm_api_key="key"), client) == 0
        article = session.scalar(select(Article))
        assert article_output(article, preferences(session)).relevance_state == "disabled"


async def test_profile_updates_during_scoring_queue_the_latest_refresh(application, monkeypatch):
    service = Service(application.state.settings, application.state.sessions)
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def assess(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            entered.set()
            await release.wait()
        return 0

    monkeypatch.setattr("app.service.process_relevance", assess)
    service.start_relevance()
    await asyncio.wait_for(entered.wait(), timeout=2)
    service.start_relevance()
    release.set()
    await asyncio.wait_for(service.relevance_task, timeout=2)
    assert len(calls) == 2 and not service.ranking
