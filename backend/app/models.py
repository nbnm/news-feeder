from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def now() -> datetime:
    return datetime.now(UTC)


def aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def identifier() -> str:
    return str(uuid4())


class Base(DeclarativeBase):
    pass


class Category(Base):
    __tablename__ = "categories"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=identifier)
    name: Mapped[str] = mapped_column(String(200))


class SourceCategory(Base):
    __tablename__ = "source_categories"
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id"), primary_key=True)
    category_id: Mapped[str] = mapped_column(ForeignKey("categories.id"), index=True)


class SourceCollectionOptions(Base):
    __tablename__ = "source_collection_options"
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id"), primary_key=True)
    release_tag_pattern: Mapped[str] = mapped_column(Text, default="")


class Source(Base):
    __tablename__ = "sources"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=identifier)
    name: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(30))
    url: Mapped[str] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    deleted: Mapped[bool] = mapped_column(Boolean, default=False)
    topics: Mapped[list] = mapped_column(JSON, default=list)
    refresh_minutes: Mapped[int] = mapped_column(Integer, default=60)
    link_selector: Mapped[str] = mapped_column(Text, default="a[href]")
    article_pattern: Mapped[str] = mapped_column(Text, default="")
    last_refreshed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    category_record: Mapped["SourceCategory | None"] = relationship(lazy="selectin", uselist=False)
    collection_options: Mapped["SourceCollectionOptions | None"] = relationship(
        lazy="selectin", uselist=False
    )

    @property
    def category_id(self) -> str | None:
        return self.category_record.category_id if self.category_record else None

    @property
    def release_tag_pattern(self) -> str:
        return self.collection_options.release_tag_pattern if self.collection_options else ""


class ArticleSource(Base):
    __tablename__ = "article_sources"
    article_id: Mapped[str] = mapped_column(ForeignKey("articles.id"), primary_key=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id"), primary_key=True)


class Article(Base):
    __tablename__ = "articles"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=identifier)
    source_id: Mapped[str] = mapped_column(String(36), index=True)
    source_name: Mapped[str] = mapped_column(String(200))
    title: Mapped[str] = mapped_column(Text)
    url: Mapped[str] = mapped_column(Text, unique=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    discovered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, index=True)
    text: Mapped[str] = mapped_column(Text, default="")
    content_state: Mapped[str] = mapped_column(String(20), default="unavailable")
    content_hash: Mapped[str] = mapped_column(String(64), default="")
    extraction_error: Mapped[str | None] = mapped_column(Text)
    summary: Mapped[dict | None] = mapped_column(JSON)
    summary_state: Mapped[str] = mapped_column(String(20), default="pending")
    summary_error: Mapped[str | None] = mapped_column(Text)
    summary_model: Mapped[str | None] = mapped_column(String(200))
    summary_attempted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    buzz: Mapped[dict | None] = mapped_column(JSON)
    relevance_record: Mapped["Relevance | None"] = relationship(lazy="selectin", uselist=False)
    repository_record: Mapped["RepositoryRecord | None"] = relationship(lazy="selectin", uselist=False)
    source_records: Mapped[list["ArticleSource"]] = relationship(lazy="selectin")

    @property
    def source_ids(self) -> list[str]:
        return sorted({self.source_id, *(record.source_id for record in self.source_records)})

    @property
    def repository(self) -> dict | None:
        return self.repository_record.value if self.repository_record else None

    @property
    def tldr(self) -> str:
        from .tldr import article_tldr

        return article_tldr(self)[0]

    @property
    def tldr_kind(self) -> str:
        from .tldr import article_tldr

        return article_tldr(self)[1]


class RepositoryRecord(Base):
    # Additive metadata keeps existing article rows and reading state intact.
    __tablename__ = "repository_metadata"
    article_id: Mapped[str] = mapped_column(ForeignKey("articles.id"), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON)


class Relevance(Base):
    # Additive table: existing archives need no destructive schema migration.
    __tablename__ = "article_relevance"
    article_id: Mapped[str] = mapped_column(ForeignKey("articles.id"), primary_key=True)
    profile_hash: Mapped[str] = mapped_column(String(64))
    content_hash: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(20), default="pending")
    result: Mapped[dict | None] = mapped_column(JSON)
    error: Mapped[str | None] = mapped_column(Text)
    attempted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Signal(Base):
    __tablename__ = "signals"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=identifier)
    article_id: Mapped[str] = mapped_column(String(36), index=True)
    hn_id: Mapped[int] = mapped_column(Integer, index=True)
    score: Mapped[int] = mapped_column(Integer)
    comments: Mapped[int] = mapped_column(Integer)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Preference(Base):
    __tablename__ = "preferences"
    id: Mapped[str] = mapped_column(String(30), primary_key=True, default="owner")
    value: Mapped[dict] = mapped_column(JSON)


class Digest(Base):
    __tablename__ = "digests"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=identifier)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), unique=True)
    article_ids: Mapped[list] = mapped_column(JSON)


class Delivery(Base):
    __tablename__ = "deliveries"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=identifier)
    dedupe_key: Mapped[str] = mapped_column(Text, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    state: Mapped[str] = mapped_column(String(20), default="pending")
    sent_count: Mapped[int] = mapped_column(Integer, default=0)
    total_count: Mapped[int] = mapped_column(Integer)
    messages: Mapped[list] = mapped_column(JSON)
    error: Mapped[str | None] = mapped_column(Text)


class WorkerState(Base):
    __tablename__ = "worker_state"
    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
