import asyncio
import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Query, Request

from src.config import settings
from src.models.request import CrawlRequest, BatchCrawlRequest
from src.models.response import (
    CrawlResponse,
    BatchCrawlResponse,
    HealthResponse,
    CrawlMetadata,
    CrawlResponseMetadata,
    ClassificationResult,
    ErrorDetail,
)
from src.crawler.fetcher import PageFetcher, FetchError
from src.parser.html_parser import extract_metadata
from src.parser.content_validator import validate_content
from src.classifier.pipeline import classify

logger = logging.getLogger(__name__)

router = APIRouter()


async def _crawl_single(
    url: str,
    fetcher: PageFetcher,
    cache,
    db=None,
    max_keywords: int = 10,
    timeout_seconds: int = 30,
    classification_mode: str = "nlp",
    parser_mode: str = "auto",
) -> CrawlResponse:
    """Execute single URL crawl pipeline: cache -> fetch -> parse -> validate -> classify -> cache."""

    # 1. Check cache
    if cache:
        cached = await cache.get(url)
        if cached:
            logger.info(f"Cache hit for {url}")
            return CrawlResponse(**cached)

    # 2. Fetch
    try:
        fetch_result = await fetcher.fetch(url, timeout=timeout_seconds)
    except FetchError as e:
        if e.code == "RATE_LIMITED":
            status = "rate_limited"
        elif e.code == "BLOCKED":
            status = "blocked"
        else:
            status = "failed"
        return CrawlResponse(
            url=url,
            status=status,
            error=ErrorDetail(
                code=e.code,
                message=e.message,
                retryable=e.retryable,
            ),
        )

    # 3. Parse HTML
    try:
        parse_result = extract_metadata(fetch_result.body, parser_mode=parser_mode)
    except Exception as e:
        logger.error(f"Parse error for {url}: {e}")
        return CrawlResponse(
            url=url,
            status="failed",
            error=ErrorDetail(
                code="PARSE_ERROR",
                message=f"HTML parsing failed: {str(e)}",
                retryable=False,
            ),
            crawl_metadata=CrawlResponseMetadata(
                fetched_at=datetime.now(timezone.utc).isoformat(),
                response_time_ms=fetch_result.response_time_ms,
                content_length=len(fetch_result.body),
                http_status=fetch_result.status_code,
            ),
        )

    # Build metadata
    metadata = CrawlMetadata(
        title=parse_result.title,
        description=parse_result.description,
        body=parse_result.body,
        canonical_url=parse_result.canonical_url,
        language=parse_result.language,
        og_tags=parse_result.og_tags or None,
    )

    crawl_meta = CrawlResponseMetadata(
        fetched_at=datetime.now(timezone.utc).isoformat(),
        response_time_ms=fetch_result.response_time_ms,
        content_length=len(fetch_result.body),
        http_status=fetch_result.status_code,
    )

    # 4. Validate content (bot detection / empty content gate)
    validation = validate_content(parse_result)
    if not validation.is_valid:
        error_code = validation.reason or "EMPTY_CONTENT"
        if error_code == "BLOCKED":
            status = "blocked"
        else:
            status = "failed"
        return CrawlResponse(
            url=url,
            status=status,
            metadata=metadata,
            error=ErrorDetail(
                code=error_code,
                message=f"Content validation failed: {validation.reason}",
                retryable=error_code == "BLOCKED",
            ),
            crawl_metadata=crawl_meta,
        )

    # 5. Classify
    try:
        classification = await classify(
            parse_result.body,
            mode=classification_mode,
            max_keywords=max_keywords,
        )
    except Exception as e:
        logger.error(f"Classification error for {url}: {e}")
        classification = ClassificationResult(
            mode=classification_mode,
            warnings=[f"Classification failed: {str(e)}"],
        )

    # 6. Build response
    response = CrawlResponse(
        url=url,
        status="success",
        metadata=metadata,
        classification=classification,
        crawl_metadata=crawl_meta,
    )

    # 7. Cache result
    if cache:
        try:
            await cache.set(url, response.model_dump())
        except Exception as e:
            logger.warning(f"Failed to cache result for {url}: {e}")

    # 8. Persist to database
    if db:
        try:
            await db.save(response)
        except Exception as e:
            logger.warning(f"Failed to persist result for {url}: {e}")

    return response


