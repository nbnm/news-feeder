import hashlib
from datetime import timedelta

from sqlalchemy import select

from .categories import seed_categories, upgrade_databricks_organizations
from .models import Article, Preference, Source, now
from .schemas import Preferences


def seed(session, demo: bool):
    defaults = Preferences().model_dump(exclude={"profile_hash"})
    owner = session.get(Preference, "owner")
    if owner is None:
        session.add(Preference(id="owner", value=defaults))
    else:
        # Backfill the editable starter profile while preserving existing choices.
        owner.value = {**defaults, **owner.value}
    if not session.scalar(select(Source.id).limit(1)):
        defaults = [
            ("OpenAI", "rss", "https://openai.com/news/rss.xml", ""),
            ("Anthropic", "website", "https://www.anthropic.com/news", r"^/news/[^/]+/?$"),
            ("LangChain / LangGraph", "website", "https://www.langchain.com/blog", r"^/blog/[^/]+/?$"),
            ("Databricks", "website", "https://www.databricks.com/blog", r"^/blog/[^/]+/?$"),
            ("Y Combinator", "website", "https://www.ycombinator.com/blog", r"^/blog/[^/]+/?$"),
            ("Hacker News", "hacker_news", "https://hacker-news.firebaseio.com/v0", ""),
        ]
        for name, kind, url, pattern in defaults:
            topics = ["AI", "LLM", "agents", "models", "OpenAI", "Claude", "Databricks", "LangChain"]
            session.add(
                Source(
                    name=name,
                    kind=kind,
                    url=url,
                    article_pattern=pattern,
                    topics=topics if kind == "hacker_news" else [],
                    enabled=not demo,
                )
            )
    session.commit()
    seed_categories(session, demo)
    upgrade_databricks_organizations(session, demo)
    if demo and not session.scalar(select(Article.id).limit(1)):
        source = Source(
            name="Demo — fictional examples", kind="rss", url="https://example.com/feed", enabled=False
        )
        session.add(source)
        session.flush()
        items = [
            (
                "A model release with a smaller serving footprint",
                "Models",
                "high",
                "This fictional model adds a smaller serving footprint and an optional reasoning budget.",
                "A smaller deployment can make experimentation easier. Evaluate quality on your own tasks.",
            ),
            (
                "An agent framework adds resumable workflows",
                "Agents",
                "medium",
                "This fictional framework adds persisted checkpoints "
                "and a resume API for interrupted workflows.",
                "Resuming a workflow can reduce repeated work when a long-running task fails.",
            ),
            (
                "A data platform introduces governed API access",
                "Data platforms",
                "medium",
                "This fictional platform adds per-service permissions and an audit trail for API access.",
                "Service-level permissions could simplify access management for data applications.",
            ),
        ]
        for index, (title, topic, impact, text, why) in enumerate(items):
            text = "DEMO CONTENT — fictional, not a real announcement. " + text
            session.add(
                Article(
                    source_id=source.id,
                    source_name=source.name,
                    title=title,
                    url=f"https://example.com/demo/{index + 1}",
                    published_at=now() - timedelta(hours=index + 1),
                    text=text,
                    content_state="excerpt",
                    content_hash=hashlib.sha256(text.encode()).hexdigest(),
                    summary_state="ready",
                    summary_model="demo",
                    summary={
                        "tldr": text,
                        "what_changed": text,
                        "why_it_matters": why,
                        "key_points": ["Fictional demo story"],
                        "topics": [topic],
                        "impact": impact,
                        "citations": [{"quote": text}],
                    },
                )
            )
        session.commit()
