import re
from datetime import datetime
from typing import Literal
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    computed_field,
    field_serializer,
    field_validator,
    model_validator,
)

from .models import aware
from .profile import DEFAULT_PROFILE, profile_fingerprint


class CategoryInput(BaseModel):
    name: str = Field(min_length=1, max_length=200)

    @field_validator("name")
    @classmethod
    def clean_name(cls, value):
        if not value.strip():
            raise ValueError("Enter a category name")
        return value.strip()


class CategoryOutput(CategoryInput):
    model_config = ConfigDict(from_attributes=True)
    id: str


class SourceInput(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    kind: Literal["rss", "website", "github", "github_org", "github_trending", "hacker_news"]
    url: str
    enabled: bool = True
    topics: list[str] = Field(default_factory=list, max_length=30)
    refresh_minutes: int = Field(default=60, ge=15, le=10080)
    link_selector: str = "a[href]"
    article_pattern: str = ""
    category_id: str | None = None
    release_tag_pattern: str = Field(default="", max_length=500)

    @model_validator(mode="after")
    def daily_trending_source(self):
        if self.kind == "github_org":
            parsed = urlsplit(self.url)
            if (
                parsed.scheme != "https"
                or parsed.netloc.lower() != "github.com"
                or not re.fullmatch(r"/[A-Za-z0-9][A-Za-z0-9-]{0,38}/?", parsed.path)
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError("Use a GitHub organization URL, such as https://github.com/databrickslabs")
            self.url = self.url.rstrip("/")
        if self.kind == "github_trending":
            parsed = urlsplit(self.url)
            if (
                parsed.scheme != "https"
                or parsed.netloc.lower() != "github.com"
                or parsed.path.rstrip("/") != "/trending"
                or parse_qs(parsed.query) != {"since": ["daily"]}
                or parsed.fragment
            ):
                raise ValueError("Use https://github.com/trending?since=daily for the daily top 10")
            if self.topics:
                raise ValueError("Daily top 10 includes all topics; use your profile for relevance tags")
        return self

    @field_validator("name")
    @classmethod
    def clean_name(cls, value):
        if not value.strip():
            raise ValueError("Enter a source name")
        return value.strip()

    @field_validator("url")
    @classmethod
    def validate_url(cls, value):
        parsed = urlsplit(value.strip())
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username:
            raise ValueError("Enter a public HTTP or HTTPS URL without credentials")
        return value.strip()

    @field_validator("article_pattern", "release_tag_pattern")
    @classmethod
    def validate_pattern(cls, value):
        if len(value) > 500:
            raise ValueError("Pattern is too long")
        try:
            re.compile(value)
        except re.error as error:
            raise ValueError("Enter a valid regular expression") from error
        return value

    @field_validator("link_selector")
    @classmethod
    def validate_selector(cls, value):
        import soupsieve

        try:
            soupsieve.compile(value)
        except soupsieve.SelectorSyntaxError as error:
            raise ValueError("Enter a valid CSS link selector") from error
        return value


class TrendingSnapshot(BaseModel):
    observed_at: str
    article_ids: list[str] = Field(max_length=10)


class RepositoryMetadata(BaseModel):
    full_name: str
    description: str
    language: str | None
    stars_today: int | None = Field(ge=0)
    total_stars: int | None = Field(ge=0)
    rank: int = Field(ge=1, le=10)
    observed_at: str
    trending_url: str


class SourceOutput(SourceInput):
    model_config = ConfigDict(from_attributes=True)
    id: str
    last_refreshed_at: datetime | None
    last_error: str | None
    trending: TrendingSnapshot | None = None

    @field_serializer("last_refreshed_at")
    def utc_time(self, value):
        return aware(value).isoformat() if value else None


class Citation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    quote: str = Field(min_length=1, max_length=1000)


class SummaryContent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tldr: str = Field(min_length=1, max_length=500)
    what_changed: str
    why_it_matters: str
    key_points: list[str] = Field(max_length=5)
    topics: list[str] = Field(max_length=8)
    impact: Literal["low", "medium", "high", "unknown"]


class Summary(SummaryContent):
    citations: list[Citation] = Field(min_length=1, max_length=5)


class SummaryDraft(SummaryContent):
    citation_ids: list[int] = Field(min_length=1, max_length=5)


class RelevanceScore(BaseModel):
    score: float = Field(ge=0, le=100, allow_inf_nan=False)
    confidence: float = Field(ge=0, le=1, allow_inf_nan=False)


class TopicRelevance(RelevanceScore):
    topic: str


class RelevanceOutput(RelevanceScore):
    topics: list[TopicRelevance]
    model: str
    profile_hash: str
    assessed_at: str
    evidence: Literal["summary", "source_excerpt", "headline"]


class ArticleOutput(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    source_id: str
    source_name: str
    source_ids: list[str] = Field(default_factory=list)
    title: str
    url: str
    published_at: datetime | None
    discovered_at: datetime
    text: str
    content_state: str
    extraction_error: str | None
    summary: Summary | None
    summary_state: str
    summary_error: str | None
    tldr: str
    tldr_kind: str
    buzz: dict | None
    relevance: RelevanceOutput | None = None
    relevance_state: str = "pending"
    relevance_error: str | None = None
    repository: RepositoryMetadata | None = None

    @field_validator("summary", mode="before")
    @classmethod
    def archived_summary(cls, value):
        if isinstance(value, dict) and "tldr" not in value:
            # Archived summaries from before TLDR support remain readable.
            value = {**value, "tldr": value.get("what_changed", "")[:500]}
        return value

    @field_serializer("published_at", "discovered_at")
    def utc_time(self, value):
        return aware(value).isoformat() if value else None


class ReaderProfile(BaseModel):
    profile: str = Field(default=DEFAULT_PROFILE, max_length=6000)
    relevance_enabled: bool = True

    @field_validator("profile")
    @classmethod
    def clean_profile(cls, value):
        return value.strip()

    @computed_field
    @property
    def profile_hash(self) -> str:
        return profile_fingerprint(self.profile)


class Preferences(ReaderProfile):
    topics: list[str] = Field(default_factory=list, max_length=30)
    digest_size: int = Field(default=8, ge=1, le=20)
    delivery_hour: int = Field(default=8, ge=0, le=23)
    timezone: str = "America/Toronto"
    telegram_enabled: bool = False

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value):
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as error:
            raise ValueError("Enter an IANA timezone, for example America/Toronto") from error
        return value


class DigestOutput(BaseModel):
    id: str
    generated_at: datetime
    start_at: datetime
    end_at: datetime
    articles: list[ArticleOutput]


class SendRequest(BaseModel):
    kind: Literal["feed", "digest"] = "digest"
    article_id: str | None = None
