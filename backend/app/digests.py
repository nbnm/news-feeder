from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select

from .collectors import matches_topics
from .models import Article, Digest, Preference, aware, now
from .relevance import article_output, current_relevance
from .schemas import DigestOutput, Preferences


def preferences(session) -> Preferences:
    return Preferences.model_validate(session.get(Preference, "owner").value)


def cutoff_at(timestamp: datetime, prefs: Preferences) -> datetime:
    local = aware(timestamp).astimezone(ZoneInfo(prefs.timezone))
    cutoff = local.replace(hour=prefs.delivery_hour, minute=0, second=0, microsecond=0)
    if local < cutoff:
        cutoff -= timedelta(days=1)
    return aware(cutoff)


def build_digest(session, cutoff: datetime | None = None) -> Digest:
    prefs = preferences(session)
    end = aware(cutoff or now())
    existing = session.scalar(select(Digest).where(Digest.end_at == end))
    if existing:
        return existing
    previous = session.scalar(
        select(Digest).where(Digest.end_at < end).order_by(Digest.end_at.desc()).limit(1)
    )
    start = (
        aware(previous.end_at)
        if previous
        else aware(end.astimezone(ZoneInfo(prefs.timezone)) - timedelta(days=1))
    )
    # Include late discoveries; do not drop articles because their publisher timestamp was earlier.
    articles = list(
        session.scalars(select(Article).where(Article.discovered_at > start, Article.discovered_at <= end))
    )

    def rank(article):
        summary = article.summary or {}
        relevant = matches_topics(article.title + " " + " ".join(summary.get("topics", [])), prefs.topics)
        impact = {"high": 3, "medium": 2, "low": 1}.get(summary.get("impact"), 0)
        assessment = current_relevance(article, prefs)
        return (
            assessment is not None,
            assessment["score"] if assessment else 0,
            relevant,
            impact,
            aware(article.discovered_at),
        )

    articles.sort(key=rank, reverse=True)
    digest = Digest(start_at=start, end_at=end, article_ids=[a.id for a in articles[: prefs.digest_size]])
    session.add(digest)
    session.commit()
    return digest


def digest_output(session, digest: Digest) -> DigestOutput:
    articles = [session.get(Article, article_id) for article_id in digest.article_ids]
    return DigestOutput(
        id=digest.id,
        generated_at=aware(digest.generated_at),
        start_at=aware(digest.start_at),
        end_at=aware(digest.end_at),
        articles=[article_output(article, preferences(session)) for article in articles if article],
    )
