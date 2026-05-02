import pytest
import json
from unittest.mock import patch, AsyncMock, MagicMock

from src.classifier.keyword_extractor import extract_keywords
from src.classifier.ner_extractor import extract_entities
from src.classifier.chunker import chunk_text
from src.classifier.pipeline import classify, run_nlp_pipeline
from src.classifier.llm_classifier import _parse_llm_response, _build_prompt, _prepare_text_for_llm
from src.classifier.topic_extractor import get_topics


SAMPLE_TEXT = """
Google announced a major breakthrough in artificial intelligence research today.
The new machine learning model, developed by DeepMind in London, achieves state-of-the-art
results on natural language processing benchmarks. CEO Sundar Pichai presented the findings
at a conference in San Francisco. The neural network architecture uses transformer technology
to understand and generate human-like text. Microsoft and OpenAI are also competing in this space.
"""


class TestChunker:
    def test_short_text_single_chunk(self):
        chunks = chunk_text("Short text here.")
        assert len(chunks) == 1
        assert chunks[0] == "Short text here."

    def test_empty_text(self):
        chunks = chunk_text("")
        assert chunks == []

    def test_long_text_splits(self):
        long_text = "This is a sentence. " * 500  # ~10K chars
        chunks = chunk_text(long_text)
        assert len(chunks) > 1

    def test_chunks_have_overlap(self):
        long_text = "This is a sentence. " * 500
        chunks = chunk_text(long_text)
        # Adjacent chunks should share some content (overlap)
        for i in range(len(chunks) - 1):
            end_of_current = chunks[i][-100:]
            start_of_next = chunks[i + 1][:100]
            # There should be some common text due to overlap
            assert any(
                word in start_of_next
                for word in end_of_current.split()
                if len(word) > 3
            )

    def test_below_threshold_not_chunked(self):
        text = "word " * 1000  # ~5K chars, below 6K threshold
        chunks = chunk_text(text)
        assert len(chunks) == 1

    def test_sentence_boundary_splitting(self):
        text = ("First sentence here. " * 150) + ("Second part here. " * 150)
        chunks = chunk_text(text)
        # Chunks should end at sentence boundaries when possible
        for chunk in chunks[:-1]:  # Last chunk can end anywhere
            assert chunk.rstrip().endswith(("."))


class TestKeywordExtractor:
    def test_extract_keywords_returns_strings(self):
        keywords = extract_keywords(SAMPLE_TEXT, top_n=5)
        assert len(keywords) > 0
        assert len(keywords) <= 5
        for kw in keywords:
            assert isinstance(kw, str)

    def test_extract_keywords_empty_text(self):
        keywords = extract_keywords("", top_n=5)
        assert keywords == []

    def test_extract_keywords_short_text(self):
        keywords = extract_keywords("hi", top_n=5)
        assert keywords == []

    def test_extract_keywords_default_top_n(self):
        keywords = extract_keywords(SAMPLE_TEXT)
        assert len(keywords) <= 10

    def test_extract_keywords_long_text_chunked(self):
        """Long text should be chunked, keywords merged across chunks."""
        long_text = SAMPLE_TEXT * 30  # ~15K chars, will be chunked
        keywords = extract_keywords(long_text, top_n=5)
        assert len(keywords) > 0
        assert len(keywords) <= 5


