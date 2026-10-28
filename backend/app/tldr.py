"""Readable previews while generated summaries are pending. No invented text."""

import re


def clean_tldr(value: str) -> str:
    def remove_reference(match):
        content = match.group(1).strip()
        labeled = re.match(r"(?:evidences?|citations?|ids?|references?)\s*:?\s*(.*)", content, re.I)
        numbers = labeled.group(1) if labeled else content
        if not labeled and "," not in numbers:
            return match.group(0)
        if not re.fullmatch(r"[\d\s,–-]+", numbers):
            return match.group(0)
        ids = [int(number) for number in re.findall(r"\d+", numbers)]
        if not ids or any(number < 1 or number > 60 for number in ids):
            return match.group(0)
        return ""

    result = re.sub(r"[\[(]([^\[\]()]+)[\])]", remove_reference, value)
    return re.sub(r"\s+([.,;:])", r"\1", re.sub(r"\s+", " ", result)).strip()


def source_excerpt(text: str, limit: int = 360) -> str:
    paragraphs = [re.sub(r"\s+", " ", part).strip() for part in re.split(r"\n\s*\n", text)]
    substantial = next((part for part in paragraphs if len(part.split()) >= 12), "")
    value = substantial or re.sub(r"\s+", " ", text).strip()
    if len(value) <= limit:
        return value
    return value[: limit - 1].rsplit(" ", 1)[0].rstrip(".,;:") + "…"


def article_tldr(article) -> tuple[str, str]:
    summary = article.summary or {}
    generated = summary.get("tldr") or summary.get("what_changed")
    if generated:
        return clean_tldr(generated), "summary"
    excerpt = source_excerpt(article.text or "")
    return (excerpt, "source_excerpt") if excerpt else (article.title, "headline")
