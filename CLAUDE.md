# BrightEdge Crawler Service

URL Crawler & Content Classification Service built with FastAPI (Python 3.11+).

## Quick Start

```bash
source .venv/bin/activate
uvicorn src.main:app --host 0.0.0.0 --port 8000
```

## Architecture

```
POST /crawl → Cache Check → Fetch (httpx) → Parse (trafilatura/BS4) → Validate → Classify → DB/Cache → Response
```

**Layers:** `src/api/` → `src/crawler/` → `src/parser/` → `src/classifier/` → `src/db/` + `src/cache/`

## Key Conventions

- **Async-first**: All I/O uses async/await (httpx, asyncpg, redis)
- **Graceful degradation**: Redis and PostgreSQL are optional; the app works without them
- **Config**: All settings via environment variables through `src/config.py` (Pydantic BaseSettings)
- **Models**: Pydantic v2 for request/response validation (`src/models/`)
- **Classification**: Two pipelines — `nlp` (free: KeyBERT + spaCy + Wikipedia) and `llm` (OpenAI/Anthropic/Gemini)
- **NLP models**: Lazy-loaded as singletons via `get_keybert_model()` / `get_spacy_model()`

## Testing

```bash
pytest tests/ -x -q
```

- Framework: pytest + pytest-asyncio (auto mode)
- HTTP mocking: respx
- Redis mocking: fakeredis
- DB testing: aiosqlite (SQLite async)
- Fixtures: `tests/fixtures/` (HTML files for different page types)

## Key Files

| File | Purpose |
|---|---|
| `src/main.py` | FastAPI app + lifespan (startup/shutdown) |
| `src/config.py` | All settings from env vars |
| `src/api/routes.py` | API endpoints and crawl pipeline orchestration |
| `src/crawler/fetcher.py` | Async HTTP fetcher with retry + streaming |
| `src/parser/html_parser.py` | Metadata + body extraction (trafilatura/BS4) |
| `src/parser/content_validator.py` | Bot/CAPTCHA detection + content length validation |
| `src/classifier/pipeline.py` | NLP vs LLM classification orchestrator |
| `src/classifier/llm_classifier.py` | Multi-provider LLM integration |
| `src/db/repository.py` | PostgreSQL persistence (upsert by url_hash) |
| `src/cache/redis_cache.py` | Redis caching with graceful degradation |
