import asyncio
from datetime import timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.categories import upgrade_databricks_organizations
from app.collectors import collect
from app.config import Settings
from app.github_org import parse_organization_events
from app.main import create_app
from app.models import Article, ArticleSource, Category, Source, SourceCategory, WorkerState, now
from app.network import Fetcher
from app.pipeline import article_content
from app.schemas import SourceInput


def event(kind, repo="org/project", **payload):
    return {
        "type": kind,
        "public": True,
        "created_at": now().isoformat(),
        "repo": {"name": repo},
        "payload": payload,
    }


def test_events_include_release_and_public_repository_but_not_branch_noise():
    items = parse_organization_events(
        [
            event(
                "ReleaseEvent",
                action="published",
                release={
                    "name": "Major update",
                    "tag_name": "v2.0.0",
                    "body": "Actual release notes",
                    "html_url": "https://github.com/org/project/releases/tag/v2.0.0",
                    "published_at": now().isoformat(),
                },
            ),
            event("CreateEvent", ref_type="repository", description="A data tool"),
            event("PublicEvent", repo="org/new-public"),
            event("CreateEvent", ref_type="branch", ref="feature/new"),
            event("CreateEvent", ref_type="tag", ref="v1"),
            event("PushEvent", ref="refs/heads/main"),
            event("IssuesEvent", action="opened"),
            event("CreateEvent", repo="other/project", ref_type="repository"),
            {**event("CreateEvent", ref_type="repository"), "public": False},
            event(
                "ReleaseEvent",
                action="published",
                release={
                    "tag_name": "draft",
                    "draft": True,
                    "html_url": "https://github.com/org/project/releases/tag/draft",
                },
            ),
            event(
                "ReleaseEvent",
                action="published",
                release={
                    "tag_name": "spoof",
                    "html_url": "https://github.com/other/project/releases/tag/v1",
                },
            ),
        ],
        "org",
    )
    assert len(items) == 3
    assert items[0].title == "org/project: Major update"
    assert items[0].excerpt == "Actual release notes" and items[0].content_state == "full"
    assert items[1].repository_intro and items[2].repository_intro
    assert "A data tool" in items[1].excerpt


@pytest.mark.asyncio
async def test_all_event_pages_inventory_pagination_dedup_and_date_order():
    calls = []
    recent = now().isoformat()
    old = (now() - timedelta(days=40)).isoformat()

    def handler(request):
        calls.append(str(request.url))
        page = request.url.params["page"]
        if request.url.path.endswith("/events"):
            if page in {"1", "2"}:
                return httpx.Response(200, json=[event("WatchEvent")] * 100)
            return httpx.Response(200, json=[event("CreateEvent", ref_type="repository", description="Tool")])
        if page == "1":
            return httpx.Response(
                200,
                json=[
                    {
                        "full_name": "org/project",
                        "created_at": recent,
                        "description": "Tool",
                        "fork": False,
                    }
                ]
                * 100,
            )
        return httpx.Response(
            200,
            json=[
                {
                    "full_name": "org/second",
                    "created_at": recent,
                    "description": "Another tool",
                    "fork": False,
                },
                {"full_name": "org/fork", "created_at": recent, "fork": True},
                {"full_name": "org/old", "created_at": old, "fork": False},
                {"full_name": "other/project", "created_at": recent, "fork": False},
            ],
        )

    source = Source(name="org", kind="github_org", url="https://github.com/org")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        items = await collect(source, Fetcher(client))
    assert len(calls) == 5
    assert {item.url for item in items} == {"https://github.com/org/project", "https://github.com/org/second"}
    assert all(item.repository_intro for item in items)
    assert all(item.published_at for item in items)


