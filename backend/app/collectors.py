import asyncio
import calendar
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import unquote, urljoin, urlsplit

import feedparser
import trafilatura
from bs4 import BeautifulSoup

from .models import Source
from .network import Fetcher, canonical_url


@dataclass
class Candidate:
    title: str
    url: str
    published_at: datetime | None = None
    excerpt: str = ""
    content_state: str = "excerpt"
    hn: dict | None = None
    repository: dict | None = None
    repository_intro: bool = False


def matches_topics(text: str, topics: list[str]) -> bool:
    return not topics or any(
        re.search(r"\b" + re.escape(topic.strip()) + r"\b", text, re.I) for topic in topics if topic.strip()
    )


def plain_text(html: str) -> str:
    return BeautifulSoup(html, "html.parser").get_text(" ", strip=True)


def parse_date(value: str | None) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None
        return parsed.replace(tzinfo=UTC) if parsed and not parsed.tzinfo else parsed
    except (ValueError, TypeError):
        return None


def parse_feed(content: bytes, base_url: str) -> list[Candidate]:
    feed = feedparser.parse(content)
    if not feed.entries and feed.bozo:
        raise ValueError("Could not parse an RSS or Atom feed at this URL")
    items = []
    for entry in feed.entries[:30]:
        link = entry.get("link", "")
        if not link:
            continue
        date = entry.get("published_parsed") or entry.get("updated_parsed")
        published = datetime.fromtimestamp(calendar.timegm(date), UTC) if date else None
        excerpt = entry.get("summary", "")
        if entry.get("content"):
            excerpt = entry.content[0].get("value", excerpt)
        items.append(
            Candidate(
                plain_text(entry.get("title", "Untitled")),
                canonical_url(urljoin(base_url, link)),
                published,
                plain_text(excerpt),
            )
        )
    return items


def parse_website(html: str, source: Source) -> list[Candidate]:
    soup = BeautifulSoup(html, "html.parser")
    seen, items = set(), []
    for link in soup.select(source.link_selector):
        href = urljoin(source.url, link.get("href", ""))
        parsed = urlsplit(href)
        if parsed.hostname != urlsplit(source.url).hostname or parsed.scheme not in {"http", "https"}:
            continue
        if source.article_pattern and not re.search(source.article_pattern, parsed.path):
            continue
        href = canonical_url(href)
        title = link.get_text(" ", strip=True)
        if not title:
            title = link.get("aria-label") or link.get("title") or ""
        if not title:
            # Some CMS cards use an empty overlay link with a sibling heading.
            for container in [link.parent, link.parent.parent if link.parent else None]:
                if container:
                    headings = container.find_all(["h1", "h2", "h3", "h4"])
                    if len(headings) == 1:
                        title = headings[0].get_text(" ", strip=True)
                        break
        if len(title) < 15 or href in seen or href == canonical_url(source.url):
            continue
        seen.add(href)
        items.append(Candidate(title, href))
        if len(items) == 20:
            break
    if not items:
        raise ValueError("No articles matched; check the website selector and path pattern")
    return items


def extract_article(html: str) -> tuple[str, datetime | None]:
    text = trafilatura.extract(html, include_comments=False, include_tables=True) or ""
    soup = BeautifulSoup(html, "html.parser")
    date = None
    meta = soup.find("meta", attrs={"property": "article:published_time"})
    if meta:
        date = parse_date(meta.get("content"))
    if date is None:
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(script.string or "{}")
                nodes = data if isinstance(data, list) else [data]
                for node in nodes:
                    if isinstance(node, dict):
                        for item in node.get("@graph", [node]):
                            date = parse_date(item.get("datePublished")) if isinstance(item, dict) else None
                            if date:
                                break
                    if date:
                        break
            except (ValueError, TypeError):
                continue
    return text, date


async def collect(source: Source, fetcher: Fetcher) -> list[Candidate]:
    if source.kind == "github_org":
        from .github_org import collect_organization

        return await collect_organization(source, fetcher)
    if source.kind == "github_trending":
        from .trending import parse_trending

        response = await fetcher.get(source.url)
        return parse_trending(response.text)
    if source.kind == "rss":
        response = await fetcher.get(source.url)
        return parse_feed(response.content, str(response.url))
    if source.kind == "website":
        response = await fetcher.get(source.url)
        return parse_website(response.text, source)
    if source.kind == "github":
        parts = urlsplit(source.url).path.strip("/").split("/")
        if urlsplit(source.url).hostname != "github.com" or len(parts) < 2:
            raise ValueError("Use a GitHub repository URL, such as https://github.com/org/repo")
        try:
            response = await fetcher.get(
                f"https://api.github.com/repos/{parts[0]}/{parts[1]}/releases?per_page=20"
            )
        except Exception as error:
            import httpx

            if not isinstance(error, httpx.HTTPStatusError) or error.response.status_code not in {403, 429}:
                raise
            # Public Atom releases remain available when the shared anonymous
            # GitHub API quota is exhausted. Keep full published release notes.
            response = await fetcher.get(f"https://github.com/{parts[0]}/{parts[1]}/releases.atom")
            candidates = parse_feed(response.content, str(response.url))
            for item in candidates:
                item.content_state = "full"
            return [
                item
                for item in candidates
                if not source.release_tag_pattern
                or re.search(
                    source.release_tag_pattern,
                    unquote(urlsplit(item.url).path.split("/releases/tag/", 1)[-1]),
                )
            ]
        return [
            Candidate(
                item["name"] or item["tag_name"],
                item["html_url"],
                parse_date(item.get("published_at")),
                item.get("body") or "",
                "full",
            )
            for item in response.json()
            if not item.get("draft")
            and (
                not source.release_tag_pattern
                or re.search(source.release_tag_pattern, item.get("tag_name", ""))
            )
        ]
    if source.kind == "hacker_news":
        root = source.url.rstrip("/")
        response = await fetcher.get(root + "/topstories.json")
        newest = await fetcher.get(root + "/newstories.json")
        ids = list(dict.fromkeys(response.json()[:60] + newest.json()[:60]))
        semaphore = asyncio.Semaphore(8)

        async def item(item_id):
            async with semaphore:
                result = await fetcher.get(f"{root}/item/{item_id}.json")
                return result.json()

        records = await asyncio.gather(*(item(item_id) for item_id in ids), return_exceptions=True)
        items = []
        for record in records:
            if not isinstance(record, dict) or record.get("type") != "story" or record.get("dead"):
                continue
            if not matches_topics(record.get("title", ""), source.topics):
                continue
            items.append(
                Candidate(
                    record["title"],
                    record.get("url") or f"https://news.ycombinator.com/item?id={record['id']}",
                    datetime.fromtimestamp(record["time"], UTC),
                    plain_text(record.get("text", "")),
                    hn=record,
                )
            )
        return items
    raise ValueError("Unsupported source type")
