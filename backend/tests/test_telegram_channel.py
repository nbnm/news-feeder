import asyncio
import hashlib
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import Settings
from app.daily_runner import request_running_service, run_scheduled
from app.digests import build_digest, daily_delivery_key, daily_due, prepare_daily_digest
from app.main import create_app
from app.models import Article, DailyDigest, Delivery, Preference, TelegramDelivery, WorkerState
from app.schemas import Preferences
from app.service import Service
from app.telegram import deliver, format_articles, poll_commands
from app.telegram_config import recent_targets, remember_targets

TOKEN = "123456789:" + "q" * 35
CHANNEL = "-1001234567890"


@pytest.fixture
def application(tmp_path):
    return create_app(
        Settings(
            database_url=f"sqlite:///{tmp_path}/channel.db", worker_enabled=False, app_api_key="local-key"
        )
    )


@pytest.fixture
def telegram_api(monkeypatch):
    calls = []
    state = {"can_post": True, "chat_type": "channel", "chat_id": int(CHANNEL)}

    def respond(request):
        method = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content)
        calls.append((method, body))
        result = {
            "getMe": {"id": 123456789, "is_bot": True, "username": "example_news_bot"},
            "getChat": {"id": state["chat_id"], "type": state["chat_type"], "title": "Example news"},
            "getChatMember": {"status": "administrator", "can_post_messages": state["can_post"]},
            "getUpdates": [
                {
                    "update_id": 7,
                    ("message" if state["chat_type"] == "private" else "my_chat_member"): {
                        "chat": {"id": state["chat_id"], "type": state["chat_type"], "title": "Example news"}
                    },
                }
            ],
            "sendMessage": {"message_id": len(calls)},
        }[method]
        return httpx.Response(200, json={"ok": True, "result": result})

    original = httpx.AsyncClient
    monkeypatch.setattr(
        "app.main.httpx.AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(respond), **kwargs),
    )
    return calls, state


def test_bot_and_channel_configuration_redaction_and_permission_validation(application, telegram_api):
    calls, state = telegram_api
    with TestClient(application, headers={"Authorization": "Bearer local-key"}) as client:
        bot = client.put("/telegram/bot", json={"bot_token": TOKEN})
        assert bot.status_code == 200 and bot.json()["bot_token_configured"]
        assert not bot.json()["configured"] and not bot.json()["verified"]
        assert TOKEN not in bot.text
        assert client.put("/preferences", json={"telegram_enabled": True}).status_code == 409
        assert client.put("/telegram/config", json={"chat_id": "@example_news_bot"}).status_code == 422
        state["can_post"] = False
        denied = client.put("/telegram/config", json={"chat_id": "https://t.me/example_news"})
        assert denied.status_code == 422 and "administrator" in denied.text
        assert not client.get("/telegram/config").json()["verified"]
        state["can_post"] = True
        connected = client.put("/telegram/config", json={"chat_id": "https://t.me/example_news"})
        assert connected.status_code == 200 and connected.json()["chat_id"] == CHANNEL
        assert connected.json()["verified"] and connected.json()["configured"]
        assert ("getChat", {"chat_id": "@example_news"}) in calls
        assert client.get("/telegram/targets").json()[0]["id"] == CHANNEL
        assert client.put("/preferences", json={"telegram_enabled": True}).status_code == 200
        status = client.get("/status")
        assert status.json()["telegram_background_configured"]
        assert TOKEN not in status.text + client.get("/telegram/config").text
        for invalid in [TOKEN + " secret spaces", "bad-" + "secret" * 100]:
            response = client.put("/telegram/bot", json={"bot_token": invalid})
            assert response.status_code == 422 and invalid not in response.text
        assert not any(method == "sendMessage" for method, _ in calls)


def test_channel_target_must_be_a_channel(application, telegram_api):
    _, state = telegram_api
    state["chat_type"] = "private"
    with TestClient(application, headers={"Authorization": "Bearer local-key"}) as client:
        response = client.put("/telegram/config", json={"bot_token": TOKEN, "chat_id": "@example_news"})
        assert response.status_code == 422
        assert not client.get("/telegram/config").json()["configured"]


