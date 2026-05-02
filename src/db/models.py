from datetime import datetime, timezone

from sqlalchemy import Index, Integer, SmallInteger, Float, Text, String, JSON, DateTime
from sqlalchemy.orm import Mapped, mapped_column

from src.db.database import Base


class CrawlResult(Base):
    __tablename__ = "crawl_results"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    url_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    domain: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)

    http_status: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    canonical_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    language: Mapped[str | None] = mapped_column(String(10), nullable=True)
    og_tags: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    classification_mode: Mapped[str | None] = mapped_column(String(10), nullable=True)
    topics: Mapped[list | None] = mapped_column(JSON, nullable=True)
    keywords: Mapped[list | None] = mapped_column(JSON, nullable=True)
    entities: Mapped[list | None] = mapped_column(JSON, nullable=True)
    category: Mapped[str | None] = mapped_column(String(255), nullable=True)
    warnings: Mapped[list | None] = mapped_column(JSON, nullable=True)

    error_code: Mapped[str | None] = mapped_column(String(50), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    response_time_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    content_length: Mapped[int | None] = mapped_column(Integer, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        Index("ix_crawl_results_domain", "domain"),
        Index("ix_crawl_results_created_at", "created_at"),
    )
