# classifier/ — Content Classification

## Pipeline Flow (`pipeline.py`)

- `classify()` routes to NLP or LLM mode
- NLP mode runs KeyBERT + spaCy in parallel (thread executors), then Wikipedia topics sequentially
- LLM mode calls the configured provider; falls back to NLP on failure

## Modules

- **`chunker.py`** — Splits text >6000 chars into overlapping 5000-char chunks at sentence boundaries
- **`keyword_extractor.py`** — KeyBERT with MMR diversity. Per-chunk extraction merged by highest score
- **`ner_extractor.py`** — spaCy NER (ORG, PERSON, GPE, PRODUCT, EVENT). Blocklist filters noise
- **`topic_extractor.py`** — Wikipedia API lookup for first 5 entities. Filters maintenance categories
- **`llm_classifier.py`** — OpenAI/Anthropic/Gemini integration. Smart text sampling for long content (beginning + middle + end). JSON response parsing with code fence handling