@router.post(
    "/crawl",
    response_model=CrawlResponse,
    summary="Crawl a single URL",
    description=(
        "Fetches a web page, extracts metadata (title, description, OG tags, body text), "
        "validates content (bot/CAPTCHA detection), and classifies it using the selected pipeline.\n\n"
        "**Parser modes:** `auto` (trafilatura first, bs4 fallback), `trafilatura`, `bs4`.\n\n"
        "**Classification modes:** `nlp` (keywords + entities + Wikipedia topics), "
        "`llm` (full LLM classification).\n\n"
        "Results are cached in Redis (if available) for subsequent requests."
    ),
)
async def crawl(request: CrawlRequest, req: Request):
    """Crawl a single URL and return extracted metadata and classification."""
    if request.options is None:
        from src.models.request import CrawlOptions
        options = CrawlOptions()
    else:
        options = request.options

    fetcher: PageFetcher = req.app.state.fetcher
    cache = req.app.state.cache
    db = getattr(req.app.state, "db", None)

    return await _crawl_single(
        url=request.url,
        fetcher=fetcher,
        cache=cache,
        db=db,
        max_keywords=options.max_keywords,
        timeout_seconds=options.timeout_seconds,
        classification_mode=options.classification_mode,
        parser_mode=options.parser_mode,
    )


@router.post(
    "/crawl/batch",
    response_model=BatchCrawlResponse,
    summary="Crawl multiple URLs",
    description=(
        "Submit up to 50 URLs for concurrent crawling. Each URL goes through the same pipeline "
        "as `POST /crawl`. Concurrency is controlled via the `concurrency` option (default 5). "
        "Returns mixed success/error results per URL."
    ),
)
async def crawl_batch(request: BatchCrawlRequest, req: Request):
    """Crawl multiple URLs concurrently."""
    from src.models.request import BatchCrawlOptions
    options = request.options or BatchCrawlOptions()

    fetcher: PageFetcher = req.app.state.fetcher
    cache = req.app.state.cache
    db = getattr(req.app.state, "db", None)

    concurrency = min(options.concurrency, settings.MAX_CONCURRENCY)
    semaphore = asyncio.Semaphore(concurrency)

    async def crawl_with_limit(url: str) -> CrawlResponse:
        async with semaphore:
            return await _crawl_single(
                url=url,
                fetcher=fetcher,
                cache=cache,
                db=db,
                max_keywords=options.max_keywords,
                timeout_seconds=options.timeout_seconds,
                classification_mode=options.classification_mode,
                parser_mode=options.parser_mode,
            )

    tasks = [crawl_with_limit(url) for url in request.urls]
    results = await asyncio.gather(*tasks)

    batch_id = f"b-{uuid.uuid4().hex[:12]}"

    return BatchCrawlResponse(
        batch_id=batch_id,
        total=len(results),
        results=list(results),
    )


@router.get(
    "/health",
    response_model=HealthResponse,
    summary="Health check",
    description="Returns service status, version, and uptime. Use this to verify the service is running.",
)
async def health(req: Request):
    """Health check endpoint."""
    import time
    startup_time = req.app.state.startup_time
    uptime = time.time() - startup_time

    return HealthResponse(
        status="healthy",
        version="1.0.0",
        uptime_seconds=round(uptime, 2),
    )


@router.get(
    "/crawls",
    summary="List crawl results",
    description="Returns paginated crawl results from PostgreSQL. Supports optional domain and status filters.",
)
async def list_crawls(
    req: Request,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    domain: str | None = Query(None),
    status: str | None = Query(None),
):
    """List stored crawl results."""
    db = getattr(req.app.state, "db", None)
    if not db:
        raise HTTPException(status_code=503, detail="Database not available")

    if domain:
        results = await db.get_by_domain(domain, limit=limit, offset=offset)
    else:
        results = await db.list_crawls(limit=limit, offset=offset, status=status)

    return {"total": len(results), "limit": limit, "offset": offset, "results": results}


@router.get(
    "/crawls/{url_hash}",
    summary="Get crawl result by URL hash",
    description="Returns a single crawl result looked up by its SHA-256 URL hash.",
)
async def get_crawl(url_hash: str, req: Request):
    """Get a single crawl result by URL hash."""
    db = getattr(req.app.state, "db", None)
    if not db:
        raise HTTPException(status_code=503, detail="Database not available")

    result = await db.get_by_url_hash(url_hash)
    if not result:
        raise HTTPException(status_code=404, detail="Crawl result not found")

    return result
