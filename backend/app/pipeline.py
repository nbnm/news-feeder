import asyncio
import hashlib
from datetime import timedelta

import httpx
from sqlalchemy import select

from .collectors import collect, extract_article, matches_topics
from .config import Settings
from .llm import summarize
from .models import Article, ArticleSource, Signal, Source, aware, now
from .network import Fetcher, canonical_url
from .trending import save_snapshot


def record_signal(session, article: Article, record: dict):
    captured = now()
    previous = session.scalar(
        select(Signal)
        .where(Signal.article_id == article.id, Signal.hn_id == record["id"])
        .order_by(Signal.observed_at.desc())
        .limit(1)
    )
    growth = None
    if previous:
        elapsed = (captured - aware(previous.observed_at)).total_seconds() / 3600
        if elapsed >= 0.05:
            growth = round(max(0, record.get("score", 0) - previous.score) / elapsed, 1)
    score, comments = record.get("score", 0), record.get("descendants", 0)
    # Deliberately simple evidence bands; no inferred internet-wide hype score.
    label = "high" if score >= 200 else "rising" if growth is not None and growth >= 10 else "observed"
    article.buzz = {
        "label": label,
        "points": score,
        "comments": comments,
        "points_per_hour": growth,
        "observed_at": captured.isoformat(),
        "url": f"https://news.ycombinator.com/item?id={record['id']}",
    }
    session.add(
        Signal(
            article_id=article.id, hn_id=record["id"], score=score, comments=comments, observed_at=captured
        )
    )


async def article_content(item, fetcher, semaphore):
    if item.repository_intro:
        from .trending import repository_content

        return await repository_content(item, fetcher, semaphore)
    text, state, published, error = item.excerpt, item.content_state, None, None
    if state != "full":
        try:
            async with semaphore, asyncio.timeout(30):
                response = await fetcher.get(canonical_url(item.url))
                extracted, published = extract_article(response.text)
            if len(extracted) >= 120:
                text, state = extracted, "full"
        except Exception:
            error = "Article extraction failed; showing the available feed excerpt"
    return item, text, state if text else "unavailable", published, error


async def process_summaries(session, settings, client, limit=50):
    pending = list(
        session.scalars(
            select(Article)
            .where(Article.summary_state != "ready", Article.text != "")
            .order_by(Article.summary_attempted_at.asc().nullsfirst(), Article.discovered_at.desc())
            .limit(limit)
        )
    )
    if not settings.llm_ready:
        for article in pending:
            article.summary_state, article.summary_attempted_at = "unconfigured", now()
        session.commit()
        return 0
    jobs = []
    for article in pending:
        cached = session.scalar(
            select(Article)
            .where(
                Article.content_hash == article.content_hash,
                Article.summary_model == settings.litellm_model,
                Article.summary_state == "ready",
            )
            .limit(1)
        )
        jobs.append((article, cached.summary if cached else None))

    async def generate(article, cached):
        try:
            summary = cached or (await summarize(article.title, article.text, settings, client)).model_dump()
            return article, summary, None
        except Exception as error:
            message = str(error)[:300] if isinstance(error, ValueError) else type(error).__name__
            return article, None, message

    ready = 0
    # Network calls run concurrently; all database writes stay in the owner task.
    for offset in range(0, len(jobs), 3):
        results = await asyncio.gather(
            *(generate(article, cached) for article, cached in jobs[offset : offset + 3])
        )
        for article, summary, error in results:
            article.summary_attempted_at = now()
            article.summary_error = error
            if error is None:
                article.summary, article.summary_state = summary, "ready"
                article.summary_model = settings.litellm_model
                ready += 1
            else:
                article.summary_state = "error"
            session.commit()
    return ready


async def refresh(session, settings: Settings, force: bool = False) -> dict:
    if settings.demo_mode:
        return {"sources": 0, "added": 0, "errors": [], "demo": True}
    result = {"sources": 0, "added": 0, "errors": [], "demo": False}
    async with httpx.AsyncClient(timeout=25) as client:
        fetcher = Fetcher(client)
        sources = session.scalars(select(Source).where(Source.enabled.is_(True), Source.deleted.is_(False)))
        for source in sources:
            if (
                not force
                and source.last_refreshed_at
                and (now() - aware(source.last_refreshed_at) < timedelta(minutes=source.refresh_minutes))
            ):
                continue
            result["sources"] += 1
            try:
                candidates = await collect(source, fetcher)
                if source.kind == "github_trending":
                    result["added"] += await save_snapshot(session, source, candidates, fetcher)
                    source.last_error = None
                    source.last_refreshed_at = now()
                    session.commit()
                    continue
                seen, fresh = set(), []
                for item in candidates:
                    url = canonical_url(item.url)
                    if url in seen:
                        continue
                    seen.add(url)
                    article = session.scalar(select(Article).where(Article.url == url))
                    if article is None:
                        if not matches_topics(item.title + " " + item.excerpt, source.topics):
                            continue
                        fresh.append(item)
                    else:
                        if session.get(ArticleSource, (article.id, source.id)) is None:
                            session.add(ArticleSource(article_id=article.id, source_id=source.id))
                        if item.hn:
                            record_signal(session, article, item.hn)
                semaphore = asyncio.Semaphore(4)
                # Keep network work bounded, and publish each completed group so
                # the reader can show actual stories before the whole refresh ends.
                for offset in range(0, len(fresh), 4):
                    extracted = await asyncio.gather(
                        *(article_content(item, fetcher, semaphore) for item in fresh[offset : offset + 4])
                    )
                    for item, text, state, published, extraction_error in extracted:
                        article = Article(
                            source_id=source.id,
                            source_name=source.name,
                            title=item.title,
                            url=canonical_url(item.url),
                            published_at=item.published_at or published,
                            text=text,
                            content_state=state,
                            extraction_error=extraction_error,
                            content_hash=hashlib.sha256(text.encode()).hexdigest(),
                            summary_state="pending" if text else "unavailable",
                        )
                        session.add(article)
                        session.flush()
                        result["added"] += 1
                        if item.hn:
                            record_signal(session, article, item.hn)
                    session.commit()
                source.last_error = None
            except Exception as error:
                # Keep errors useful without returning network URLs or potential credentials.
                if isinstance(error, httpx.HTTPStatusError):
                    source.last_error = f"Source returned HTTP {error.response.status_code}"
                else:
                    source.last_error = (
                        str(error)[:300] if isinstance(error, ValueError) else type(error).__name__
                    )
                result["errors"].append({"source": source.name, "error": source.last_error})
            source.last_refreshed_at = now()
            session.commit()
        result["summaries_ready"] = await process_summaries(session, settings, client)
    return result
