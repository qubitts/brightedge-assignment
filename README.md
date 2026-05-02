# URL Crawler & Content Classification Service

A service that crawls web pages, extracts metadata, and classifies content using NLP or LLM pipelines. Built with FastAPI, Python 3.11+.

- **NLP mode** (free, no API key): KeyBERT keywords + spaCy NER + Wikipedia topics
- **LLM mode** (requires API key): Full classification via OpenAI, Anthropic, or Google Gemini

## Table of Contents

- [Features](#features)
- [Quick Start](#quick-start)
- [API Reference](#api-reference)
- [Configuration](#configuration)
- [Running Tests](#running-tests)
- [Project Structure](#project-structure)
- [Architecture](#architecture)

## Features

- **Single & batch URL crawling** -- one URL or up to 50 concurrently
- **Metadata extraction** -- title, description, OG tags, canonical URL, language
- **Body text extraction** -- via Trafilatura or BeautifulSoup (selectable per request)
- **Keyword extraction** -- semantic keywords via KeyBERT (BERT embeddings + MMR diversity)
- **Named entity recognition** -- organizations, people, locations, products via spaCy
- **Topic extraction** -- Wikipedia category-based topics from discovered entities
- **LLM classification** -- optional OpenAI/Anthropic/Gemini with automatic NLP fallback on failure
- **Content validation** -- detects bot walls, CAPTCHAs, and empty pages before classification
- **Redis caching** -- configurable TTL, gracefully degrades if unavailable
- **PostgreSQL persistence** -- upsert by URL hash, gracefully degrades if unavailable
- **Retry with backoff** -- automatic retries on 5xx, 429, timeouts, and connection errors
- **Streaming fetch** -- 10MB size limit enforced during download, not after

## Quick Start

### Prerequisites

- Python 3.11+
- Redis (optional -- caching disabled without it)
- PostgreSQL (optional -- persistence disabled without it)

### Local Setup

```bash
# Install uv (if not already installed)
curl -LsSf https://astral.sh/uv/install.sh | sh

# Create virtual environment and install dependencies
uv venv
source .venv/bin/activate
uv pip install -e ".[dev]"

# Download spaCy language model
python -m spacy download en_core_web_sm

# Start the server
uvicorn src.main:app --host 0.0.0.0 --port 8000
```

### Docker Setup

```bash
docker-compose up --build
```

Starts the API (port 8000), Redis (6379), and PostgreSQL (5432).

### Verify

```bash
curl http://localhost:8000/health
# {"status": "healthy", "version": "1.0.0", "uptime_seconds": 12.34}
```

API docs: http://localhost:8000/docs (Swagger) | http://localhost:8000/redoc (ReDoc)

---

## API Reference

### `POST /crawl` -- Crawl a Single URL

```bash
# Minimal (NLP mode, auto parser, all defaults)
curl -X POST http://localhost:8000/crawl \
  -H "Content-Type: application/json" \
  -d '{"url": "https://en.wikipedia.org/wiki/Python_(programming_language)"}'

# With all options
curl -X POST http://localhost:8000/crawl \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://en.wikipedia.org/wiki/Python_(programming_language)",
    "options": {
      "max_keywords": 15,
      "timeout_seconds": 60,
      "classification_mode": "llm",
      "parser_mode": "trafilatura"
    }
  }'
```

**Request body:**

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `url` | string | required | HTTP(S) URL to crawl |
| `options.classification_mode` | `"nlp"` \| `"llm"` | `"nlp"` | Classification pipeline |
| `options.parser_mode` | `"auto"` \| `"trafilatura"` \| `"bs4"` | `"auto"` | Body text extraction method |
| `options.max_keywords` | 1-50 | 10 | Number of keywords to extract |
| `options.timeout_seconds` | 1-120 | 30 | HTTP fetch timeout |

**Success response:**

```json
{
  "url": "https://en.wikipedia.org/wiki/Python_(programming_language)",
  "status": "success",
  "metadata": {
    "title": "Python (programming language) - Wikipedia",
    "description": "...",
    "body": "Python is a high-level, general-purpose programming language...",
    "canonical_url": "https://en.wikipedia.org/wiki/Python_(programming_language)",
    "language": "en",
    "og_tags": {"og:title": "...", "og:type": "website"}
  },
  "classification": {
    "mode": "nlp",
    "topics": ["Programming languages", "Software engineering"],
    "keywords": ["python programming", "high-level language", "guido van rossum"],
    "entities": ["Python Software Foundation", "Guido van Rossum"],
    "category": null,
    "warnings": []
  },
  "crawl_metadata": {
    "fetched_at": "2026-05-02T10:30:00+00:00",
    "response_time_ms": 450.23,
    "content_length": 152340,
    "http_status": 200
  }
}
```

**Error response:**

```json
{
  "url": "https://example.com/not-found",
  "status": "failed",
  "error": {"code": "FETCH_CLIENT_ERROR", "message": "Client error: HTTP 404", "retryable": false}
}
```

---

### `POST /crawl/batch` -- Crawl Multiple URLs

```bash
curl -X POST http://localhost:8000/crawl/batch \
  -H "Content-Type: application/json" \
  -d '{
    "urls": [
      "https://en.wikipedia.org/wiki/Python_(programming_language)",
      "https://en.wikipedia.org/wiki/JavaScript",
      "https://en.wikipedia.org/wiki/Rust_(programming_language)"
    ],
    "options": {
      "concurrency": 10,
      "classification_mode": "nlp"
    }
  }'
```

Accepts 1-50 URLs. All `/crawl` options apply, plus `concurrency` (1-50, default 5). Returns mixed success/error results per URL.

**Response:**

```json
{
  "batch_id": "b-a1b2c3d4e5f6",
  "total": 3,
  "results": [
    {"url": "...", "status": "success", "metadata": {}, "classification": {}, "crawl_metadata": {}},
    {"url": "...", "status": "success", "...": "..."},
    {"url": "...", "status": "failed", "error": {"code": "FETCH_TIMEOUT", "retryable": true}}
  ]
}
```

---

### `GET /health`

```bash
curl http://localhost:8000/health
```

Returns `{"status": "healthy", "version": "1.0.0", "uptime_seconds": 3600.5}`.

---

### `GET /crawls` -- List Stored Results (requires PostgreSQL)

```bash
curl "http://localhost:8000/crawls?limit=10&offset=0"
curl "http://localhost:8000/crawls?domain=en.wikipedia.org"
curl "http://localhost:8000/crawls?status=success&limit=50"
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `limit` | 1-100 | 20 | Results per page |
| `offset` | int | 0 | Pagination offset |
| `domain` | string | -- | Filter by domain |
| `status` | string | -- | Filter: `success`, `failed`, `blocked`, `rate_limited` |

Returns `503` if PostgreSQL is not connected.

---

### `GET /crawls/{url_hash}` -- Get Result by URL Hash (requires PostgreSQL)

```bash
# Compute the SHA-256 hash of a URL
echo -n "https://en.wikipedia.org/wiki/Python_(programming_language)" | shasum -a 256

# Look it up
curl http://localhost:8000/crawls/<hash>
```

Returns `404` if not found, `503` if PostgreSQL is not connected.

---

### Error Codes

| Code | Description | Retryable |
|------|-------------|-----------|
| `FETCH_TIMEOUT` | Request timed out | Yes |
| `FETCH_CONNECTION_ERROR` | Could not connect to host | Yes |
| `FETCH_HTTP_ERROR` | Server returned 5xx | Yes |
| `RATE_LIMITED` | HTTP 429 | Yes |
| `BLOCKED` | HTTP 403 or bot wall/CAPTCHA detected | Depends |
| `FETCH_CLIENT_ERROR` | HTTP 4xx (not 403/429) | No |
| `CONTENT_TOO_LARGE` | Response exceeds 10MB | No |
| `EMPTY_CONTENT` | Page has less than 200 chars of text | No |
| `PARSE_ERROR` | HTML parsing failed | No |

---

## Configuration

All settings via environment variables or `.env` file in the project root.

| Variable | Default | Description |
|----------|---------|-------------|
| `REDIS_URL` | `redis://localhost:6379` | Redis connection URL |
| `DATABASE_URL` | `postgresql+asyncpg://postgres:postgres@localhost:5432/brightedge` | PostgreSQL connection URL |
| `CACHE_TTL_SECONDS` | `86400` | Cache TTL (24h) |
| `MAX_CONTENT_SIZE` | `10485760` | Max response body (10MB) |
| `FETCH_TIMEOUT` | `30` | HTTP timeout in seconds |
| `MAX_RETRIES` | `3` | Retry attempts for transient errors |
| `MAX_CONCURRENCY` | `10` | Max concurrent batch crawls |
| `MIN_CONTENT_LENGTH` | `200` | Min text length for content validation |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `CLASSIFICATION_MODE` | `nlp` | Default classification mode |
| `LLM_PROVIDER` | `openai` | `openai`, `anthropic`, or `gemini` |
| `OPENAI_API_KEY` | -- | Required for OpenAI LLM mode |
| `ANTHROPIC_API_KEY` | -- | Required for Anthropic LLM mode |
| `GEMINI_API_KEY` | -- | Required for Gemini LLM mode |
| `LLM_MODEL` | `gpt-4o-mini` | Model name (OpenAI/Anthropic) |
| `GEMINI_MODEL` | `gemini-2.5-flash` | Model name (Gemini) |

NLP mode requires no configuration. For LLM mode, set the provider and its API key:

```env
LLM_PROVIDER=gemini
GEMINI_API_KEY=your-key-here
```

## Running Tests

Tests mock all external dependencies (HTTP, Redis, PostgreSQL). No running services needed.

```bash
source .venv/bin/activate

# All tests
pytest tests/ -v

# With coverage
pytest tests/ --cov=src --cov-report=term-missing

# Single file
pytest tests/test_api.py -v
```

Test files: `test_api.py`, `test_fetcher.py`, `test_parser.py`, `test_content_validator.py`, `test_classifier.py`, `test_cache.py`, `test_db.py`

## Project Structure

```
src/
├── main.py                    # FastAPI app, lifespan (startup/shutdown)
├── config.py                  # Pydantic settings from env vars
├── models/
│   ├── request.py             # CrawlRequest, BatchCrawlRequest
│   └── response.py            # CrawlResponse, ClassificationResult, errors
├── api/
│   └── routes.py              # Endpoints + crawl pipeline orchestration
├── crawler/
│   └── fetcher.py             # Async HTTP fetcher (retry, streaming, size limits)
├── parser/
│   ├── html_parser.py         # Metadata + body extraction (Trafilatura / BS4)
│   └── content_validator.py   # Bot/CAPTCHA detection, content length check
├── classifier/
│   ├── pipeline.py            # NLP vs LLM classification orchestrator
│   ├── chunker.py             # Overlapping text chunking
│   ├── keyword_extractor.py   # KeyBERT keywords
│   ├── ner_extractor.py       # spaCy NER
│   ├── topic_extractor.py     # Wikipedia topics
│   └── llm_classifier.py      # OpenAI / Anthropic / Gemini
├── db/
│   ├── database.py            # SQLAlchemy async engine
│   ├── models.py              # CrawlResult table schema
│   └── repository.py          # CRUD with upsert
└── cache/
    └── redis_cache.py         # Redis cache with graceful degradation
```

## Architecture

See [ARCHITECTURE.md](ARCHITECTURE.md) for internals: request lifecycle, pipeline design, classification details, and design decisions.
