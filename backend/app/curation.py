"""Source-based news signals, shared with the offline reader through article sync."""

import re
from urllib.parse import unquote, urlsplit

DATA_ENGINEERING = re.compile(
    r"\b(data engineering|data pipelines?|etl|elt|lakehouse|data warehouses?|data lakes?|"
    r"apache spark|pyspark|lakeflow|lakebase|unity catalog|databricks sql|delta lake|iceberg|"
    r"dbt|airflow|dagster|prefect|flink|"
    r"kafka|data ingestion|data quality|data governance|change data capture|cdc|parquet|"
    r"data catalogs?|stream processing|data orchestration)\b",
    re.I,
)
AGENTIC_AI = re.compile(
    r"\b(agents?|agentic|multi[- ]agent|langchain|langgraph|autogen|crewai|tool calling|"
    r"function calling|mcp|model context protocol|computer use|coding assistants?|"
    r"autonomous workflows?|retrieval[- ]augmented|rag|claude code|codex)\b",
    re.I,
)
IMPORTANT_FIX = re.compile(
    r"\b(breaking changes?|backward[s]? incompatible|security (fix|patch|vulnerabilit(?:y|ies))|"
    r"critical (fix|patch|bug|vulnerabilit(?:y|ies))|cve-\d{4}-\d+|remote code execution|"
    r"data (loss|corruption)|deadlock|severe regression|privilege escalation)\b",
    re.I,
)
ROUTINE = re.compile(
    r"\b(bump|deps?|dependenc\w*|docs?|documentation|typos?|tests?|ci|chore|lint|"
    r"formatting|readme|reformat|rename|logging|logs?|examples?|contributors?|changelog|"
    r"error messages?|type hints?)\b",
    re.I,
)
FEATURE = re.compile(
    r"(?:^|\s)(?:feat(?:ure)?(?:\([^)]*\))?!?\s*:|"
    r"introduc\w*\b|launch\w*\b|new (?:feature|capability|api|integration|connector|engine)|"
    r"(?:add(?:ed|s)?|enable[ds]?|implement(?:ed|s)?)\b|(?:now )?supports? for\b|now supports?\b)",
    re.I,
)


def github_update(article):
    parsed = urlsplit(article.url)
    if (parsed.hostname or "").lower() not in {"github.com", "www.github.com"}:
        return "none"
    path = unquote(parsed.path).strip("/")
    if re.fullmatch(r"[^/]+/[^/]+", path):
        # Organization introductions and newly discovered Trending repositories.
        return "repository"
    tag = path.split("/releases/tag/", 1)[1] if "/releases/tag/" in "/" + path else ""
    if re.search(r"(?:^|[.\-_\d])(alpha|beta|rc\d*|dev|nightly|snapshot|preview)(?:\d|[.\-_]|$)", tag, re.I):
        return "routine"
    if re.fullmatch(r"v?[1-9]\d*\.0(?:\.0)?(?:\+[^/]+)?", tag):
        return "major"
    # Judge release notes, not generic README text or promotional impact prose.
    summary = article.summary or {}
    notes = article.title + "\n" + article.text
    if not article.text.strip():
        notes += "\n" + summary.get("what_changed", "")
    important_section = False
    for raw_line in notes.splitlines():
        line = re.sub(r"https?://\S+", "", raw_line).strip(" #*-\t")
        if re.match(r"^\s*#{1,6}\s", raw_line):
            important_section = bool(IMPORTANT_FIX.search(line))
            continue
        if re.match(r"(?:none\b|n/a\b|no (?:breaking changes?|security fixes?)\b)", line, re.I):
            continue
        if IMPORTANT_FIX.search(line) and len(line.split()) >= 3:
            return "major"
        if important_section and not ROUTINE.search(line) and len(line.split()) >= 4:
            return "major"
        if not ROUTINE.search(line) and FEATURE.search(line) and len(line.split()) >= 5:
            return "major"
    # A version number, a Features heading, or a high-impact summary alone is not evidence.
    return "routine"


def curation_signals(article):
    summary = article.summary or {}
    description = " ".join(
        [
            summary.get("tldr", ""),
            summary.get("what_changed", ""),
            *summary.get("topics", []),
            *summary.get("key_points", []),
        ]
    )

    def affinity(pattern):
        return max(
            1.0 if pattern.search(article.title) else 0.0,
            0.75 if pattern.search(description) else 0.0,
            0.35 if pattern.search(article.text[:6000]) else 0.0,
        )

    return {
        "data_engineering": affinity(DATA_ENGINEERING),
        "agentic_ai": affinity(AGENTIC_AI),
        "github_update": github_update(article),
    }


def include_in_news(article, prefs):
    return not prefs.major_github_only or github_update(article) != "routine"


def focus_adjustment(signals, prefs):
    return (prefs.news_focus - 50) * 2 * (signals["agentic_ai"] - signals["data_engineering"])
