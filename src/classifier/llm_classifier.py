import json
import logging
from typing import Optional

from src.config import settings
from src.classifier.chunker import chunk_text

logger = logging.getLogger(__name__)

# LLM context window budget in chars (~4K tokens)
LLM_MAX_TEXT_LENGTH = 12000


def _prepare_text_for_llm(text: str) -> str:
    """Prepare text for LLM by summarizing chunks if too long.

    If text fits in the LLM context budget, use it as-is.
    If not, take the first and last chunks to capture intro + conclusion,
    plus a middle sample for breadth.
    """
    if len(text) <= LLM_MAX_TEXT_LENGTH:
        return text

    chunks = chunk_text(text)
    if len(chunks) <= 2:
        return text[:LLM_MAX_TEXT_LENGTH]

    # Budget per section
    budget = LLM_MAX_TEXT_LENGTH // 3

    first = chunks[0][:budget]
    middle = chunks[len(chunks) // 2][:budget]
    last = chunks[-1][:budget]

    return (
        f"[Beginning of page]\n{first}\n\n"
        f"[Middle of page]\n{middle}\n\n"
        f"[End of page]\n{last}"
    )


def _build_prompt(text: str) -> str:
    prepared = _prepare_text_for_llm(text)

    return f"""Analyze the following web page content and classify it.

Return a JSON object with exactly these fields:
- "topics": array of strings (top 3-5 topic names)
- "keywords": array of strings (top 10 keywords)
- "entities": array of strings (named entities: organizations, people, places, products, events)
- "category": single best category path as a string (e.g., "Technology/AI")

Content:
{prepared}

Return ONLY valid JSON, no other text."""


async def classify_with_llm(text: str) -> Optional[dict]:
    """Classify text using LLM (OpenAI, Anthropic, or Gemini).

    Returns dict with topics, keywords, entities, category or None on failure.
    """
    prompt = _build_prompt(text)

    try:
        if settings.LLM_PROVIDER == "openai":
            return await _call_openai(prompt)
        elif settings.LLM_PROVIDER == "anthropic":
            return await _call_anthropic(prompt)
        elif settings.LLM_PROVIDER == "gemini":
            return await _call_gemini(prompt)
        else:
            logger.error(f"Unknown LLM provider: {settings.LLM_PROVIDER}")
            return None
    except Exception as e:
        logger.error(f"LLM classification failed: {e}")
        return None


async def _call_openai(prompt: str) -> Optional[dict]:
    from openai import AsyncOpenAI

    if not settings.OPENAI_API_KEY:
        logger.error("OPENAI_API_KEY not set")
        return None

    client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)

    for attempt in range(2):
        try:
            response = await client.chat.completions.create(
                model=settings.LLM_MODEL,
                messages=[
                    {"role": "system", "content": "You are a content classification assistant. Always respond with valid JSON only."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.1,
                response_format={"type": "json_object"},
            )

            content = response.choices[0].message.content
            if content:
                return _parse_llm_response(content)
            return None

        except Exception as e:
            logger.warning(f"OpenAI attempt {attempt + 1} failed: {e}")
            if attempt == 1:
                raise

    return None


async def _call_anthropic(prompt: str) -> Optional[dict]:
    from anthropic import AsyncAnthropic

    if not settings.ANTHROPIC_API_KEY:
        logger.error("ANTHROPIC_API_KEY not set")
        return None

    client = AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)

    for attempt in range(2):
        try:
            response = await client.messages.create(
                model=settings.LLM_MODEL,
                max_tokens=1024,
                messages=[
                    {"role": "user", "content": prompt},
                ],
                system="You are a content classification assistant. Always respond with valid JSON only.",
            )

            content = response.content[0].text
            if content:
                return _parse_llm_response(content)
            return None

        except Exception as e:
            logger.warning(f"Anthropic attempt {attempt + 1} failed: {e}")
            if attempt == 1:
                raise

    return None


async def _call_gemini(prompt: str) -> Optional[dict]:
    from google import genai
    from google.genai import types

    if not settings.GEMINI_API_KEY:
        logger.error("GEMINI_API_KEY not set")
        return None

    client = genai.Client(api_key=settings.GEMINI_API_KEY)

    for attempt in range(2):
        try:
            response = await client.aio.models.generate_content(
                model=settings.GEMINI_MODEL,
                contents=prompt,
                config=types.GenerateContentConfig(
                    temperature=0.1,
                    response_mime_type="application/json",
                ),
            )

            content = response.text
            if content:
                return _parse_llm_response(content)
            return None

        except Exception as e:
            logger.warning(f"Gemini attempt {attempt + 1} failed: {e}")
            if attempt == 1:
                raise

    return None


def _parse_llm_response(content: str) -> Optional[dict]:
    """Parse and validate LLM JSON response."""
    try:
        # Try to extract JSON from response
        content = content.strip()
        if content.startswith("```"):
            # Remove code fences
            lines = content.split("\n")
            content = "\n".join(
                line for line in lines
                if not line.strip().startswith("```")
            )

        data = json.loads(content)

        # Validate structure — all list fields should be plain strings
        result = {
            "topics": [],
            "keywords": [],
            "entities": [],
            "category": None,
        }

        if "topics" in data and isinstance(data["topics"], list):
            result["topics"] = [
                str(t) if isinstance(t, str) else str(t.get("topic", ""))
                for t in data["topics"]
                if t
            ]

        if "keywords" in data and isinstance(data["keywords"], list):
            result["keywords"] = [
                str(k) if isinstance(k, str) else str(k.get("keyword", ""))
                for k in data["keywords"]
                if k
            ]

        if "entities" in data and isinstance(data["entities"], list):
            result["entities"] = [
                str(e) if isinstance(e, str) else str(e.get("text", ""))
                for e in data["entities"]
                if e
            ]

        if "category" in data:
            result["category"] = str(data["category"])

        return result

    except (json.JSONDecodeError, ValueError) as e:
        logger.error(f"Failed to parse LLM response: {e}")
        return None
