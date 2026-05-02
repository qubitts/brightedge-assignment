from pydantic import BaseModel, Field, field_validator
from typing import Literal, Optional


ClassificationMode = Literal["nlp", "llm"]
ParserMode = Literal["auto", "trafilatura", "bs4"]


class CrawlOptions(BaseModel):
    max_keywords: int = Field(10, ge=1, le=50, description="Number of keywords to extract")
    timeout_seconds: int = Field(30, ge=1, le=120, description="HTTP fetch timeout in seconds")
    classification_mode: ClassificationMode = Field(
        "nlp",
        description="Classification pipeline: 'nlp' (keywords + entities + Wikipedia topics), 'llm' (full LLM classification)",
    )
    parser_mode: ParserMode = Field(
        "auto",
        description="Body text parser: 'auto' (trafilatura first, bs4 fallback), 'trafilatura', 'bs4'",
    )


class CrawlRequest(BaseModel):
    url: str = Field(..., description="URL to crawl (must start with http:// or https://)")
    options: Optional[CrawlOptions] = Field(None, description="Optional crawl configuration")

    @field_validator("url")
    @classmethod
    def validate_url(cls, v: str) -> str:
        if not v.startswith(("http://", "https://")):
            raise ValueError("URL must start with http:// or https://")
        return v


class BatchCrawlOptions(BaseModel):
    max_keywords: int = Field(10, ge=1, le=50, description="Number of keywords to extract")
    timeout_seconds: int = Field(30, ge=1, le=120, description="HTTP fetch timeout in seconds")
    concurrency: int = Field(5, ge=1, le=50, description="Max concurrent requests")
    classification_mode: ClassificationMode = Field(
        "nlp",
        description="Classification pipeline: 'nlp' or 'llm'",
    )
    parser_mode: ParserMode = Field(
        "auto",
        description="Body text parser: 'auto', 'trafilatura', 'bs4'",
    )


class BatchCrawlRequest(BaseModel):
    urls: list[str] = Field(..., description="List of URLs to crawl (1-50)")
    options: Optional[BatchCrawlOptions] = Field(None, description="Optional batch crawl configuration")

    @field_validator("urls")
    @classmethod
    def validate_urls(cls, v: list[str]) -> list[str]:
        if len(v) == 0:
            raise ValueError("urls must not be empty")
        if len(v) > 50:
            raise ValueError("Maximum 50 URLs per batch")
        for url in v:
            if not url.startswith(("http://", "https://")):
                raise ValueError(f"Invalid URL: {url}")
        return v
