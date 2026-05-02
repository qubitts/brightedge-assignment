# Contributing

## Local Development Setup

### Prerequisites

- Python 3.11+
- Docker and Docker Compose
- ~4 GB RAM (spaCy + KeyBERT models load ~1.5 GB)

### Getting Started

```bash
# 1. Clone and enter the repository
git clone <repo-url> && cd brightedge_assignment

# 2. Create virtual environment
python -m venv .venv && source .venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Download NLP models
python -m spacy download en_core_web_sm

# 5. Copy environment config
cp .env.example .env
# Edit .env: set OPENAI_API_KEY if you want LLM mode (optional)

# 6. Start infrastructure (Redis + PostgreSQL)
docker compose up -d redis postgres

# 7. Run the service
uvicorn src.main:app --host 0.0.0.0 --port 8000 --reload

# 8. Verify it works
curl -X POST http://localhost:8000/crawl \
  -H "Content-Type: application/json" \
  -d '{"url": "https://www.bbc.com/news", "mode": "nlp"}'
```

### Without Docker (minimal mode)

```bash
# Skip step 6. The service works without Redis and PostgreSQL.
# Caching and persistence are disabled; results are returned but not stored.
uvicorn src.main:app --host 0.0.0.0 --port 8000 --reload
```

---

## Running Tests

```bash
# All tests
pytest tests/ -x -q

# With coverage
pytest tests/ --cov=src --cov-report=term-missing

# Specific module
pytest tests/test_classifier.py -v

# Tests don't require Redis/PostgreSQL — they use fakeredis and aiosqlite
```

---

## Common Issues

| Problem | Fix |
|---------|-----|
| `ModuleNotFoundError: spacy` | Run `pip install -r requirements.txt` inside venv |
| spaCy model not found | Run `python -m spacy download en_core_web_sm` |
| Redis connection refused | Start Redis: `docker compose up -d redis` or ignore (graceful degradation) |
| Port 8000 in use | Use `--port 8001` or kill the existing process |
| Out of memory on model load | Ensure ≥ 4 GB RAM available; close other heavy processes |
| LLM mode returns error | Set `OPENAI_API_KEY` in `.env` (or use `mode: "nlp"` instead) |

---

## Repository Structure

```
brightedge_assignment/
├── src/
│   ├── main.py                    # FastAPI app + lifespan (startup/shutdown)
│   ├── config.py                  # Pydantic BaseSettings, all env vars
│   ├── api/
│   │   └── routes.py              # Endpoints + crawl pipeline orchestration
│   ├── crawler/
│   │   └── fetcher.py             # Async HTTP fetcher, retry, streaming
│   ├── parser/
│   │   ├── html_parser.py         # Metadata + body extraction
│   │   └── content_validator.py   # Bot/CAPTCHA detection, length validation
│   ├── classifier/
│   │   ├── pipeline.py            # NLP vs LLM orchestrator
│   │   ├── nlp_classifier.py      # KeyBERT + spaCy + Wikipedia
│   │   └── llm_classifier.py      # Multi-provider LLM integration
│   ├── models/
│   │   ├── request.py             # CrawlRequest, BatchCrawlRequest
│   │   └── response.py            # CrawlResult, ClassificationResult
│   ├── db/
│   │   └── repository.py          # PostgreSQL upsert by url_hash
│   └── cache/
│       └── redis_cache.py         # Redis with graceful degradation
├── tests/
│   ├── fixtures/                  # HTML files for different page types
│   ├── test_fetcher.py
│   ├── test_parser.py
│   ├── test_classifier.py
│   ├── test_routes.py
│   └── ...
├── docs/
│   ├── design.md                  # Architecture and design decisions
│   ├── api_reference.md           # Endpoint documentation
│   └── engineering_plan.md        # PoC-to-production plan
├── docker-compose.yml             # Local dev: app + Redis + PostgreSQL
├── Dockerfile
├── requirements.txt
└── .env.example                   # Template for environment variables
```

---

## Onboarding Guide

### Day 1: Get it running

1. Follow "Getting Started" above
2. Run `pytest tests/ -x -q` — all 88+ tests should pass
3. Crawl a URL manually with curl (see step 8 above)
4. Read `CLAUDE.md` for codebase conventions
5. Read `docs/design.md` for architecture context

### Day 2: Understand the pipeline

1. Trace a request through the code: `routes.py` → `fetcher.py` → `html_parser.py` → `content_validator.py` → `pipeline.py` → `repository.py` → `redis_cache.py`
2. Run a crawl with `mode: "nlp"` and read the logs to see each step
3. Run with `mode: "llm"` (requires API key) and compare output
4. Break something intentionally (stop Redis, use invalid URL) and observe graceful degradation

### Day 3: Make a change

1. Pick a trivial blocker from `docs/production_roadmap.md` Section 6 (e.g., URL normalization)
2. Write the implementation + tests
3. Open a PR, go through CI, get review

---

## Key Concepts

| Concept | Where to Look | Why It Matters |
|---------|---------------|----------------|
| Graceful degradation | `redis_cache.py`, `repository.py` | Service works without Redis/PG — don't add hard dependencies |
| Lazy model loading | `classifier/pipeline.py` | NLP models load on first use — saves memory when unused |
| URL hashing | `repository.py` | Dedup and idempotent upserts are keyed on SHA-256 of normalized URL |
| Dual classification | `pipeline.py` | `mode` param routes to NLP or LLM — both produce same output schema |
| Content validation | `content_validator.py` | Detects bot pages, CAPTCHAs, thin content before classification |

---

## Environment Variables

See `.env.example` for all variables. Key ones:

| Variable | Required | Default | Purpose |
|----------|----------|---------|---------|
| `REDIS_URL` | No | None (cache disabled) | Redis connection string |
| `DATABASE_URL` | No | None (persistence disabled) | PostgreSQL connection string |
| `OPENAI_API_KEY` | No | None (LLM mode unavailable) | OpenAI API key for LLM classification |
| `ANTHROPIC_API_KEY` | No | None | Anthropic API key (alternative LLM provider) |
| `LOG_LEVEL` | No | `INFO` | Logging verbosity |
| `CRAWL_TIMEOUT` | No | `30` | HTTP request timeout in seconds |
| `MAX_RETRIES` | No | `3` | Retry count for failed fetches |
| `CACHE_TTL` | No | `3600` | Cache expiry in seconds |