class TestNERExtractor:
    def test_extract_entities_returns_strings(self):
        entities = extract_entities(SAMPLE_TEXT)
        assert len(entities) > 0
        for ent in entities:
            assert isinstance(ent, str)

    def test_extract_entities_finds_known_entities(self):
        entities = extract_entities(SAMPLE_TEXT)
        ent_lower = [e.lower() for e in entities]
        # Should find at least one of the prominent entities
        assert any(
            name in " ".join(ent_lower)
            for name in ["google", "deepmind", "pichai", "sundar", "london", "san francisco", "microsoft", "openai"]
        )

    def test_extract_entities_empty_text(self):
        entities = extract_entities("")
        assert entities == []

    def test_extract_entities_deduplication(self):
        text = "Google announced. Google revealed. Google said."
        entities = extract_entities(text)
        google_count = sum(1 for e in entities if e.lower() == "google")
        assert google_count <= 1

    def test_extract_entities_filters_short(self):
        """Entities with <= 2 chars should be filtered out."""
        entities = extract_entities(SAMPLE_TEXT)
        for ent in entities:
            assert len(ent) > 2

    def test_extract_entities_long_text_deduped(self):
        """Entities from chunked long text should be deduplicated."""
        long_text = SAMPLE_TEXT * 30
        entities = extract_entities(long_text)
        seen = set()
        for e in entities:
            key = e.lower()
            assert key not in seen, f"Duplicate entity: {e}"
            seen.add(key)

    def test_extract_entities_capped(self):
        """Should not exceed MAX_ENTITIES."""
        from src.classifier.ner_extractor import MAX_ENTITIES
        long_text = SAMPLE_TEXT * 30
        entities = extract_entities(long_text)
        assert len(entities) <= MAX_ENTITIES


class TestTopicExtractor:
    def test_get_topics_with_mock(self):
        """Test topic extraction with mocked Wikipedia API."""
        mock_page = MagicMock()
        mock_page.exists.return_value = True
        mock_page.categories = {
            "Category:Artificial intelligence": MagicMock(),
            "Category:Machine learning": MagicMock(),
        }

        mock_wiki = MagicMock()
        mock_wiki.page.return_value = mock_page

        with patch("src.classifier.topic_extractor.wikipediaapi") as mock_wikiapi:
            mock_wikiapi.Wikipedia.return_value = mock_wiki
            topics = get_topics(["Artificial intelligence", "Google"])

        assert len(topics) > 0
        assert "Artificial intelligence" in topics or "Machine learning" in topics

    def test_get_topics_empty_entities(self):
        """Should return empty list for no entities."""
        with patch("src.classifier.topic_extractor.wikipediaapi") as mock_wikiapi:
            mock_wiki = MagicMock()
            mock_wikiapi.Wikipedia.return_value = mock_wiki
            topics = get_topics([])
        assert topics == []

    def test_get_topics_no_wikipedia_pages(self):
        """Should return empty list when no pages are found."""
        mock_page = MagicMock()
        mock_page.exists.return_value = False

        mock_wiki = MagicMock()
        mock_wiki.page.return_value = mock_page

        with patch("src.classifier.topic_extractor.wikipediaapi") as mock_wikiapi:
            mock_wikiapi.Wikipedia.return_value = mock_wiki
            topics = get_topics(["NonexistentEntity12345"])
        assert topics == []


class TestLLMResponseParsing:
    def test_parse_valid_response_string_format(self):
        response = json.dumps({
            "topics": ["Technology", "AI"],
            "keywords": ["ai", "machine learning"],
            "entities": ["Google", "DeepMind"],
            "category": "Technology/AI",
        })
        result = _parse_llm_response(response)
        assert result is not None
        assert result["topics"] == ["Technology", "AI"]
        assert result["keywords"] == ["ai", "machine learning"]
        assert result["entities"] == ["Google", "DeepMind"]
        assert result["category"] == "Technology/AI"

    def test_parse_legacy_dict_format(self):
        """Parser should handle LLMs that return old dict format gracefully."""
        response = json.dumps({
            "topics": [{"topic": "Technology/AI", "confidence": 0.95}],
            "keywords": [{"keyword": "ai", "score": 0.9}],
            "entities": [{"text": "Google", "type": "ORG"}],
            "category": "Technology/AI",
        })
        result = _parse_llm_response(response)
        assert result is not None
        assert result["topics"] == ["Technology/AI"]
        assert result["keywords"] == ["ai"]
        assert result["entities"] == ["Google"]
        assert result["category"] == "Technology/AI"

    def test_parse_with_code_fences(self):
        response = '```json\n{"topics": [], "keywords": [], "entities": [], "category": "Test"}\n```'
        result = _parse_llm_response(response)
        assert result is not None
        assert result["category"] == "Test"

    def test_parse_invalid_json(self):
        result = _parse_llm_response("not valid json")
        assert result is None

    def test_build_prompt_basic(self):
        prompt = _build_prompt("Some text content")
        assert "Some text content" in prompt
        assert "topics" in prompt
        assert "keywords" in prompt
        assert "array of strings" in prompt

    def test_prepare_text_short(self):
        """Short text passed through unchanged."""
        text = "Short text"
        assert _prepare_text_for_llm(text) == text

    def test_prepare_text_long_uses_sections(self):
        """Long text should be sampled from beginning, middle, end."""
        long_text = "word " * 10000  # ~50K chars
        prepared = _prepare_text_for_llm(long_text)
        assert len(prepared) <= 15000  # Within budget
        assert "[Beginning of page]" in prepared
        assert "[Middle of page]" in prepared
        assert "[End of page]" in prepared


