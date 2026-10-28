import hashlib
import json

JEV_MODEL = "typesafe/jev-1.13"
RUBRIC_VERSION = "relevance-v1"
DEFAULT_PROFILE = (
    "I follow AI industry updates, especially foundation models, developer APIs, "
    "LangChain / LangGraph agent frameworks, OpenAI and Anthropic releases, and "
    "Databricks data and AI platforms. Prioritize practical capabilities, model availability, "
    "pricing, API changes, and useful engineering releases. Also track Y Combinator startup trends."
)


def profile_fingerprint(profile: str) -> str:
    value = [profile.strip(), JEV_MODEL, RUBRIC_VERSION]
    return hashlib.sha256(json.dumps(value, ensure_ascii=False).encode()).hexdigest()
