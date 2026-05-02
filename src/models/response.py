from pydantic import BaseModel
from typing import Optional


class ErrorDetail(BaseModel):
    code: str
    message: str
    retryable: bool


class CrawlMetadata(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    body: Optional[str] = None
    canonical_url: Optional[str] = None
    language: Optional[str] = None
    og_tags: Optional[dict[str, str]] = None


class ClassificationResult(BaseModel):
    mode: str
    topics: list[str] = []
    keywords: list[str] = []
    entities: list[str] = []
    category: Optional[str] = None
    warnings: list[str] = []


class CrawlResponseMetadata(BaseModel):
    fetched_at: str
    response_time_ms: float
    content_length: int
    http_status: int


class CrawlResponse(BaseModel):
    url: str
    status: str  # "success", "blocked", "rate_limited", or "failed"
    metadata: Optional[CrawlMetadata] = None
    classification: Optional[ClassificationResult] = None
    crawl_metadata: Optional[CrawlResponseMetadata] = None
    error: Optional[ErrorDetail] = None


class BatchCrawlResponse(BaseModel):
    batch_id: str
    total: int
    results: list[CrawlResponse]


class HealthResponse(BaseModel):
    status: str
    version: str
    uptime_seconds: float
