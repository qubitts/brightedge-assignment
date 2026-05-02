import asyncio
import logging
from typing import Optional

from src.classifier.keyword_extractor import extract_keywords
from src.classifier.ner_extractor import extract_entities
from src.classifier.topic_extractor import get_topics
from src.classifier.llm_classifier import classify_with_llm
from src.models.response import ClassificationResult

logger = logging.getLogger(__name__)


async def run_nlp_pipeline(text: str, max_keywords: int = 10) -> dict:
    """Run the NLP pipeline (KeyBERT + spaCy NER + Wikipedia topics)."""
    loop = asyncio.get_running_loop()

    # Run KeyBERT and spaCy in parallel using thread pool (they're CPU-bound)
    keywords_future = loop.run_in_executor(None, extract_keywords, text, max_keywords)
    entities_future = loop.run_in_executor(None, extract_entities, text)

    keywords, entities = await asyncio.gather(keywords_future, entities_future)

    # Extract topics from Wikipedia based on discovered entities
    topics = await loop.run_in_executor(None, get_topics, entities)

    return {
        "keywords": keywords,
        "entities": entities,
        "topics": topics,
    }


async def classify(
    text: str,
    mode: str = "nlp",
    max_keywords: int = 10,
) -> ClassificationResult:
    """Run classification pipeline based on selected mode."""
    warnings = []

    if mode == "nlp":
        result = await run_nlp_pipeline(text, max_keywords)
        return _build_result("nlp", result, warnings)

    elif mode == "llm":
        try:
            llm_result = await classify_with_llm(text)
            if llm_result:
                return _build_result("llm", llm_result, warnings)
        except Exception as e:
            logger.warning(f"LLM classification failed, falling back to NLP: {e}")
            warnings.append(f"LLM classification failed ({e}), fell back to NLP")

        # Fallback to NLP
        result = await run_nlp_pipeline(text, max_keywords)
        warnings.append("LLM unavailable, used NLP fallback")
        return _build_result("nlp", result, warnings)

    else:
        raise ValueError(f"Unknown classification mode: {mode}")


def _build_result(mode: str, data: dict, warnings: list[str]) -> ClassificationResult:
    return ClassificationResult(
        mode=mode,
        topics=data.get("topics", []),
        keywords=data.get("keywords", []),
        entities=data.get("entities", []),
        category=data.get("category"),
        warnings=warnings,
    )
