import pytest
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import create_async_engine

from src.db.database import Base, create_session_factory, init_db
from src.db.repository import CrawlRepository, _url_hash
from src.models.response import (
    CrawlResponse,
    CrawlMetadata,
    CrawlResponseMetadata,
    ClassificationResult,
    ErrorDetail,
)


@pytest.fixture
async def repo():
    """Create an in-memory SQLite database and repository for testing."""
    engine = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = create_session_factory(engine)
    repository = CrawlRepository(session_factory)
    yield repository
    await engine.dispose()


def _make_response(url: str = "https://example.com/page", status: str = "success") -> CrawlResponse:
    """Build a sample CrawlResponse for tests."""
    return CrawlResponse(
        url=url,
        status=status,
        metadata=CrawlMetadata(
            title="Test Page",
            description="A test page",
            body="Some body text for testing.",
            canonical_url=url,
            language="en",
            og_tags={"og_title": "Test"},
        ),
        classification=ClassificationResult(
            mode="nlp",
            topics=["technology"],
            keywords=["test"],
            entities=["Example"],
            category="Technology",
        ),
        crawl_metadata=CrawlResponseMetadata(
            fetched_at=datetime.now(timezone.utc).isoformat(),
            response_time_ms=123.4,
            content_length=1024,
            http_status=200,
        ),
    )


class TestCrawlRepository:
    @pytest.mark.asyncio
    async def test_save_and_get_by_url_hash(self, repo):
        response = _make_response()
        await repo.save_generic(response)

        url_hash = _url_hash("https://example.com/page")
        result = await repo.get_by_url_hash(url_hash)
        assert result is not None
        assert result["url"] == "https://example.com/page"
        assert result["status"] == "success"
        assert result["title"] == "Test Page"
        assert result["domain"] == "example.com"
        assert result["keywords"] == ["test"]

    @pytest.mark.asyncio
    async def test_get_by_url_hash_not_found(self, repo):
        result = await repo.get_by_url_hash("nonexistent")
        assert result is None

    @pytest.mark.asyncio
    async def test_upsert_updates_existing(self, repo):
        response = _make_response()
        await repo.save_generic(response)

        updated = _make_response()
        updated.metadata.title = "Updated Title"
        await repo.save_generic(updated)

        url_hash = _url_hash("https://example.com/page")
        result = await repo.get_by_url_hash(url_hash)
        assert result["title"] == "Updated Title"

    @pytest.mark.asyncio
    async def test_get_by_domain(self, repo):
        await repo.save_generic(_make_response("https://example.com/page1"))
        await repo.save_generic(_make_response("https://example.com/page2"))
        await repo.save_generic(_make_response("https://other.com/page1"))

        results = await repo.get_by_domain("example.com")
        assert len(results) == 2
        assert all(r["domain"] == "example.com" for r in results)

    @pytest.mark.asyncio
    async def test_list_crawls(self, repo):
        await repo.save_generic(_make_response("https://example.com/p1"))
        await repo.save_generic(_make_response("https://example.com/p2"))
        await repo.save_generic(
            CrawlResponse(
                url="https://example.com/p3",
                status="error",
                error=ErrorDetail(code="FETCH_ERROR", message="timeout", retryable=True),
            )
        )

        all_results = await repo.list_crawls()
        assert len(all_results) == 3

        success_only = await repo.list_crawls(status="success")
        assert len(success_only) == 2

        error_only = await repo.list_crawls(status="error")
        assert len(error_only) == 1

    @pytest.mark.asyncio
    async def test_list_crawls_pagination(self, repo):
        for i in range(5):
            await repo.save_generic(_make_response(f"https://example.com/p{i}"))

        page1 = await repo.list_crawls(limit=2, offset=0)
        assert len(page1) == 2

        page2 = await repo.list_crawls(limit=2, offset=2)
        assert len(page2) == 2

        page3 = await repo.list_crawls(limit=2, offset=4)
        assert len(page3) == 1

    @pytest.mark.asyncio
    async def test_save_error_response(self, repo):
        response = CrawlResponse(
            url="https://example.com/fail",
            status="error",
            error=ErrorDetail(code="FETCH_TIMEOUT", message="Request timed out", retryable=True),
        )
        await repo.save_generic(response)

        url_hash = _url_hash("https://example.com/fail")
        result = await repo.get_by_url_hash(url_hash)
        assert result is not None
        assert result["status"] == "error"
        assert result["error_code"] == "FETCH_TIMEOUT"
        assert result["error_message"] == "Request timed out"