@pytest.mark.asyncio
async def test_failed_organization_request_does_not_return_a_partial_success():
    def handler(request):
        if request.url.path.endswith("/events"):
            return httpx.Response(200, json=[event("CreateEvent", ref_type="repository")])
        return httpx.Response(403, json={"message": "rate limited"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await collect(
                Source(name="org", kind="github_org", url="https://github.com/org"), Fetcher(client)
            )


@pytest.mark.asyncio
async def test_new_repository_archives_readme_without_github_navigation():
    def handler(request):
        return httpx.Response(
            200,
            text="""<nav>GitHub navigation</nav>
            <article class="markdown-body"><h1>Project</h1><p>Actual documentation and capabilities.</p></article>""",
        )

    candidate = parse_organization_events(
        [event("CreateEvent", ref_type="repository", description="A data tool")], "org"
    )[0]
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        _, text, state, _, error = await article_content(candidate, Fetcher(client), asyncio.Semaphore(1))
    assert "Actual documentation and capabilities." in text
    assert "GitHub navigation" not in text
    assert state == "full" and error is None


@pytest.mark.parametrize(
    "url",
    [
        "http://github.com/org",
        "https://github.com/org/repo",
        "https://example.com/org",
        "https://github.com/org?tab=repositories",
        "https://github.com/org#repos",
    ],
)
def test_source_requires_organization_level_url(url):
    with pytest.raises(ValueError):
        SourceInput(name="org", kind="github_org", url=url)


def test_upgrade_collapses_repository_sources_and_preserves_archive_and_other_categories(tmp_path):
    app = create_app(Settings(database_url=f"sqlite:///{tmp_path}/upgrade.db", worker_enabled=False))
    with app.state.sessions() as session:
        category = session.scalar(select(Category).where(Category.name == "Databricks"))
        category.name = "Data platforms"
        old = Source(
            name="Databricks CLI", kind="github", url="https://github.com/databricks/cli", enabled=True
        )
        session.add(old)
        session.flush()
        session.add(SourceCategory(source_id=old.id, category_id=category.id))
        article = Article(
            source_id=old.id,
            source_name=old.name,
            title="CLI release",
            text="Archived notes",
            url="https://github.com/databricks/cli/releases/tag/v1.0.0",
            summary_state="pending",
        )
        shared = Article(
            source_id="community",
            source_name="Community",
            title="Shared release",
            text="Shared notes",
            url="https://github.com/databricks/cli/releases/tag/v2.0.0",
            summary_state="pending",
        )
        session.add_all([article, shared])
        session.flush()
        article_id, shared_id = article.id, shared.id
        session.add(ArticleSource(article_id=shared.id, source_id=old.id))
        session.delete(session.get(WorkerState, "databricks:organizations:v1"))
        session.commit()
        upgrade_databricks_organizations(session, False)
    with TestClient(app) as client:
        sources = client.get("/sources").json()
        databricks = [source for source in sources if source["category_id"] == category.id]
        assert len(databricks) == 5 and all(source["kind"] != "github" for source in databricks)
        organizations = {source["url"] for source in databricks if source["kind"] == "github_org"}
        assert organizations == {
            f"https://github.com/{org}"
            for org in [
                "databricks-solutions",
                "databricks-industry-solutions",
                "databrickslabs",
                "databricks",
            ]
        }
        parent = next(source for source in databricks if source["url"] == "https://github.com/databricks")
        archived = {item["id"]: item for item in client.get("/articles").json()}
        assert archived[article_id]["text"] == "Archived notes"
        assert parent["id"] in archived[article_id]["source_ids"]
        assert parent["id"] in archived[shared_id]["source_ids"]
        assert any(source["name"] == "MLflow releases" and source["kind"] == "github" for source in sources)
        client.delete(f"/sources/{parent['id']}").raise_for_status()
        with app.state.sessions() as session:
            upgrade_databricks_organizations(session, False)
        assert len(client.get("/sources").json()) == len(sources) - 1


def test_organization_source_crud_is_configurable(tmp_path):
    app = create_app(Settings(database_url=f"sqlite:///{tmp_path}/crud.db", worker_enabled=False))
    with TestClient(app) as client:
        source = client.post(
            "/sources",
            json={"name": "Another org", "kind": "github_org", "url": "https://github.com/example/"},
        ).json()
        assert source["url"] == "https://github.com/example"
        source["enabled"] = False
        assert client.put(f"/sources/{source['id']}", json=source).json()["enabled"] is False
