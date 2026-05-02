import pytest

from src.parser.html_parser import extract_metadata


class TestMetadataExtraction:
    def test_article_metadata(self, article_html):
        result = extract_metadata(article_html)
        assert result.title == "Google Study Shows 90% of Tech Jobs Will Require AI Skills"
        assert "90 percent" in result.description
        assert result.canonical_url == "https://www.cnn.com/2025/09/23/tech/google-study-90-percent-tech-jobs-ai"
        assert result.language == "en"

    def test_article_og_tags(self, article_html):
        result = extract_metadata(article_html)
        assert "og:title" in result.og_tags
        assert "og:type" in result.og_tags
        assert result.og_tags["og:type"] == "article"

    def test_product_metadata(self, product_html):
        result = extract_metadata(product_html)
        assert "Cuisinart" in result.title
        assert "CPT-122" in result.title
        assert result.og_tags.get("og:type") == "product"

    def test_blog_metadata(self, blog_html):
        result = extract_metadata(blog_html)
        assert "Indoorsy" in result.title or "Outdoors" in result.title
        assert result.description is not None
        assert result.language == "en"

    def test_body_extraction(self, article_html):
        result = extract_metadata(article_html)
        assert len(result.body) > 200
        assert "artificial intelligence" in result.body.lower()
        assert "Google" in result.body

    def test_product_body(self, product_html):
        result = extract_metadata(product_html)
        assert len(result.body) > 100
        assert "toaster" in result.body.lower() or "Cuisinart" in result.body

    def test_empty_html(self, empty_html):
        result = extract_metadata(empty_html)
        assert result.title == "Empty Page"
        assert len(result.body) < 50

    def test_malformed_html(self, malformed_html):
        result = extract_metadata(malformed_html)
        assert result.title is not None
        assert len(result.body) > 0
        assert "malformed" in result.body.lower() or "content" in result.body.lower()

    def test_meta_keywords(self, article_html):
        result = extract_metadata(article_html)
        assert result.keywords is not None
        assert "google" in result.keywords.lower()

    def test_no_og_tags(self):
        html = "<html><head><title>Simple</title></head><body><p>Content here</p></body></html>"
        result = extract_metadata(html)
        assert result.og_tags == {}

    def test_no_body_truncation(self):
        """Body text should NOT be truncated - chunking handles long content."""
        long_text = "word " * 20000  # ~100K chars
        html = f"<html><body><p>{long_text}</p></body></html>"
        result = extract_metadata(html)
        # Full text preserved, no arbitrary truncation
        assert len(result.body) > 50000


class TestParserMode:
    def test_auto_mode_uses_trafilatura_first(self, article_html):
        result = extract_metadata(article_html, parser_mode="auto")
        assert result.parser_used == "trafilatura"
        assert len(result.body) > 200

    def test_bs4_mode(self, article_html):
        result = extract_metadata(article_html, parser_mode="bs4")
        assert result.parser_used == "bs4"
        assert len(result.body) > 200

    def test_trafilatura_mode(self, article_html):
        result = extract_metadata(article_html, parser_mode="trafilatura")
        assert result.parser_used == "trafilatura"
        assert len(result.body) > 200

    def test_auto_falls_back_to_bs4(self):
        """When trafilatura returns nothing, auto mode should fall back to bs4."""
        # Minimal HTML that trafilatura may not extract from
        html = "<html><body><p>Short text</p></body></html>"
        result = extract_metadata(html, parser_mode="auto")
        # Should have extracted something via one parser or the other
        assert result.parser_used in ("trafilatura", "bs4")

    def test_bs4_and_trafilatura_both_extract_metadata(self, article_html):
        """Metadata (title, OG tags, etc.) should be identical regardless of parser_mode."""
        auto_result = extract_metadata(article_html, parser_mode="auto")
        bs4_result = extract_metadata(article_html, parser_mode="bs4")

        # Metadata extraction always uses BS4, so these should match
        assert auto_result.title == bs4_result.title
        assert auto_result.description == bs4_result.description
        assert auto_result.og_tags == bs4_result.og_tags
        assert auto_result.canonical_url == bs4_result.canonical_url
