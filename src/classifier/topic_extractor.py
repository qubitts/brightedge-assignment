import logging
import re

import wikipediaapi

logger = logging.getLogger(__name__)

MAX_TOPICS = 5

# Patterns that indicate Wikipedia maintenance/meta categories, not real topics
_NOISE_PATTERNS = [
    r"^Articles ",
    r"^All articles ",
    r"^All Wikipedia ",
    r"^Wikipedia ",
    r"^Pages ",
    r"^Use (mdy|dmy|American|British) ",
    r"^CS1 ",
    r"^Webarchive ",
    r"^Commons ",
    r"^Short description",
    r"^Coordinates ",
    r"^Infobox ",
    r"containing potentially dated statements",
    r"articles in need of updating",
    r"articles written in American English",
    r"semi-protected",
    r"move-protected",
    r"indefinitely",
    r"\bdisambigu",
    r"hCards",
    r"wikidata",
    r"Wikidata",
    r"Sister project",
    r"maint:",
    r"sources \(",
    r"Module:",
    r"Phonos extension",
    r"formatnum",
    r"recorded pronunciations",
    r"gadget ",
    r"\b\d{4} births\b",
    r"\b\d{4} deaths\b",
    r"Living people",
]

_NOISE_RE = re.compile("|".join(_NOISE_PATTERNS), re.IGNORECASE)


def _is_real_topic(category: str) -> bool:
    """Return True if the category represents a meaningful topic."""
    return not _NOISE_RE.search(category)


def get_topics(entities: list[str]) -> list[str]:
    """Extract topics from Wikipedia categories for the given entities.

    Looks up the first 5 entities on Wikipedia and collects their categories.
    Filters out Wikipedia maintenance/meta categories and caps at MAX_TOPICS.
    Returns a deduplicated list of meaningful category names.
    """
    wiki = wikipediaapi.Wikipedia(
        user_agent="BrightEdgeCrawler/1.0",
        language="en",
    )

    topics: set[str] = set()
    for ent in entities[:5]:
        try:
            page = wiki.page(ent)
            if page.exists():
                for cat in page.categories.keys():
                    clean = cat.replace("Category:", "").strip()
                    if clean and _is_real_topic(clean):
                        topics.add(clean)
        except Exception as e:
            logger.debug(f"Wikipedia lookup failed for '{ent}': {e}")
            continue

    return sorted(topics)[:MAX_TOPICS]