def test_private_chat_configuration_and_delivery(application, telegram_api):
    calls, state = telegram_api
    state.update(chat_type="private", chat_id=223344556, can_post=False)
    with TestClient(application, headers={"Authorization": "Bearer local-key"}) as client:
        response = client.put(
            "/telegram/config",
            json={"bot_token": TOKEN, "chat_id": "223344556", "target_type": "private"},
        )
        assert response.status_code == 200
        config = response.json()
        assert config["verified"] and config["target_type"] == "private" and config["chat_id"] == "223344556"
        assert client.get("/telegram/targets").json() == [
            {"id": "223344556", "title": "Example news", "type": "private"}
        ]
        assert not any(method in {"sendMessage", "getChatMember"} for method, _ in calls)
        assert client.put("/preferences", json={"telegram_enabled": True}).status_code == 200
        assert client.get("/status").json()["telegram_background_configured"]
        sent = client.post("/telegram/send", json={"kind": "feed"})
        assert sent.status_code == 200 and sent.json()["state"] == "sent"
        posts = [body for method, body in calls if method == "sendMessage"]
        assert len(posts) == 1 and posts[0]["chat_id"] == "223344556"


def test_private_chat_cannot_be_the_bot_itself(application, telegram_api):
    calls, state = telegram_api
    state.update(chat_type="private", chat_id=123456789)
    with TestClient(application, headers={"Authorization": "Bearer local-key"}) as client:
        rejected = client.put(
            "/telegram/config", json={"bot_token": TOKEN, "chat_id": "123456789", "target_type": "private"}
        )
        assert rejected.status_code == 422 and "bot's own ID" in rejected.json()["detail"]
        username = client.put(
            "/telegram/config", json={"bot_token": TOKEN, "chat_id": "@someone_else", "target_type": "private"}
        )
        assert username.status_code == 422 and "Find My Chat" in username.json()["detail"]
        assert not client.get("/telegram/config").json()["configured"]
        assert not any(method == "sendMessage" for method, _ in calls)


def test_retry_requires_confirmation_and_skips_acknowledged_posts(application, telegram_api):
    calls, _ = telegram_api
    with TestClient(application, headers={"Authorization": "Bearer local-key"}) as client:
        client.put(
            "/telegram/config", json={"bot_token": TOKEN, "chat_id": "@example_news"}
        ).raise_for_status()
        with application.state.sessions() as session:
            delivery = Delivery(
                dedupe_key="daily:interrupted",
                total_count=2,
                messages=["already posted", "missing post"],
                sent_count=1,
                state="uncertain",
                error="Outcome unknown",
            )
            session.add(delivery)
            session.flush()
            session.add(TelegramDelivery(delivery_id=delivery.id, target=CHANNEL, message_ids=[31]))
            session.commit()
            id = delivery.id
        assert (
            client.post(f"/telegram/deliveries/{id}/retry", json={"confirmed_unsent": False}).status_code
            == 422
        )
        retried = client.post(f"/telegram/deliveries/{id}/retry", json={"confirmed_unsent": True})
        assert retried.status_code == 200 and retried.json()["state"] == "sent"
        assert [body["text"] for method, body in calls if method == "sendMessage"] == ["missing post"]
        client.post(f"/telegram/deliveries/{id}/retry", json={"confirmed_unsent": True}).raise_for_status()
        assert len([1 for method, _ in calls if method == "sendMessage"]) == 1


def test_generated_tldr_is_not_labeled_as_source_excerpt():
    item = Article(
        source_id="vendor",
        source_name="Vendor",
        title="Release",
        url="https://example.com/release",
        text="Original source text",
        summary={"tldr": "Generated TLDR", "why_it_matters": "Useful update"},
        summary_state="ready",
    )
    text = "\n".join(message["text"] for message in format_articles([item], "Daily digest"))
    assert "Generated TLDR" in text and "Preview: source excerpt" not in text


def test_runner_credential_is_scoped_and_never_returned(application):
    with application.state.sessions() as session:
        runner = session.get(WorkerState, "service:runner_key").value
    with TestClient(application, headers={"X-NewsFeeder-Runner-Token": runner}) as client:
        status = client.get("/status")
        assert status.status_code == 200 and runner not in status.text
        assert client.get("/articles").status_code == 401
        assert client.put("/preferences", json={"telegram_enabled": False}).status_code == 401
        assert client.post("/telegram/daily", json={"scheduled": False}).status_code == 403
        scheduled = client.post("/telegram/daily", json={"scheduled": True})
        assert scheduled.status_code == 200 and scheduled.json()["state"] == "skipped"


