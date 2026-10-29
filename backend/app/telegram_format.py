"""Readable Telegram messages with explicit entities, never parsed source markup."""

from bisect import bisect_left, bisect_right
from typing import NotRequired, TypedDict
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from .models import Article, aware
from .relevance import current_relevance, relevance_level

MESSAGE_LIMIT = 3800


class Entity(TypedDict):
    type: str
    offset: int
    length: int
    url: NotRequired[str]


class FormattedMessage(TypedDict):
    text: str
    entities: list[Entity]


def utf16_length(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


class _Text:
    def __init__(self):
        self.parts = []
        self.entities = []
        self.length = 0

    def add(self, text, style=None, url=None):
        length = utf16_length(text)
        if style and length:
            entity = {"type": style, "offset": self.length, "length": length}
            if url:
                entity["url"] = url
            self.entities.append(entity)
        self.parts.append(text)
        self.length += length

    def extend(self, message):
        self.entities.extend(
            {**entity, "offset": entity["offset"] + self.length} for entity in message["entities"]
        )
        self.add(message["text"])

    def finish(self) -> FormattedMessage:
        return {"text": "".join(self.parts), "entities": self.entities.copy()}


def split_formatted_message(message: FormattedMessage, limit: int) -> list[FormattedMessage]:
    """Split at paragraph/word boundaries, preserving text and UTF-16 entity ranges."""
    if limit < 2:
        raise ValueError("Telegram message limit must allow an emoji")
    text = message["text"]
    positions = [0]
    for character in text:
        positions.append(positions[-1] + (2 if ord(character) > 0xFFFF else 1))
    parts, start = [], 0
    while start < len(text):
        end = bisect_right(positions, positions[start] + limit) - 1
        if end < len(text):
            middle = start + (end - start) // 2
            for separator in ("\n\n", "\n", " "):
                boundary = text.rfind(separator, middle, end)
                if boundary >= middle:
                    end = boundary + len(separator)
                    break
            # Keep short link labels together rather than dividing a source link.
            for entity in message["entities"]:
                if (
                    entity["type"] == "text_link"
                    and positions[start] < entity["offset"] < positions[end]
                    and positions[end] < entity["offset"] + entity["length"]
                ):
                    end = bisect_left(positions, entity["offset"])
        left, right = positions[start], positions[end]
        entities = []
        for entity in message["entities"]:
            begin, finish = max(left, entity["offset"]), min(right, entity["offset"] + entity["length"])
            if begin < finish:
                entities.append({**entity, "offset": begin - left, "length": finish - begin})
        parts.append({"text": text[start:end], "entities": entities})
        start = end
    return parts


def _link(output, label, url):
    try:
        parsed = urlsplit(url)
        valid = (
            parsed.scheme in {"http", "https"}
            and bool(parsed.hostname)
            and not parsed.username
            and not any(ord(character) <= 32 for character in url)
        )
    except ValueError:
        valid = False
    if valid:
        output.add(label, "text_link", url)
    else:
        output.add("Source link unavailable", "italic")


def _header(title, count, part=None):
    output = _Text()
    heading, _, note = title.partition("\n")
    output.add("🗞 " + heading, "bold")
    output.add("\n")
    count_label = f"{count} {'story' if count == 1 else 'stories'}"
    if part:
        count_label += f" · Part {part[0]}/{part[1]}"
    output.add(count_label, "italic")
    if note:
        output.add("\n" + note, "italic")
    return output.finish()


def _story(article, index, prefs):
    output = _Text()
    output.add(f"{index}. {article.title}", "bold")
    output.add("\n")
    relevance = current_relevance(article, prefs) if prefs else None
    metadata = article.source_name
    if relevance:
        metadata += f" · {relevance_level(relevance['score'])} priority"
    output.add(metadata, "italic")
    timestamp = article.published_at or article.discovered_at
    if timestamp is not None:
        timezone = ZoneInfo(prefs.timezone if prefs else "America/Toronto")
        local = aware(timestamp).astimezone(timezone)
        label = "Published" if article.published_at is not None else "First seen"
        output.add("\n")
        output.add(f"🕒 {label}: {local:%d %b %Y · %H:%M %Z} (UTC{local:%z})", "italic")
    output.add("\n\n")
    output.add("⚡ TLDR", "bold")
    output.add("\n" + article.tldr)
    if article.tldr_kind != "summary":
        output.add("\n")
        output.add(
            "Preview: headline only" if article.tldr_kind == "headline" else "Preview: source excerpt",
            "italic",
        )
    if (article.summary or {}).get("why_it_matters"):
        output.add("\n\n")
        output.add("💡 Why it matters", "bold")
        output.add("\n" + article.summary["why_it_matters"])
    if article.repository:
        repo = article.repository
        output.add("\n\n")
        output.add(f"⭐ GitHub daily rank: #{repo['rank']}", "bold")
        output.add(" · " + (repo["language"] or "Language unspecified"))
        metrics = []
        if repo["stars_today"] is not None:
            metrics.append(f"Stars gained today: {repo['stars_today']:,}")
        if repo["total_stars"] is not None:
            metrics.append(f"Total stars: {repo['total_stars']:,}")
        if metrics:
            output.add("\n" + " · ".join(metrics))
        output.add("\nTrending snapshot: " + repo["observed_at"], "italic")
        output.add("\n")
        _link(output, "↗ Trending list", repo["trending_url"])
    if article.buzz:
        output.add(f"\n\n💬 HN buzz: {article.buzz['points']} points · {article.buzz['comments']} comments")
    output.add("\n\n")
    _link(output, "↗ Read source", article.url)
    return output.finish()


def format_articles(articles: list[Article], title: str, prefs=None) -> list[FormattedMessage]:
    header = _header(title, len(articles))
    # Reserve room for pagination and an oversized story's continuation label.
    budget = MESSAGE_LIMIT - utf16_length(header["text"]) - 64
    if budget < 128:
        raise ValueError("Digest heading leaves no room for stories")
    fragments = []
    for index, article in enumerate(articles, 1):
        story = _story(article, index, prefs)
        pieces = (
            [story]
            if utf16_length(story["text"]) <= budget
            else split_formatted_message(story, budget - 32)
        )
        for part, piece in enumerate(pieces):
            if part:
                continued = _Text()
                continued.add(f"{index}. Continued", "bold")
                continued.add("\n\n")
                continued.extend(piece)
                piece = continued.finish()
            fragments.append(piece)
    if not fragments:
        fragments = [{"text": "No new stories in this period.", "entities": []}]
    groups, current = [], _Text()
    for fragment in fragments:
        if current.length and current.length + 2 + utf16_length(fragment["text"]) > budget:
            groups.append(current.finish())
            current = _Text()
        if current.length:
            current.add("\n\n")
        current.extend(fragment)
    groups.append(current.finish())
    messages = []
    for index, group in enumerate(groups, 1):
        output = _Text()
        output.extend(_header(title, len(articles), (index, len(groups)) if len(groups) > 1 else None))
        output.add("\n\n")
        output.extend(group)
        messages.append(output.finish())
    return messages
