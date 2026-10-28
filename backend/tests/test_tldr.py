import hashlib
import json

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import Settings
from app.llm import evidence_passages, summarize, validate_citations
from app.main import create_app
from app.models import Article
from app.pipeline import process_summaries
from app.schemas import ArticleOutput, Summary
from app.tldr import clean_tldr, source_excerpt


def test_tldr_hides_reference_ids_and_retains_factual_numbers():
    assert clean_tldr("A release adds a capability (evidence: 1, 3-5).") == "A release adds a capability."
    assert clean_tldr("A release adds a capability (1,2,5).") == "A release adds a capability."
    assert (
        clean_tldr("Costs fell 20% ($1,200) between years (2025, 2026).")
        == "Costs fell 20% ($1,200) between years (2025, 2026)."
    )


def test_evidence_passages_preserve_exact_source_text():
    text = "A new release is now available.\n\nIt includes a capability for production workflows."
    evidence = evidence_passages(text)
    assert [item["id"] for item in evidence] == [1, 2]
    summary = Summary(
        tldr="A release adds a capability.",
        what_changed="A new release.",
        why_it_matters="A capability.",
        key_points=[],
        topics=[],
        impact="medium",
        citations=[{"quote": item["quote"]} for item in evidence],
    )
    validate_citations(summary, text)


async def test_unknown_evidence_id_is_rejected():
    payload = {
        "tldr": "A new release.",
        "what_changed": "A release.",
        "why_it_matters": "A capability.",
        "key_points": [],
        "topics": [],
        "impact": "unknown",
        "citation_ids": [999],
    }
    response = {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(payload)}}]}
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=response))
    ) as client:
        with pytest.raises(ValueError, match="unknown source passage"):
            await summarize(
                "Release",
                "A new release is now available.",
                Settings(litellm_api_key="test", litellm_model="test"),
                client,
            )


def test_previews_are_bounded_source_text_or_headlines():
    body = "A detailed article describing a new release with twelve or more words for readers. " * 20
    preview = source_excerpt("Short heading\n\n" + body)
    assert len(preview) <= 360
    assert body.startswith(preview.removesuffix("…"))
    article = Article(title="A real announcement", text=body, summary=None)
    assert article.tldr == source_excerpt(body)
    assert article.tldr_kind == "source_excerpt"
    article.text = ""
    assert article.tldr == article.title
    assert article.tldr_kind == "headline"


def test_archived_summary_without_tldr_still_decodes():
    summary = {
        "what_changed": "A new release is available.",
        "why_it_matters": "It adds a capability.",
        "key_points": [],
        "topics": [],
        "impact": "medium",
        "citations": [{"quote": "A new release is available."}],
    }
    output = ArticleOutput.model_validate(
        {
            "id": "one",
            "source_id": "source",
            "source_name": "Example",
            "title": "Release",
            "url": "https://example.com/release",
            "published_at": None,
            "discovered_at": "2026-10-01T12:00:00Z",
            "text": "A new release is available.",
            "content_state": "full",
            "extraction_error": None,
            "summary": summary,
            "summary_state": "ready",
            "summary_error": None,
            "buzz": None,
            "tldr": summary["what_changed"],
            "tldr_kind": "summary",
        }
    )
    assert output.summary.tldr == summary["what_changed"]


def test_live_sources_contain_no_demo_and_are_enabled(tmp_path):
    app = create_app(
        Settings(database_url=f"sqlite:///{tmp_path}/live.db", demo_mode=False, worker_enabled=False)
    )
    with TestClient(app) as client:
        sources = client.get("/sources").json()
        assert len(sources) == 12
        assert all(source["enabled"] for source in sources)
        assert not any("Demo" in source["name"] for source in sources)
        assert client.get("/articles").json() == []
        assert not client.get("/status").json()["demo"]


async def test_summary_batches_isolate_failures_and_populate_tldr(tmp_path, monkeypatch):
    app = create_app(Settings(database_url=f"sqlite:///{tmp_path}/summaries.db", worker_enabled=False))
    settings = Settings(litellm_api_key="test-key", litellm_model="test-model")

    async def generate(title, text, settings, client):
        if title == "Broken":
            raise ValueError("Incomplete response")
        return Summary(
            tldr="A release adds a new capability.",
            what_changed=text,
            why_it_matters="It adds a capability.",
            key_points=[],
            topics=[],
            impact="medium",
            citations=[{"quote": text}],
        )

    monkeypatch.setattr("app.pipeline.summarize", generate)
    with app.state.sessions() as session:
        for title in ("First", "Broken", "Third"):
            text = title + " has a new release."
            session.add(
                Article(
                    source_id="source",
                    source_name="Example",
                    title=title,
                    url="https://example.com/" + title,
                    text=text,
                    content_state="full",
                    content_hash=hashlib.sha256(text.encode()).hexdigest(),
                )
            )
        session.commit()
        async with httpx.AsyncClient() as client:
            assert await process_summaries(session, settings, client) == 2
        values = {a.title: a for a in session.scalars(select(Article))}
        assert values["Broken"].summary_state == "error"
        assert values["First"].tldr == "A release adds a new capability."
        assert values["Third"].tldr_kind == "summary"