@pytest.mark.asyncio
async def test_channel_discovery_survives_private_chat_polling(application, monkeypatch):
    updates = [{"update_id": 21, "my_chat_member": {"chat": {"id": int(CHANNEL), "type": "channel", "title": "News"}}}]
    settings = application.state.settings.model_copy(update={
        "telegram_bot_token": TOKEN, "telegram_chat_id": "223344556", "telegram_target_type": "private",
    })
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            assert "my_chat_member" in requests[-1]["allowed_updates"]
            assert "channel_post" in requests[-1]["allowed_updates"]
        return httpx.Response(200, json={"ok": True, "result": updates if len(requests) == 1 else []})

    original = httpx.AsyncClient
    monkeypatch.setattr("app.telegram.httpx.AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(respond), **kwargs))
    with application.state.sessions() as session:
        await poll_commands(session, settings)
    with application.state.sessions() as session:
        async with httpx.AsyncClient() as client:
            discovered = await recent_targets(settings, session, client)
        assert discovered == [{"id": CHANNEL, "title": "News", "type": "channel"}]
        assert session.get(WorkerState, "telegram_offset:" + hashlib.sha256(TOKEN.encode()).hexdigest()[:16]).value == "22"


def test_discovered_destinations_are_scoped_to_the_bot_and_removed_when_it_leaves(application):
    event = {"channel_post": {"chat": {"id": int(CHANNEL), "title": "News", "type": "channel"}, "text": "Private message content"}}
    with application.state.sessions() as session:
        assert remember_targets(session, TOKEN, [event]) == [{"id": CHANNEL, "title": "News", "type": "channel"}]
        assert remember_targets(session, "other-bot", []) == []
        assert not any("Private message content" in record.value for record in session.scalars(select(WorkerState)))
        assert remember_targets(session, TOKEN, [{"my_chat_member": {"chat": event["channel_post"]["chat"], "new_chat_member": {"status": "left"}}}]) == []


def set_clock(monkeypatch, value):
    clock = [value]
    for module in ["app.service", "app.digests", "app.telegram", "app.telegram_config"]:
        monkeypatch.setattr(module + ".now", lambda: clock[0])
    return clock


def enable(session, **changes):
    prefs = Preferences(telegram_enabled=True, **changes)
    session.get(Preference, "owner").value = prefs.model_dump(exclude={"profile_hash"})
    session.commit()
    return prefs


def article(session, name, discovered):
    item = Article(
        source_id="vendor",
        source_name="Vendor",
        title=name,
        url="https://example.com/" + name,
        text="Actual source text for " + name,
        discovered_at=discovered,
        published_at=discovered - timedelta(days=3),
        summary_state="pending",
    )
    session.add(item)
    session.commit()
    return item.id


@pytest.mark.asyncio
async def test_collect_first_includes_late_discoveries_and_publishes_once_per_day(
    application, telegram_api, monkeypatch
):
    calls, _ = telegram_api
    clock = set_clock(monkeypatch, datetime(2026, 10, 29, 12, 4, tzinfo=UTC))  # 08:04 Toronto
    settings = application.state.settings.model_copy(
        update={"telegram_bot_token": TOKEN, "telegram_chat_id": CHANNEL, "telegram_target_type": "channel"}
    )
    with application.state.sessions() as session:
        enable(session)
        early = article(session, "early", clock[0] - timedelta(hours=1))
        build_digest(
            session, clock[0] - timedelta(minutes=30)
        )  # Manual digest must not shorten the daily window.

    async def collect_first(self):
        with self.sessions() as session:
            if session.scalar(select(Article.id).where(Article.title == "late")) is None:
                article(session, "late", clock[0] - timedelta(minutes=1))

    monkeypatch.setattr(Service, "refresh", collect_first)
    service = Service(settings, application.state.sessions)
    result = await service.publish_daily()
    assert result["state"] == "sent"
    assert len([c for c in calls if c[0] == "sendMessage"]) == 1
    posted = next(body for method, body in calls if method == "sendMessage")
    assert "early" in posted["text"] and "late" in posted["text"]
    assert "https://example.com/late" in [entity.get("url") for entity in posted["entities"]]
    with application.state.sessions() as session:
        daily = session.scalar(select(DailyDigest))
        from app.models import Digest

        assert early in session.get(Digest, daily.digest_id).article_ids
        first_digest = daily.digest_id
    clock[0] += timedelta(hours=2)
    await Service(settings, application.state.sessions).publish_daily()  # Worker restart.
    with application.state.sessions() as session:
        enable(session, delivery_hour=9)
        assert prepare_daily_digest(session).id == first_digest
    await service.publish_daily()  # Changing the hour cannot resend today's edition.
    assert len([c for c in calls if c[0] == "sendMessage"]) == 1
    clock[0] += timedelta(days=1)
    with application.state.sessions() as session:
        article(session, "tomorrow", clock[0] - timedelta(minutes=1))
    await service.publish_daily()
    posts = [body["text"] for method, body in calls if method == "sendMessage"]
    assert len(posts) == 2 and "tomorrow" in posts[-1] and "early" not in posts[-1]


def test_local_due_time_and_dst_duplicate_key():
    prefs = Preferences(delivery_hour=1, timezone="America/Toronto")
    assert not daily_due(datetime(2026, 11, 1, 4, 59, tzinfo=UTC), prefs)
    first = datetime(2026, 11, 1, 5, 30, tzinfo=UTC)
    repeated = datetime(2026, 11, 1, 6, 30, tzinfo=UTC)
    assert daily_due(first, prefs) and daily_due(repeated, prefs)
    assert daily_delivery_key(first, prefs, CHANNEL) == daily_delivery_key(repeated, prefs, CHANNEL)
    assert daily_delivery_key(first, prefs, CHANNEL) != daily_delivery_key(first, prefs, "-100other")


@pytest.mark.asyncio
async def test_rate_limited_partial_delivery_resumes_only_unsent_posts(application, monkeypatch):
    clock = set_clock(monkeypatch, datetime(2026, 10, 29, 12, tzinfo=UTC))
    settings = Settings(telegram_bot_token=TOKEN, telegram_chat_id=CHANNEL)
    posted = []

    async def no_wait(*args):
        pass

    monkeypatch.setattr("app.telegram.asyncio.sleep", no_wait)

    def respond(request):
        posted.append(json.loads(request.content)["text"])
        if len(posted) == 2:
            return httpx.Response(429, json={"ok": False, "parameters": {"retry_after": 60}})
        return httpx.Response(200, json={"ok": True, "result": {"message_id": len(posted)}})

    with application.state.sessions() as session:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            first = await deliver(session, settings, ["one", "two"], "daily:retry", client)
            assert first.state == "failed" and first.sent_count == 1
            await deliver(session, settings, ["replacement"], "daily:retry", client)
            assert posted == ["one", "two"]
            clock[0] += timedelta(seconds=61)
            final = await deliver(session, settings, ["replacement"], "daily:retry", client)
            assert final.state == "sent" and final.sent_count == 2
            assert session.get(TelegramDelivery, final.id).message_ids == [1, 3]
    assert posted == ["one", "two", "two"]


@pytest.mark.asyncio
async def test_stale_session_cannot_double_send_a_claimed_delivery(application):
    settings = Settings(telegram_bot_token=TOKEN, telegram_chat_id=CHANNEL)
    with application.state.sessions() as setup:
        row = Delivery(dedupe_key="daily:race", total_count=1, messages=["Only once"])
        setup.add(row)
        setup.flush()
        setup.add(TelegramDelivery(delivery_id=row.id, target=CHANNEL))
        setup.commit()
        id = row.id
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def respond(request):
        calls.append(request)
        entered.set()
        await release.wait()
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    with application.state.sessions() as first, application.state.sessions() as stale:
        stale_row = stale.get(Delivery, id)  # Cached pending state before the other worker claims it.
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            owner = asyncio.create_task(deliver(first, settings, ["Only once"], "daily:race", client))
            await entered.wait()
            assert stale_row.state == "pending"
            contender = await deliver(stale, settings, ["Only once"], "daily:race", client)
            assert contender.state == "sending" and len(calls) == 1
            release.set()
            assert (await owner).state == "sent"


@pytest.mark.asyncio
async def test_failed_connect_is_retryable_but_unknown_response_is_not(application):
    settings = Settings(telegram_bot_token=TOKEN, telegram_chat_id=CHANNEL)

    def offline(request):
        raise httpx.ConnectError("private URL should not escape", request=request)

    def invalid(request):
        return httpx.Response(200, json=["invalid"])

    with application.state.sessions() as session:
        async with httpx.AsyncClient(transport=httpx.MockTransport(offline)) as client:
            failed = await deliver(session, settings, ["story"], "daily:offline", client)
            assert failed.state == "failed" and session.get(TelegramDelivery, failed.id).retry_at
        async with httpx.AsyncClient(transport=httpx.MockTransport(invalid)) as client:
            uncertain = await deliver(session, settings, ["story"], "daily:invalid", client)
            assert uncertain.state == "uncertain" and TOKEN not in uncertain.error


@pytest.mark.asyncio
async def test_disabled_background_run_exits_without_starting_a_service(application, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", application.state.settings.database_url)
    result = await run_scheduled(port=18549)
    assert result["state"] == "skipped"


@pytest.mark.asyncio
async def test_background_runner_refuses_a_different_local_archive(monkeypatch):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"instance_id": "other-archive"})

    original = httpx.AsyncClient
    monkeypatch.setattr(
        "app.daily_runner.httpx.AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(respond), **kwargs),
    )
    with pytest.raises(RuntimeError, match="Another service"):
        await request_running_service("http://127.0.0.1:8000", "our-archive", "private-runner-key")
    assert len(requests) == 1 and requests[0].method == "GET"
