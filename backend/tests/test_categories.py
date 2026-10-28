import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.collectors import Candidate, collect
from app.config import Settings
from app.main import create_app
from app.models import Article, Source, SourceCollectionOptions
from app.network import Fetcher
from app.pipeline import refresh
from app.seed import seed


@pytest.fixture
def application(tmp_path):
    return create_app(Settings(database_url=f"sqlite:///{tmp_path}/categories.db", worker_enabled=False))


def test_category_crud_source_assignment_and_old_client_compatibility(application):
    with TestClient(application) as client:
        category = client.post("/categories", json={"name": " Custom AI "}).json()
        assert category["name"] == "Custom AI"
        source = client.post(
            "/sources",
            json={
                "name": "Releases",
                "kind": "github",
                "url": "https://github.com/org/repo",
                "category_id": category["id"],
                "release_tag_pattern": r"^v\d+",
            },
        ).json()
        assert source["category_id"] == category["id"]
        legacy = {
            key: value for key, value in source.items() if key not in {"category_id", "release_tag_pattern"}
        }
        legacy["enabled"] = False
        updated = client.put(f"/sources/{source['id']}", json=legacy).json()
        assert updated["category_id"] == category["id"]
        assert updated["release_tag_pattern"] == r"^v\d+"
        renamed = client.put(f"/categories/{category['id']}", json={"name": "My AI"}).json()
        assert renamed["name"] == "My AI"
        source["category_id"] = None
        assert client.put(f"/sources/{source['id']}", json=source).json()["category_id"] is None
        source["category_id"] = category["id"]
        client.put(f"/sources/{source['id']}", json=source).raise_for_status()
        original_articles = client.get("/articles").json()
        assert client.delete(f"/categories/{category['id']}").status_code == 200
        remaining = next(item for item in client.get("/sources").json() if item["id"] == source["id"])
        assert remaining["category_id"] is None
        assert client.get("/articles").json() == original_articles
        source["category_id"] = "missing"
        assert client.put(f"/sources/{source['id']}", json=source).status_code == 422
        assert client.post("/categories", json={"name": " "}).status_code == 422
        source["category_id"] = None
        source["release_tag_pattern"] = "["
        assert client.put(f"/sources/{source['id']}", json=source).status_code == 422


def test_starter_categories_are_separate_and_restart_respects_edits(application):
    with TestClient(application) as client:
        categories = {item["name"]: item["id"] for item in client.get("/categories").json()}
        sources = client.get("/sources").json()
        databricks = [item for item in sources if item["category_id"] == categories["Databricks"]]
        mlflow = [item for item in sources if item["category_id"] == categories["MLflow"]]
        assert len(databricks) == 5
        assert len([item for item in databricks if item["kind"] == "github_org"]) == 4
        assert len(mlflow) == 2
        assert all(item["enabled"] for item in databricks + mlflow)
        assert next(item for item in mlflow if item["kind"] == "github")["release_tag_pattern"]
        client.delete(f"/sources/{databricks[1]['id']}").raise_for_status()
        client.put(
            f"/categories/{categories['Databricks']}", json={"name": "Data platform"}
        ).raise_for_status()
        with application.state.sessions() as session:
            seed(session, False)
        assert len(client.get("/sources").json()) == len(sources) - 1
        assert "Databricks" not in {item["name"] for item in client.get("/categories").json()}


@pytest.mark.asyncio
async def test_duplicate_article_retains_both_source_memberships(application, monkeypatch):
    async def candidates(source, fetcher):
        return [
            Candidate(
                "A shared release",
                "https://example.com/shared",
                excerpt="Release notes",
                content_state="full",
            )
        ]

    monkeypatch.setattr("app.pipeline.collect", candidates)
    with application.state.sessions() as session:
        # Only two sources, both publishing the same URL.
        for source in session.scalars(select(Source)):
            source.enabled = False
        first = Source(name="Vendor", kind="rss", url="https://example.com/vendor", enabled=True)
        second = Source(name="Community", kind="rss", url="https://example.com/community", enabled=True)
        session.add_all([first, second])
        session.commit()
        await refresh(session, Settings(worker_enabled=False), force=True)
        ids = {first.id, second.id}
    with TestClient(application) as client:
        articles = client.get("/articles").json()
        assert len(articles) == 1
        assert set(articles[0]["source_ids"]) == ids
    with application.state.sessions() as session:
        assert len(list(session.scalars(select(Article)))) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("rate_limited", [False, True])
async def test_mlflow_filters_catalog_and_rate_limit_atom_fallback(rate_limited):
    requests = []

    def handler(request):
        requests.append(str(request.url))
        if request.url.host == "api.github.com":
            if rate_limited:
                return httpx.Response(403, json={"message": "rate limited"})
            return httpx.Response(
                200,
                json=[
                    {
                        "name": "Catalog",
                        "tag_name": "model-catalog/latest",
                        "html_url": "https://github.com/mlflow/mlflow/releases/tag/model-catalog/latest",
                    },
                    {
                        "name": "MLflow 3.17",
                        "tag_name": "v3.17.0",
                        "html_url": "https://github.com/mlflow/mlflow/releases/tag/v3.17.0",
                        "body": "Full release notes",
                    },
                    {
                        "name": "Preview",
                        "tag_name": "v3.18.0rc0",
                        "html_url": "https://github.com/mlflow/mlflow/releases/tag/v3.18.0rc0",
                    },
                ],
            )
        return httpx.Response(
            200,
            text="""<feed xmlns="http://www.w3.org/2005/Atom">
            <entry><title>Catalog</title><link href="https://github.com/mlflow/mlflow/releases/tag/model-catalog/latest"/></entry>
            <entry><title>MLflow 3.17</title><link href="https://github.com/mlflow/mlflow/releases/tag/v3.17.0"/>
            <content type="html">Full release notes</content></entry>
            <entry><title>TypeScript SDK</title><link href="https://github.com/mlflow/mlflow/releases/tag/typescript/v0.2.0"/></entry>
            </feed>""",
        )

    source = Source(name="MLflow", kind="github", url="https://github.com/mlflow/mlflow")
    source.collection_options = SourceCollectionOptions(release_tag_pattern=r"^v?\d+\.\d+\.\d+$")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        items = await collect(source, Fetcher(client))
    assert len(items) == 1
    assert items[0].url.endswith("/v3.17.0")
    assert items[0].excerpt == "Full release notes" and items[0].content_state == "full"
    assert len(requests) == (2 if rate_limited else 1)
