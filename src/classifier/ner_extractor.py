import logging
from src.classifier.chunker import chunk_text

logger = logging.getLogger(__name__)

_nlp_model = None

ENTITY_TYPES = {"ORG", "PERSON", "GPE", "PRODUCT", "EVENT"}

# Generic terms that spaCy often mislabels as entities
BLOCKLIST = {
    "the", "this", "that", "which", "who", "what", "where", "when",
    "http", "https", "www", "html", "css", "json", "xml", "api",
    "image", "video", "audio", "file", "page", "section", "article",
    "click", "here", "read", "more", "next", "previous", "home",
}

# Tech concepts that spaCy often mislabels as PERSON
TECH_PERSON_BLOCKLIST = {
    "machine learning", "deep learning", "artificial intelligence",
    "natural language processing", "neural network", "transformer",
    "reinforcement learning", "computer vision", "nlp", "ai", "ml",
}

MAX_ENTITIES = 30


def get_spacy_model():
    global _nlp_model
    if _nlp_model is None:
        import spacy
        logger.info("Loading spaCy model...")
        _nlp_model = spacy.load("en_core_web_sm")
        logger.info("spaCy model loaded")
    return _nlp_model


def _extract_from_chunk(text: str) -> list[tuple[str, str]]:
    """Run spaCy NER on a single chunk, returning (text, label) tuples."""
    nlp = get_spacy_model()
    doc = nlp(text)
    results = []
    for ent in doc.ents:
        if ent.label_ in ENTITY_TYPES and ent.text.strip():
            results.append((ent.text.strip(), ent.label_))
    return results


def _is_valid_entity(text: str, label: str) -> bool:
    """Filter out noisy entities."""
    if len(text) <= 2:
        return False
    if text.lower() in BLOCKLIST:
        return False
    if label == "PERSON" and text.lower() in TECH_PERSON_BLOCKLIST:
        return False
    return True


def extract_entities(text: str) -> list[str]:
    """Extract named entities using spaCy.

    Long texts are chunked, entities extracted per chunk, then
    deduplicated case-insensitively and filtered for noise.
    Returns a list of entity text strings, capped at MAX_ENTITIES.
    """
    if not text or len(text.strip()) < 10:
        return []

    try:
        chunks = chunk_text(text)

        seen: set[str] = set()
        entities: list[str] = []

        for chunk in chunks:
            raw = _extract_from_chunk(chunk)
            for ent_text, ent_label in raw:
                if not _is_valid_entity(ent_text, ent_label):
                    continue
                key = ent_text.lower()
                if key not in seen:
                    seen.add(key)
                    entities.append(ent_text)

        return entities[:MAX_ENTITIES]
    except Exception as e:
        logger.error(f"spaCy NER failed: {e}")
        return []
