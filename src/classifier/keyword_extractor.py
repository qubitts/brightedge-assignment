import logging
from src.classifier.chunker import chunk_text

logger = logging.getLogger(__name__)

_keybert_model = None


def get_keybert_model():
    global _keybert_model
    if _keybert_model is None:
        from keybert import KeyBERT
        logger.info("Loading KeyBERT model...")
        _keybert_model = KeyBERT(model="all-MiniLM-L6-v2")
        logger.info("KeyBERT model loaded")
    return _keybert_model


def _extract_from_chunk(
    text: str,
    top_n: int = 10,
    ngram_range: tuple[int, int] = (1, 3),
) -> list[tuple[str, float]]:
    """Run KeyBERT on a single chunk, returning raw (keyword, score) tuples."""
    model = get_keybert_model()
    return model.extract_keywords(
        text,
        keyphrase_ngram_range=ngram_range,
        stop_words="english",
        use_mmr=True,
        diversity=0.5,
        top_n=top_n,
    )


def extract_keywords(
    text: str,
    top_n: int = 10,
    ngram_range: tuple[int, int] = (1, 3),
) -> list[str]:
    """Extract keywords using KeyBERT with MMR diversity.

    Long texts are chunked, keywords extracted per chunk, then merged
    by keeping the highest score for each unique keyword.
    Returns a list of keyword strings sorted by relevance score.
    """
    if not text or len(text.strip()) < 20:
        return []

    try:
        chunks = chunk_text(text)

        if len(chunks) == 1:
            raw = _extract_from_chunk(chunks[0], top_n=top_n, ngram_range=ngram_range)
            return [kw for kw, score in raw]

        # Extract more keywords per chunk, then merge and re-rank
        per_chunk = max(top_n, 15)
        merged: dict[str, float] = {}
        for chunk in chunks:
            raw = _extract_from_chunk(chunk, top_n=per_chunk, ngram_range=ngram_range)
            for kw, score in raw:
                kw_lower = kw.lower()
                if kw_lower not in merged or score > merged[kw_lower]:
                    merged[kw_lower] = score
        # Sort by score and take top_n
        sorted_kws = sorted(merged.items(), key=lambda x: x[1], reverse=True)[:top_n]
        return [kw for kw, score in sorted_kws]

    except Exception as e:
        logger.error(f"KeyBERT extraction failed: {e}")
        return []
