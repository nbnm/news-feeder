import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.gateway import GatewayError, available_models, effective_settings
from app.main import create_app
from app.service import Service

BASE = "https://secondstack.t1a.io/gateway/api/v1"
KEY = "test-virtual-key"
AUTH = {"Authorization": "Bearer owner-key"}


@pytest.fixture
def application(tmp_path):
    return create_app(
        Settings(
            database_url=f"sqlite:///{tmp_path}/gateway.db",
            demo_mode=True,
            worker_enabled=False,
            app_api_key="owner-key",
            litellm_base_url=BASE,
            litellm_api_key="",
            litellm_model="",
        )
    )


@pytest.fixture
def client(application):
    with TestClient(application, headers=AUTH) as value:
        yield value


@pytest.fixture
def gateway(monkeypatch):
    requests = []
    original = httpx.AsyncClient

    def respond(request):
        requests.append(request)
        assert str(request.url) == BASE + "/models"
        if request.headers["Authorization"] != "Bearer " + KEY:
            return httpx.Response(401, json={"error": "Invalid key: " + request.headers["Authorization"]})
        return httpx.Response(
            200,
            json={"data": [{"id": "t1a-fast"}, {"id": "t1a-deep"}, {"id": "t1a-fast"}, {"not_id": "bad"}]},
        )

    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(respond), **kwargs)
    )
    return requests


def test_gateway_defaults_and_missing_key(client):
    config = client.get("/llm/config").json()
    assert config["base_url"] == BASE
    assert config["models_url"] == BASE + "/models"
    assert config["completions_url"] == BASE + "/chat/completions"
    assert not config["api_key_configured"]
    assert not config["configured"]
    assert client.get("/llm/models").status_code == 409


def test_gateway_requires_owner_auth(client):
    wrong = {"Authorization": "Bearer wrong"}
    assert client.get("/llm/config", headers=wrong).status_code == 401
    assert client.get("/llm/models", headers=wrong).status_code == 401
    assert client.put("/llm/config", headers=wrong, json={"api_key": KEY}).status_code == 401


def test_catalog_selection_persists_and_redacts_key(client, application, gateway):
    saved = client.put("/llm/config", json={"api_key": KEY})
    assert saved.status_code == 200
    assert saved.json()["api_key_configured"]
    assert not saved.json()["configured"]
    catalog = client.get("/llm/models").json()
    assert [item["id"] for item in catalog["models"]] == ["t1a-deep", "t1a-fast"]
    assert client.put("/llm/config", json={"model": "t1a-fast"}).status_code == 200
    assert client.get("/status").json()["llm_configured"]
    for path in ("/llm/config", "/llm/models", "/status", "/preferences", "/articles"):
        assert KEY not in client.get(path).text

    # Re-create the service over the same database, as a backend restart would.
    restarted = create_app(application.state.settings)
    with TestClient(restarted, headers=AUTH) as other:
        assert other.get("/llm/config").json()["model"] == "t1a-fast"
        assert other.get("/llm/config").json()["api_key_configured"]
    assert all(request.headers["Authorization"] == "Bearer " + KEY for request in gateway)


def test_bad_model_or_key_preserves_previous_configuration(client, gateway):
    assert client.put("/llm/config", json={"api_key": KEY, "model": "t1a-fast"}).status_code == 200
    assert client.put("/llm/config", json={"model": "invented-model"}).status_code == 422
    rejected = client.put("/llm/config", json={"api_key": "rejected-secret"})
    assert rejected.status_code == 422
    assert "rejected-secret" not in rejected.text
    assert client.get("/llm/config").json()["model"] == "t1a-fast"
    assert client.get("/llm/models").status_code == 200


def test_invalid_key_is_not_echoed_in_validation_response(client):
    key = "secret with whitespace"
    response = client.put("/llm/config", json={"api_key": key})
    assert response.status_code == 422
    assert key not in response.text
    assert "input" not in response.text


def test_saved_key_is_scoped_to_gateway(client, application, gateway):
    client.put("/llm/config", json={"api_key": KEY, "model": "t1a-fast"})
    changed = application.state.settings.model_copy(update={"litellm_base_url": "https://other.example/v1"})
    with application.state.sessions() as session:
        current = effective_settings(changed, session)
    assert current.litellm_api_key == ""
    assert current.litellm_model == ""


async def test_worker_uses_saved_model_and_virtual_key(application, gateway, monkeypatch):
    # Configure through the same API the Mac uses.
    with TestClient(application, headers=AUTH) as client:
        assert client.put("/llm/config", json={"api_key": KEY, "model": "t1a-deep"}).status_code == 200
    captured = []

    async def capture(session, settings, force):
        captured.append(settings)
        return {}

    monkeypatch.setattr("app.service.refresh", capture)
    await Service(application.state.settings, application.state.sessions).refresh()
    assert captured[0].litellm_model == "t1a-deep"
    assert captured[0].litellm_api_key == KEY
    assert captured[0].litellm_base_url == BASE


async def test_malformed_catalog_is_visible_without_raw_gateway_content():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, text=json.dumps({"data": "bad"})))
    ) as client:
        with pytest.raises(GatewayError, match="incompatible model catalog"):
            await available_models(Settings(litellm_api_key=KEY), client)
