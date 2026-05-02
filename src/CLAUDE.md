# src/ — Application Source

## Module Map

- **`main.py`** — FastAPI app entry point. Lifespan manages startup (fetcher, cache, DB, NLP models) and shutdown (cleanup).
- **`config.py`** — Single `Settings` class using Pydantic BaseSettings. All config from env vars / `.env` file.
- **`api/`** — HTTP route handlers. `routes.py` contains the full crawl pipeline orchestration in `_crawl_single()`.
- **`models/`** — Pydantic request/response schemas. `Literal` types for classification_mode and parser_mode.
- **`crawler/`** — `PageFetcher` (httpx async client with streaming, retry, size limits). `FetchError` with error codes.
- **`parser/`** — `html_parser.py` extracts metadata + body text. `content_validator.py` detects bot walls and empty pages.
- **`classifier/`** — NLP pipeline (KeyBERT, spaCy, Wikipedia) and LLM pipeline (OpenAI/Anthropic/Gemini). Chunking for long texts.
- **`db/`** — SQLAlchemy async ORM. PostgreSQL with upsert via `ON CONFLICT`. Graceful degradation if DB unavailable.
- **`cache/`** — Redis cache with SHA-256 URL hashing. Graceful degradation if Redis unavailable.

## Patterns

- CPU-bound NLP work (KeyBERT, spaCy) runs in thread executors via `asyncio.get_running_loop().run_in_executor()`
- NLP models are lazy-loaded singletons (`_keybert_model`, `_nlp_model` globals)
- LLM classification falls back to NLP pipeline on failure
- Long texts are chunked with overlap for per-chunk processing (see `classifier/chunker.py`)
