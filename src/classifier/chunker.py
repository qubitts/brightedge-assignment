import logging

logger = logging.getLogger(__name__)

# Chunk size in characters (~2K tokens). Overlap ensures context isn't lost at boundaries.
CHUNK_SIZE = 5000
CHUNK_OVERLAP = 500
CHUNK_THRESHOLD = 6000  # Only chunk if text exceeds this length


def chunk_text(text: str, chunk_size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """Split text into overlapping chunks for processing.

    Short texts (< CHUNK_THRESHOLD) are returned as a single chunk.
    Longer texts are split at sentence boundaries when possible,
    with overlap between chunks to preserve context.
    """
    if not text:
        return []

    if len(text) <= CHUNK_THRESHOLD:
        return [text]

    chunks = []
    start = 0

    while start < len(text):
        end = start + chunk_size

        if end >= len(text):
            chunks.append(text[start:])
            break

        # Try to break at a sentence boundary (look back from the end)
        boundary = _find_sentence_boundary(text, end)
        if boundary > start:
            end = boundary

        chunks.append(text[start:end])
        start = end - overlap  # Step back by overlap amount

    logger.info(f"Split text ({len(text)} chars) into {len(chunks)} chunks")
    return chunks


def _find_sentence_boundary(text: str, position: int) -> int:
    """Find the nearest sentence-ending punctuation before position.

    Looks back up to 500 chars for a sentence boundary.
    Returns position if no good boundary found.
    """
    search_start = max(0, position - 500)
    search_region = text[search_start:position]

    # Look for sentence-ending punctuation followed by space
    for marker in [". ", ".\n", "! ", "!\n", "? ", "?\n"]:
        last_idx = search_region.rfind(marker)
        if last_idx != -1:
            return search_start + last_idx + len(marker)

    return position