class TestPipeline:
    @pytest.mark.asyncio
    async def test_nlp_pipeline(self):
        with patch("src.classifier.pipeline.get_topics", return_value=["AI", "Technology"]):
            result = await run_nlp_pipeline(SAMPLE_TEXT, max_keywords=5)
        assert "keywords" in result
        assert "entities" in result
        assert "topics" in result
        assert len(result["keywords"]) > 0
        assert len(result["entities"]) > 0

    @pytest.mark.asyncio
    async def test_nlp_pipeline_returns_topics(self):
        """NLP mode should now return Wikipedia-based topics."""
        with patch("src.classifier.pipeline.get_topics", return_value=["AI", "Technology"]):
            result = await run_nlp_pipeline(SAMPLE_TEXT, max_keywords=5)
        assert "topics" in result
        assert len(result["topics"]) > 0

    @pytest.mark.asyncio
    async def test_classify_nlp_mode(self):
        with patch("src.classifier.pipeline.get_topics", return_value=["AI"]):
            result = await classify(SAMPLE_TEXT, mode="nlp", max_keywords=5)
        assert result.mode == "nlp"
        assert len(result.keywords) > 0
        assert len(result.entities) > 0
        for kw in result.keywords:
            assert isinstance(kw, str)
        for ent in result.entities:
            assert isinstance(ent, str)

    @pytest.mark.asyncio
    async def test_classify_llm_fallback_to_nlp(self):
        with patch("src.classifier.pipeline.classify_with_llm", new_callable=AsyncMock) as mock_llm:
            mock_llm.side_effect = Exception("API unavailable")
            with patch("src.classifier.pipeline.get_topics", return_value=[]):
                result = await classify(SAMPLE_TEXT, mode="llm", max_keywords=5)
            assert result.mode == "nlp"
            assert any("fallback" in w.lower() or "failed" in w.lower() for w in result.warnings)

    @pytest.mark.asyncio
    async def test_classify_llm_success(self):
        mock_result = {
            "topics": ["Technology/AI"],
            "keywords": ["ai"],
            "entities": ["Google"],
            "category": "Technology/AI",
        }
        with patch("src.classifier.pipeline.classify_with_llm", new_callable=AsyncMock) as mock_llm:
            mock_llm.return_value = mock_result
            result = await classify(SAMPLE_TEXT, mode="llm", max_keywords=5)
            assert result.mode == "llm"
            assert result.category == "Technology/AI"
            assert result.topics == ["Technology/AI"]
            assert result.keywords == ["ai"]
            assert result.entities == ["Google"]

    @pytest.mark.asyncio
    async def test_classify_invalid_mode(self):
        with pytest.raises(ValueError, match="Unknown classification mode"):
            await classify(SAMPLE_TEXT, mode="invalid")

    @pytest.mark.asyncio
    async def test_classify_nlp_plus_llm_rejected(self):
        """nlp+llm mode should no longer be supported."""
        with pytest.raises(ValueError, match="Unknown classification mode"):
            await classify(SAMPLE_TEXT, mode="nlp+llm")
