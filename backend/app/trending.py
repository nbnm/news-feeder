"""GitHub's daily top ten, with separate live membership and archived content."""

import asyncio
import hashlib
import json
import re

from bs4 import BeautifulSoup
from sqlalchemy import select

from .collectors import Candidate
from .models import Article, RepositoryRecord, WorkerState, now
from .schemas import SourceOutput, TrendingSnapshot

TRENDING_URL = "https://github.com/trending?since=daily"


def count(value: str) -> int | None:
    text = value.strip().replace(",", "")
    return int(text) if text.isdecimal() else None


def parse_trending(html: str) -> list[Candidate]:
    cards = BeautifulSoup(html, "html.parser").select("article.Box-row")[:10]
    if not cards:
        raise ValueError("No GitHub Trending repositories found; the previous snapshot is retained")
    items, seen = [], set()
    for rank, card in enumerate(cards, 1):
        link = card.select_one("h2 a[href]")
        path = link.get("href", "") if link else ""
        if not re.fullmatch(r"/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", path) or path in seen:
            raise ValueError("GitHub Trending markup changed; the previous snapshot is retained")
        seen.add(path)
        full_name = path[1:]
        description = card.select_one("p")
        description = description.get_text(" ", strip=True) if description else ""
        language = card.select_one('[itemprop="programmingLanguage"]')
        language = language.get_text(" ", strip=True) if language else None
        stars = card.select_one(f'a[href="{path}/stargazers"]')
        total = count(stars.get_text(" ", strip=True)) if stars else None
        daily = re.search(r"([\d,]+)\s+stars?\s+today\b", card.get_text(" ", strip=True))
        today = count(daily.group(1)) if daily else None
        if today is None or total is None:
            raise ValueError("GitHub daily star counts are unavailable; the previous snapshot is retained")
        overview = f"Repository: {full_name}.\nDescription: {description or 'No description supplied.'}"
        if language:
            overview += f"\nPrimary language: {language}."
        items.append(
            Candidate(
                title=full_name,
                url="https://github.com" + path,
                excerpt=overview,
                repository={
                    "full_name": full_name,
                    "description": description,
                    "language": language,
                    "stars_today": today,
                    "total_stars": total,
                    "rank": rank,
                },
            )
        )
    return items


async def repository_content(item, fetcher, semaphore):
    text, state, error = item.excerpt, "excerpt", None
    try:
        async with semaphore, asyncio.timeout(30):
            response = await fetcher.get(item.url)
            readme = BeautifulSoup(response.text, "html.parser").select_one("article.markdown-body")
        if readme and readme.get_text("\n", strip=True):
            text += "\n\nRepository README:\n" + readme.get_text("\n", strip=True)[:24000]
            state = "full"
        else:
            error = "README unavailable; showing the repository description."
    except Exception:
        error = "README could not be fetched; showing the repository description."
    return item, text, state, None, error


async def save_snapshot(session, source, items, fetcher):
    # Collect/parse the entire list before publishing membership. Failed requests
    # cannot replace a good snapshot with an empty feed.
    if not items or len(items) > 10 or any(not item.repository for item in items):
        raise ValueError("Invalid GitHub Trending snapshot")
    captured = now().isoformat()
    articles, fresh = {}, []
    for item in items:
        article = session.scalar(select(Article).where(Article.url == item.url))
        if article:
            articles[item.url] = article
        else:
            fresh.append(item)
    semaphore = asyncio.Semaphore(4)
    for offset in range(0, len(fresh), 4):
        extracted = await asyncio.gather(
            *(repository_content(item, fetcher, semaphore) for item in fresh[offset : offset + 4])
        )
        for item, text, state, _, error in extracted:
            article = Article(
                source_id=source.id,
                source_name=source.name,
                title=item.title,
                url=item.url,
                text=text,
                content_state=state,
                extraction_error=error,
                content_hash=hashlib.sha256(text.encode()).hexdigest(),
                summary_state="pending",
            )
            session.add(article)
            session.flush()
            articles[item.url] = article
    ids = []
    for item in items:
        article = articles[item.url]
        value = {**item.repository, "observed_at": captured, "trending_url": source.url}
        record = session.get(RepositoryRecord, article.id)
        if record:
            record.value = value
        else:
            session.add(RepositoryRecord(article_id=article.id, value=value))
        ids.append(article.id)
    key = "github_trending:" + source.id
    value = json.dumps({"observed_at": captured, "article_ids": ids})
    snapshot = session.get(WorkerState, key)
    if snapshot:
        snapshot.value = value
    else:
        session.add(WorkerState(key=key, value=value))
    session.commit()
    return len(fresh)


def source_output(session, source):
    output = SourceOutput.model_validate(source)
    if source.kind == "github_trending":
        snapshot = session.get(WorkerState, "github_trending:" + source.id)
        if snapshot:
            output.trending = TrendingSnapshot.model_validate_json(snapshot.value)
    return output
