import json

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.collectors import collect
from app.config import Settings
from app.main import create_app
from app.models import Article, Source, WorkerState
from app.pipeline import refresh
from app.schemas import SourceInput
from app.telegram import format_articles
from app.trending import TRENDING_URL, parse_trending, save_snapshot, source_output


def page(names=None, stars=1500):
    names = names or [f"org/repo{i}" for i in range(12)]
    return "".join(
        f'<article class="Box-row"><h2><a href="/{name}">{name}</a></h2>'
        f"<p>A useful agent toolkit for building enterprise workflows: {name}.</p>"
        '<span itemprop="programmingLanguage">Python</span>'
        f'<a href="/{name}/stargazers">{stars:,}</a><span>{rank:,} stars today</span></article>'
        for rank, name in enumerate(names)
    )


class ReadmeFetcher:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    async def get(self, url):
        self.calls.append(url)
        if self.fail:
            raise httpx.ConnectError("Unavailable")
        return httpx.Response(
            200,
            text='<article class="markdown-body"><h1>Toolkit</h1>'
            "<p>This project supports enterprise agents with a documented Python API.</p></article>",
        )


def test_daily_top_ten_keeps_github_order_and_exact_counts():
    items = parse_trending(page())
    assert len(items) == 10
    assert [item.title for item in items] == [f"org/repo{i}" for i in range(10)]
    assert items[0].repository == {
        "full_name": "org/repo0",
        "description": "A useful agent toolkit for building enterprise workflows: org/repo0.",
        "language": "Python",
        "stars_today": 0,
        "total_stars": 1500,
        "rank": 1,
    }
    assert items[-1].repository["rank"] == 10
    assert items[0].url == "https://github.com/org/repo0"
    no_language = page().replace('<span itemprop="programmingLanguage">Python</span>', "")
    assert parse_trending(no_language)[0].repository["language"] is None


@pytest.mark.parametrize(
    "html",
    [
        "<h1>Temporarily unavailable</h1>",
        page().replace('href="/org/repo0"', 'href="https://untrusted.example/repo"'),
        page().replace("0 stars today", "No daily count"),
        page().replace("1,500", "Unavailable"),
    ],
)
def test_changed_markup_fails_instead_of_fabricating_counts(html):
    with pytest.raises(ValueError, match="previous snapshot is retained"):
        parse_trending(html)


@pytest.mark.parametrize(
    "changes",
    [
        {"url": "https://github.com/trending?since=weekly"},
        {"url": "https://example.com/trending?since=daily"},
        {"url": "https://github.com/trending/python?since=daily"},
        {"topics": ["AI"]},
    ],
)
def test_source_is_daily_and_includes_all_languages_and_topics(changes):
    with pytest.raises(ValueError):
        SourceInput.model_validate(
            {"name": "Trending", "kind": "github_trending", "url": TRENDING_URL, **changes}
        )


async def test_collector_routes_to_daily_page():
    class PageFetcher:
        async def get(self, url):
            assert url == TRENDING_URL
            return httpx.Response(200, text=page())

    items = await collect(Source(kind="github_trending", url=TRENDING_URL), PageFetcher())
    assert len(items) == 10


async def test_snapshot_reorders_without_duplicate_articles_or_regenerating_content(tmp_path, monkeypatch):
    application = create_app(Settings(database_url=f"sqlite:///{tmp_path}/archive.db", worker_enabled=False))
    with application.state.sessions() as session:
        # The same repository can already have been discovered on Hacker News.
        existing = Article(
            source_id="hn",
            source_name="Hacker News",
            title="An agent toolkit",
            url="https://github.com/org/repo0",
            text="Stored README.",
            content_state="full",
            content_hash="unchanged",
            summary_state="ready",
            summary={
                "tldr": "An agent toolkit.",
                "what_changed": "An agent toolkit.",
                "why_it_matters": "Useful workflows.",
                "key_points": [],
                "topics": [],
                "impact": "low",
                "citations": [{"quote": "Stored README."}],
            },
        )
        session.add(existing)
        session.add_all([Source(name="Daily", kind="github_trending", url=TRENDING_URL)])
        session.commit()
        source = session.scalar(select(Source).where(Source.name == "Daily"))
        fetcher = ReadmeFetcher()
        assert await save_snapshot(session, source, parse_trending(page()), fetcher) == 9
        assert len(fetcher.calls) == 9
        first_ids = source_output(session, source).trending.article_ids
        assert first_ids[0] == existing.id
        assert len(first_ids) == 10

        names = [f"org/repo{i}" for i in reversed(range(1, 11))]
        assert await save_snapshot(session, source, parse_trending(page(names, stars=2500)), fetcher) == 1
        assert len(fetcher.calls) == 10
        session.expire_all()
        reordered = source_output(session, source).trending.article_ids
        assert reordered[-1] == first_ids[1]
        assert existing.id not in reordered
        assert session.get(Article, existing.id).summary_state == "ready"
        assert session.get(Article, existing.id).content_hash == "unchanged"
        assert session.get(Article, existing.id).text == "Stored README."
        assert len(list(session.scalars(select(Article)))) == 11
        current = session.get(Article, reordered[0])
        assert current.repository["rank"] == 1
        assert current.repository["total_stars"] == 2500

        # A failed source request preserves the last successful membership.
        for other in session.scalars(select(Source)):
            other.enabled = other.id == source.id
        session.commit()

        async def broken(*_):
            return parse_trending("<h1>Rate limited</h1>")

        async def no_summaries(*_):
            return 0

        monkeypatch.setattr("app.pipeline.collect", broken)
        monkeypatch.setattr("app.pipeline.process_summaries", no_summaries)
        await refresh(session, Settings(), force=True)
        assert source_output(session, source).trending.article_ids == reordered
        assert "previous snapshot is retained" in source.last_error

    with TestClient(application) as client:
        saved = next(item for item in client.get("/sources").json() if item["name"] == "Daily")
        assert saved["trending"]["article_ids"] == reordered
        returned = {item["id"]: item for item in client.get("/articles").json()}
        assert returned[reordered[0]]["repository"]["total_stars"] == 2500


async def test_readme_failure_keeps_description_and_telegram_evidence(tmp_path):
    application = create_app(Settings(database_url=f"sqlite:///{tmp_path}/archive.db", worker_enabled=False))
    with application.state.sessions() as session:
        source = Source(name="Daily", kind="github_trending", url=TRENDING_URL)
        session.add(source)
        session.commit()
        await save_snapshot(session, source, parse_trending(page()), ReadmeFetcher(fail=True))
        session.expire_all()
        snapshot = json.loads(session.get(WorkerState, "github_trending:" + source.id).value)
        article = session.get(Article, snapshot["article_ids"][0])
        assert article.content_state == "excerpt"
        assert "enterprise workflows" in article.text
        assert "README could not be fetched" in article.extraction_error
        messages = format_articles([article], "Daily repos")
        text = "\n".join(message["text"] for message in messages)
        assert "GitHub daily rank: #1" in text
        assert "Stars gained today: 0" in text
        assert "Total stars: 1,500" in text
        assert TRENDING_URL in [entity.get("url") for message in messages for entity in message["entities"]]
