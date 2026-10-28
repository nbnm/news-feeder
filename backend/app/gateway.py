"""Gateway catalog and durable owner configuration. Never return credentials."""

import json

import httpx
from pydantic import BaseModel, Field, SecretStr, field_validator

from .config import Settings
from .models import WorkerState

CONFIG_KEY = "llm:configuration"


class GatewayError(ValueError):
    def __init__(self, message: str, status: int = 502):
        super().__init__(message)
        self.status = status


class GatewayUpdate(BaseModel):
    model: str | None = Field(default=None, min_length=1, max_length=200)
    api_key: SecretStr | None = Field(default=None, min_length=1, max_length=4096)

    @field_validator("model")
    @classmethod
    def clean_model(cls, value):
        if value is not None and not value.strip():
            raise ValueError("Choose a model")
        return value.strip() if value else value

    @field_validator("api_key")
    @classmethod
    def clean_key(cls, value):
        if value is not None:
            key = value.get_secret_value().strip()
            if not key or any(char.isspace() for char in key):
                raise ValueError("Enter a valid virtual key")
            return SecretStr(key)
        return value


def overrides(session) -> dict:
    record = session.get(WorkerState, CONFIG_KEY)
    return json.loads(record.value) if record else {}


def effective_settings(settings: Settings, session) -> Settings:
    value = overrides(session)
    # Scope saved choices to the gateway that issued the key. Changing the server's
    # base URL must never send a saved virtual key to a different gateway.
    if value.get("base_url") != settings.litellm_base_url.rstrip("/"):
        return settings
    changes = {"litellm_" + key: value[key] for key in ("api_key", "model") if key in value}
    return settings.model_copy(update=changes)


def public_configuration(settings: Settings) -> dict:
    base = settings.litellm_base_url.rstrip("/")
    return {
        "base_url": base,
        "models_url": base + "/models",
        "completions_url": base + "/chat/completions",
        "model": settings.litellm_model,
        "api_key_configured": bool(settings.litellm_api_key),
        "configured": settings.llm_ready,
    }


async def available_models(settings: Settings, client: httpx.AsyncClient) -> list[dict]:
    if not settings.litellm_api_key:
        raise GatewayError("Enter your T1A gateway virtual key to load models.", 409)
    try:
        response = await client.get(
            settings.litellm_base_url.rstrip("/") + "/models",
            headers={"Authorization": f"Bearer {settings.litellm_api_key}"},
            timeout=20,
        )
    except httpx.HTTPError as error:
        raise GatewayError("Could not reach the gateway. Try loading models again.") from error
    if response.status_code in {401, 403}:
        raise GatewayError("The gateway rejected the virtual key. Check it in Settings → Models.", 422)
    if response.status_code != 200:
        raise GatewayError(f"Gateway model catalog returned HTTP {response.status_code}.")
    try:
        records = response.json()["data"]
        if not isinstance(records, list):
            raise ValueError("Expected model list")
        found = {}
        for item in records:
            if isinstance(item, dict) and isinstance(item.get("id"), str) and item["id"].strip():
                model_id = item["id"].strip()
                found[model_id] = {"id": model_id}
        return [found[key] for key in sorted(found, key=str.casefold)]
    except (ValueError, KeyError, TypeError) as error:
        raise GatewayError("The gateway returned an incompatible model catalog.") from error


async def update_configuration(settings: Settings, session, value: GatewayUpdate, client) -> dict:
    saved = overrides(session)
    base = settings.litellm_base_url.rstrip("/")
    if saved.get("base_url") != base:
        saved = {"base_url": base}
    proposed = effective_settings(settings, session)
    changes = {}
    if value.api_key is not None:
        changes["litellm_api_key"] = value.api_key.get_secret_value()
    if value.model is not None:
        changes["litellm_model"] = value.model
    proposed = proposed.model_copy(update=changes)
    if changes:
        models = await available_models(proposed, client)
        available = {item["id"] for item in models}
        if value.model is not None and value.model not in available:
            raise GatewayError(
                "That model is no longer available. Reload the model list and choose again.", 422
            )
        if value.model is None and proposed.litellm_model and proposed.litellm_model not in available:
            changes["litellm_model"] = ""
            proposed = proposed.model_copy(update={"litellm_model": ""})
        for key in ("api_key", "model"):
            if "litellm_" + key in changes:
                saved[key] = changes["litellm_" + key]
        session.merge(WorkerState(key=CONFIG_KEY, value=json.dumps(saved)))
        session.commit()
    return public_configuration(proposed)
