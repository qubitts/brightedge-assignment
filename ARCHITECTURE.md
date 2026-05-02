# Architecture

How the Crawler Service works internally: request lifecycle, component responsibilities, and design decisions.

## System Overview

```
  POST /crawl ──────►  Cache ──► Fetcher ──► Parser ──► Validator ──► Classifier
  POST /crawl/batch ►    │                                                │
                         ▼                                                ▼
                       Redis                                          PostgreSQL
                     (optional)                                       (optional)
```

A request passes through a linear pipeline. Results are optionally cached in Redis and persisted to PostgreSQL. Both storage layers are optional -- the service works without them.

## Request Lifecycle

`_crawl_single()` in `src/api/routes.py` orchestrates the full pipeline:

### 1. Cache Check

Key format: `crawl:{sha256(url)}`. Cache hits return the stored `CrawlResponse` directly, skipping all downstream work. TTL is configurable (default 24 hours).

**File:** `src/cache/redis_cache.py`

### 2. Fetch

The fetcher opens a streaming HTTP connection via httpx, checks `Content-Length` against the 10MB limit, then reads the body in chunks while enforcing the size limit. This prevents downloading oversized pages into memory.

**Retry logic:** Transient failures (5xx, 429, timeout, connection error) retry up to `MAX_RETRIES` times with exponential backoff (1s, 2s, 4s). Non-transient errors (4xx, 403) fail immediately.

The HTTP client uses a Chrome-like User-Agent, follows up to 5 redirects, and uses connection pooling for batch requests.

**File:** `src/crawler/fetcher.py`

### 3. Parse

Parsing has two stages:

1. **Metadata** (always BeautifulSoup): `<title>`, `<meta description>`, `<meta keywords>`, `<link canonical>`, `<html lang>`, and `<meta og:*>` tags.

2. **Body text** (configurable per request):
   - `auto`: Trafilatura first, BS4 fallback if empty
   - `trafilatura`: Purpose-built article extraction, strips boilerplate
   - `bs4`: Removes script/style/nav/footer/aside/header tags, extracts remaining text

**File:** `src/parser/html_parser.py`

### 4. Validate

Runs *before* classification to avoid wasting compute on unusable pages:

1. **Bot detection:** Scans title + body for phrases like "access denied", "robot check", "captcha", "security check". Triggers `BLOCKED` status.
2. **Content length:** Body shorter than `MIN_CONTENT_LENGTH` (200 chars) triggers `EMPTY_CONTENT`.

**File:** `src/parser/content_validator.py`

### 5. Classify

Two modes, selected per request:

#### NLP Mode (default, free)

Three extractors run in parallel via `asyncio.gather`:

```
              ┌──► KeyBERT ──────► keywords
              │
Body text ────┼──► spaCy NER ────► entities
              │
              └──► Wikipedia API ─► topics (from entities)
```

- **KeyBERT** uses BERT embeddings (`all-MiniLM-L6-v2`) with MMR for diverse 1-to-3-word keywords. Long texts are chunked, processed per-chunk, and merged by score.
- **spaCy** (`en_core_web_sm`) extracts ORG, PERSON, GPE, PRODUCT, EVENT entities. Blocklists filter generic terms and common mislabels.
- **Wikipedia** looks up extracted entities, collects page categories, and filters out maintenance categories via regex.

KeyBERT and spaCy are CPU-bound and run in thread executors via `asyncio.run_in_executor()` to avoid blocking the event loop.

**Files:** `src/classifier/keyword_extractor.py`, `src/classifier/ner_extractor.py`, `src/classifier/topic_extractor.py`

#### LLM Mode (requires API key)

Sends text to OpenAI, Anthropic, or Gemini with a structured prompt requesting JSON output (topics, keywords, entities, category path).

If text exceeds ~12,000 characters, it samples beginning, middle, and end sections to preserve context from all parts of the page.

If the LLM call fails for any reason, the classifier automatically falls back to the NLP pipeline and adds a warning to the response.

**Files:** `src/classifier/pipeline.py`, `src/classifier/llm_classifier.py`

### 6. Cache & Persist

Both operations are fire-and-forget -- if either fails, the response is still returned. The database uses `ON CONFLICT url_hash DO UPDATE` so re-crawling a URL updates the existing record.

**Files:** `src/cache/redis_cache.py`, `src/db/repository.py`

## Batch Crawling

`POST /crawl/batch` runs `_crawl_single()` for each URL concurrently, controlled by `asyncio.Semaphore(min(requested_concurrency, MAX_CONCURRENCY))`. Each URL is independent; results can be a mix of successes and failures.

## Text Chunking

Texts over 6000 characters are split into overlapping chunks before keyword and entity extraction:

- Chunk size: 5000 characters (~2K tokens)
- Overlap: 500 characters
- Splits at sentence boundaries when possible
- Results are merged and deduplicated across chunks

**File:** `src/classifier/chunker.py`

## Database Schema

Single table `crawl_results` with columns for the full crawl response: URL identity (`url_hash`, `url`, `domain`), HTTP metadata (`status`, `http_status`, `response_time_ms`, `content_length`, `fetched_at`), parsed content (`title`, `description`, `body`, `canonical_url`, `language`, `og_tags`), classification output (`classification_mode`, `topics`, `keywords`, `entities`, `category`, `warnings`), error info (`error_code`, `error_message`), and timestamps (`created_at`, `updated_at`).

Unique constraint on `url_hash`. Indexes on `domain` and `created_at`.

**Files:** `src/db/models.py`, `src/db/repository.py`

## Application Lifecycle

**Startup** (`src/main.py` lifespan context manager):
1. Create httpx async client (`PageFetcher`)
2. Connect to Redis (graceful degradation on failure)
3. Connect to PostgreSQL + create tables (graceful degradation on failure)
4. Pre-load KeyBERT and spaCy models to avoid first-request latency

**Shutdown:** Close httpx client, Redis connection, and SQLAlchemy engine.

## Design Decisions

**Graceful degradation.** Redis and PostgreSQL are optional. All cache/DB operations are wrapped in try/except. Development needs zero infrastructure; production adds Redis and PostgreSQL.

**Thread executors for CPU work.** KeyBERT and spaCy block the thread. Running them in `asyncio.run_in_executor()` keeps the event loop responsive for concurrent requests.

**Streaming fetch with size enforcement.** Size is checked as chunks arrive, not after the full download. A 100MB page is rejected early without consuming memory.

**LLM fallback to NLP.** LLM APIs can fail (rate limits, outages, bad JSON). The system falls back to the free NLP pipeline and adds a warning. Users always get results.

**Upsert by URL hash.** SHA-256 of the URL is the unique key. Re-crawling updates the existing record instead of creating duplicates.

**Lazy-loaded model singletons.** NLP models load into module-level globals on first use, pre-loaded at startup. Modules can be imported without triggering model loading (useful for tests).

## Component Map

```
routes.py (pipeline orchestration)
├── fetcher.py           HTTP fetch with retry + streaming
├── html_parser.py       Metadata + body extraction
├── content_validator.py Bot detection, content length
├── pipeline.py          Classification orchestrator
│   ├── keyword_extractor.py   KeyBERT
│   ├── ner_extractor.py       spaCy NER
│   ├── topic_extractor.py     Wikipedia topics
│   ├── llm_classifier.py      LLM providers
│   └── chunker.py             Text splitting
├── redis_cache.py       Caching
└── repository.py        Database persistence
    └── database.py      Engine + sessions
```
