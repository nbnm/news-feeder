from urllib.parse import urlsplit

from sqlalchemy import select

from .models import Category, Source, SourceCategory, SourceCollectionOptions, WorkerState

STARTER_SOURCES = {
    "Databricks": [
        ("databricks-solutions", "github_org", "https://github.com/databricks-solutions", ""),
        (
            "databricks-industry-solutions",
            "github_org",
            "https://github.com/databricks-industry-solutions",
            "",
        ),
        ("databrickslabs", "github_org", "https://github.com/databrickslabs", ""),
        ("databricks", "github_org", "https://github.com/databricks", ""),
    ],
    "MLflow": [
        ("MLflow releases", "github", "https://github.com/mlflow/mlflow", r"^v?\d+\.\d+\.\d+$"),
        ("MLflow blog", "rss", "https://mlflow.org/blog/rss.xml", ""),
    ],
}


def seed_categories(session, demo: bool):
    # One-time, additive upgrade. Subsequent starts respect renamed categories,
    # moved/removed sources, and user-selected refresh intervals.
    marker = "categories:initialized:v1"
    if session.get(WorkerState, marker):
        return
    categories = {item.name: item for item in session.scalars(select(Category))}

    def category_for(name):
        if name not in categories:
            category = Category(name=name)
            session.add(category)
            session.flush()
            categories[name] = category
        return categories[name]

    sources = list(session.scalars(select(Source).where(Source.deleted.is_(False))))
    for source in sources:
        if source.name.startswith("Demo") or source.category_id:
            continue
        category = category_for(source.name)
        session.add(SourceCategory(source_id=source.id, category_id=category.id))
    existing = {source.url: source for source in sources}
    for name, starters in STARTER_SOURCES.items():
        category = category_for(name)
        for title, kind, url, pattern in starters:
            source = existing.get(url)
            if source is None:
                source = Source(
                    name=title,
                    kind=kind,
                    url=url,
                    enabled=not demo,
                    topics=[],
                    refresh_minutes=120,
                    article_pattern=pattern if kind == "website" else "",
                )
                session.add(source)
                session.flush()
                existing[url] = source
            membership = session.get(SourceCategory, source.id)
            if membership:
                membership.category_id = category.id
            else:
                session.add(SourceCategory(source_id=source.id, category_id=category.id))
            if kind == "github" and pattern:
                session.add(SourceCollectionOptions(source_id=source.id, release_tag_pattern=pattern))
    session.add(WorkerState(key=marker, value="1"))
    session.commit()


def upgrade_databricks_organizations(session, demo: bool):
    marker = "databricks:organizations:v1"
    if session.get(WorkerState, marker):
        return
    sources = list(session.scalars(select(Source).where(Source.deleted.is_(False))))

    def owner_for(source):
        parsed = urlsplit(source.url)
        parts = parsed.path.strip("/").split("/")
        return parts[0] if parsed.hostname == "github.com" and len(parts) >= 2 else None

    previous = [
        source
        for source in sources
        if source.kind == "github"
        and owner_for(source)
        in {"databricks", "databrickslabs", "databricks-solutions", "databricks-industry-solutions"}
    ]
    category_id = next((source.category_id for source in previous if source.category_id), None)
    if category_id is None:
        category_id = session.scalar(select(Category.id).where(Category.name == "Databricks"))
    if category_id is None:
        category = Category(name="Databricks")
        session.add(category)
        session.flush()
        category_id = category.id
    organizations = {}
    for name, kind, url, _ in STARTER_SOURCES["Databricks"]:
        source = next((item for item in sources if item.url.rstrip("/") == url), None)
        if source is None:
            source = Source(name=name, kind=kind, url=url, enabled=not demo, refresh_minutes=120, topics=[])
            session.add(source)
            session.flush()
        if source.kind != kind:
            source.kind = kind
            source.last_refreshed_at, source.last_error = None, None
        membership = session.get(SourceCategory, source.id)
        if membership:
            membership.category_id = category_id
        else:
            session.add(SourceCategory(source_id=source.id, category_id=category_id))
        organizations[url.rsplit("/", 1)[-1]] = source
    # Replace the old repository subscriptions; preserve archived articles and
    # add organization membership without changing article IDs or reading flags.
    from .models import Article, ArticleSource

    for old in previous:
        owner = owner_for(old)
        parent = organizations[owner]
        articles = session.scalars(select(Article).where(Article.source_id == old.id))
        linked_ids = session.scalars(
            select(ArticleSource.article_id).where(ArticleSource.source_id == old.id)
        )
        article_ids = {article.id for article in articles} | set(linked_ids)
        for article_id in article_ids:
            if session.get(ArticleSource, (article_id, parent.id)) is None:
                session.add(ArticleSource(article_id=article_id, source_id=parent.id))
        old.enabled, old.deleted = False, True
    session.add(WorkerState(key=marker, value="1"))
    session.commit()
