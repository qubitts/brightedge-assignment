import hashlib
import logging
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.db.models import CrawlResult
from src.models.response import CrawlResponse

logger = logging.getLogger(__name__)


def _url_hash(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()


def _extract_domain(url: str) -> str:
    return urlparse(url).netloc


def _response_to_row(response: CrawlResponse) -> dict:
    """Convert a CrawlResponse to a dict of column values."""
    row: dict = {
        "url_hash": _url_hash(response.url),
        "url": response.url,
        "domain": _extract_domain(response.url),
        "status": response.status,
    }

    if response.crawl_metadata:
        row["http_status"] = response.crawl_metadata.http_status
        row["response_time_ms"] = response.crawl_metadata.response_time_ms
        row["content_length"] = response.crawl_metadata.content_length
        if response.crawl_metadata.fetched_at:
            row["fetched_at"] = datetime.fromisoformat(response.crawl_metadata.fetched_at)

    if response.metadata:
        row["title"] = response.metadata.title
        row["description"] = response.metadata.description
        row["body"] = response.metadata.body
        row["canonical_url"] = response.metadata.canonical_url
        row["language"] = response.metadata.language
        row["og_tags"] = response.metadata.og_tags

    if response.classification:
        row["classification_mode"] = response.classification.mode
        row["topics"] = response.classification.topics
        row["keywords"] = response.classification.keywords
        row["entities"] = response.classification.entities
        row["category"] = response.classification.category
        row["warnings"] = response.classification.warnings

    if response.error:
        row["error_code"] = response.error.code
        row["error_message"] = response.error.message

    row["updated_at"] = datetime.now(timezone.utc)
    return row


def _row_to_dict(row: CrawlResult) -> dict:
    """Convert a CrawlResult ORM object to a plain dict."""
    return {
        "id": row.id,
        "url_hash": row.url_hash,
        "url": row.url,
        "domain": row.domain,
        "status": row.status,
        "http_status": row.http_status,
        "title": row.title,
        "description": row.description,
        "body": row.body,
        "canonical_url": row.canonical_url,
        "language": row.language,
        "og_tags": row.og_tags,
        "classification_mode": row.classification_mode,
        "topics": row.topics,
        "keywords": row.keywords,
        "entities": row.entities,
        "category": row.category,
        "warnings": row.warnings,
        "error_code": row.error_code,
        "error_message": row.error_message,
        "fetched_at": row.fetched_at.isoformat() if row.fetched_at else None,
        "response_time_ms": row.response_time_ms,
        "content_length": row.content_length,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


class CrawlRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]):
        self._session_factory = session_factory

    async def save(self, response: CrawlResponse) -> None:
        """Upsert a crawl result by url_hash."""
        row = _response_to_row(response)
        async with self._session_factory() as session:
            async with session.begin():
                stmt = pg_insert(CrawlResult).values(**row)
                update_cols = {k: v for k, v in row.items() if k != "url_hash"}
                stmt = stmt.on_conflict_do_update(
                    index_elements=["url_hash"],
                    set_=update_cols,
                )
                await session.execute(stmt)

    async def save_generic(self, response: CrawlResponse) -> None:
        """Save a crawl result using generic SQL (for SQLite/testing)."""
        row = _response_to_row(response)
        async with self._session_factory() as session:
            async with session.begin():
                existing = await session.execute(
                    select(CrawlResult).where(CrawlResult.url_hash == row["url_hash"])
                )
                obj = existing.scalar_one_or_none()
                if obj:
                    for k, v in row.items():
                        if k != "url_hash":
                            setattr(obj, k, v)
                else:
                    session.add(CrawlResult(**row))

    async def get_by_url_hash(self, url_hash: str) -> Optional[dict]:
        """Get a single crawl result by its URL hash."""
        async with self._session_factory() as session:
            result = await session.execute(
                select(CrawlResult).where(CrawlResult.url_hash == url_hash)
            )
            row = result.scalar_one_or_none()
            return _row_to_dict(row) if row else None

    async def get_by_domain(
        self, domain: str, limit: int = 20, offset: int = 0
    ) -> list[dict]:
        """Get crawl results filtered by domain."""
        async with self._session_factory() as session:
            result = await session.execute(
                select(CrawlResult)
                .where(CrawlResult.domain == domain)
                .order_by(CrawlResult.created_at.desc())
                .limit(limit)
                .offset(offset)
            )
            return [_row_to_dict(r) for r in result.scalars().all()]

    async def list_crawls(
        self, limit: int = 20, offset: int = 0, status: Optional[str] = None
    ) -> list[dict]:
        """List crawl results with optional status filter."""
        async with self._session_factory() as session:
            stmt = select(CrawlResult).order_by(CrawlResult.created_at.desc())
            if status:
                stmt = stmt.where(CrawlResult.status == status)
            stmt = stmt.limit(limit).offset(offset)
            result = await session.execute(stmt)
            return [_row_to_dict(r) for r in result.scalars().all()]
