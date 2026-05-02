# tests/ — Test Suite

## Running Tests

```bash
pytest tests/ -x -q          # quick run, stop on first failure
pytest tests/ -v --cov=src   # verbose with coverage
```

## Structure

- **`conftest.py`** — Shared fixtures (FastAPI test client, mock fetcher, mock cache, mock DB)
- **`test_api.py`** — API endpoint integration tests (crawl, batch, health, list, get)
- **`test_fetcher.py`** — HTTP fetcher tests (success, retries, timeouts, streaming, size limits)
- **`test_parser.py`** — HTML parsing tests (metadata extraction, body parsing, parser modes)
- **`test_content_validator.py`** — Bot detection and empty content validation
- **`test_classifier.py`** — NLP pipeline tests (keywords, entities, topics, chunking, LLM fallback)
- **`test_cache.py`** — Redis cache tests (connect, get/set, graceful degradation)
- **`test_db.py`** — Database repository tests (save, upsert, query by domain/hash)
- **`fixtures/`** — HTML files: `article.html`, `blog.html`, `product.html`, `empty.html`, `captcha.html`, `malformed.html`

## Conventions

- All async tests use `pytest-asyncio` with `asyncio_mode = "auto"`
- HTTP mocking via `respx`
- Redis mocking via `fakeredis`
- DB tests use `aiosqlite` (SQLite async) instead of PostgreSQL
