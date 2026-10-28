import re
from datetime import timedelta
from urllib.parse import urlsplit

from .collectors import Candidate, parse_date, plain_text
from .models import now
from .network import canonical_url


def organization_name(url):
    parsed = urlsplit(url)
    name = parsed.path.strip("/")
    if parsed.hostname != "github.com" or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}", name):
        raise ValueError("Use a GitHub organization URL")
    return name


def owned_repository(name, organization):
    return (
        isinstance(name, str)
        and re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", name)
        and (name.split("/", 1)[0].lower() == organization.lower())
    )


def parse_organization_events(events, organization, release_pattern=""):
    items = []
    for event in events:
        if not isinstance(event, dict) or event.get("public") is False:
            continue
        repo = event.get("repo", {}).get("name")
        if not owned_repository(repo, organization):
            continue
        payload = event.get("payload") or {}
        date = parse_date(event.get("created_at"))
        if event.get("type") == "ReleaseEvent" and payload.get("action") == "published":
            release = payload.get("release") or {}
            tag = release.get("tag_name", "")
            url = release.get("html_url", "")
            if release.get("draft") or not url.lower().startswith(
                f"https://github.com/{repo}/releases/tag/".lower()
            ):
                continue
            if release_pattern and not re.search(release_pattern, tag):
                continue
            title = release.get("name") or tag or "Release"
            body = release.get("body") or ""
            items.append(
                Candidate(
                    f"{repo}: {title}",
                    canonical_url(url),
                    parse_date(release.get("published_at")) or date,
                    body,
                    "full" if body else "excerpt",
                )
            )
        elif event.get("type") == "PublicEvent" or (
            event.get("type") == "CreateEvent" and payload.get("ref_type") == "repository"
        ):
            description = plain_text(payload.get("description") or "")
            items.append(repository_introduction(repo, date, description))
    return items


def repository_introduction(repo, date, description):
    text = f"New public repository: {repo}\nGitHub organization: {repo.split('/')[0]}"
    if description:
        text += "\nRepository description: " + description
    return Candidate(
        f"New public repository: {repo}", f"https://github.com/{repo}", date, text, repository_intro=True
    )


async def collect_organization(source, fetcher):
    organization = organization_name(source.url)
    items = []
    # GitHub exposes at most 300 public events from the past 30 days. Read all
    # pages, including delayed/out-of-order events, rather than advancing a cursor
    # that could skip releases. Article URL deduplication makes repeated polls safe.
    for page in range(1, 4):
        response = await fetcher.get(
            f"https://api.github.com/orgs/{organization}/events?per_page=100&page={page}"
        )
        events = response.json()
        if not isinstance(events, list):
            raise ValueError("GitHub returned an invalid organization activity feed")
        items.extend(parse_organization_events(events, organization, source.release_tag_pattern))
        if len(events) < 100:
            break
    # A busy event timeline can omit repository creations. The creation-sorted
    # inventory discovers new public repositories across the entire organization.
    cutoff = now() - timedelta(days=30)
    page = 1
    while True:
        response = await fetcher.get(
            f"https://api.github.com/orgs/{organization}/repos?type=public&sort=created&direction=desc&per_page=100&page={page}"
        )
        repos = response.json()
        if not isinstance(repos, list):
            raise ValueError("GitHub returned an invalid repository inventory")
        dates = []
        for repo in repos:
            date = parse_date(repo.get("created_at"))
            if date:
                dates.append(date)
            name = repo.get("full_name")
            if not date or date < cutoff or not owned_repository(name, organization) or repo.get("fork"):
                continue
            items.append(repository_introduction(name, date, repo.get("description") or ""))
        if len(repos) < 100 or (dates and min(dates) < cutoff):
            break
        page += 1
        if page > 20:
            raise ValueError("Organization has more than 2,000 new repositories; narrow the source")
    unique = {}
    for item in items:
        unique.setdefault(item.url, item)
    return sorted(unique.values(), key=lambda item: item.published_at or cutoff, reverse=True)
