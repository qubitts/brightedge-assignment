import pytest

from src.parser.html_parser import extract_metadata
from src.parser.content_validator import validate_content


class TestContentValidator:
    def test_valid_article(self, article_html):
        parse_result = extract_metadata(article_html)
        validation = validate_content(parse_result)
        assert validation.is_valid is True

    def test_valid_product(self, product_html):
        parse_result = extract_metadata(product_html)
        validation = validate_content(parse_result)
        assert validation.is_valid is True

    def test_valid_blog(self, blog_html):
        parse_result = extract_metadata(blog_html)
        validation = validate_content(parse_result)
        assert validation.is_valid is True

    def test_empty_content(self, empty_html):
        parse_result = extract_metadata(empty_html)
        validation = validate_content(parse_result)
        assert validation.is_valid is False
        assert validation.reason == "EMPTY_CONTENT"

    def test_captcha_detection(self, captcha_html):
        parse_result = extract_metadata(captcha_html)
        validation = validate_content(parse_result)
        assert validation.is_valid is False
        assert validation.reason == "BLOCKED"

    def test_bot_title_detection(self):
        html = """
        <html><head>
        <title>Access Denied - Security Check</title>
        <meta name="robots" content="noindex">
        </head><body>
        <p>Your access has been blocked. Please contact support if you believe this is an error.
        This page is shown when automated access is detected by our security systems.</p>
        </body></html>
        """
        parse_result = extract_metadata(html)
        validation = validate_content(parse_result)
        assert validation.is_valid is False
        assert validation.reason == "BLOCKED"

    def test_cloudflare_challenge(self):
        html = """
        <html><head>
        <title>Just a moment...</title>
        </head><body>
        <div class="challenge-platform">
            <div id="cf-challenge-running">Checking your browser before accessing the website.
            This process is automatic. Your browser will redirect shortly. Please allow up to 5 seconds.</div>
        </div>
        </body></html>
        """
        parse_result = extract_metadata(html)
        validation = validate_content(parse_result)
        assert validation.is_valid is False
        assert validation.reason == "BLOCKED"

    def test_short_but_valid_content(self):
        """Short content below threshold should be invalid."""
        html = "<html><head><title>Short</title></head><body><p>Hi</p></body></html>"
        parse_result = extract_metadata(html)
        validation = validate_content(parse_result)
        assert validation.is_valid is False
        assert validation.reason == "EMPTY_CONTENT"

    def test_long_content_without_blocking_phrases(self):
        """Content with enough length and no blocking phrases should be valid."""
        body_text = "This is a perfectly normal article about technology. " * 10
        html = f"<html><head><title>Normal Article</title></head><body><p>{body_text}</p></body></html>"
        parse_result = extract_metadata(html)
        validation = validate_content(parse_result)
        assert validation.is_valid is True
