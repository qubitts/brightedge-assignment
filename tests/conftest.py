import os
import pytest
import pytest_asyncio
from pathlib import Path

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def article_html():
    return (FIXTURES_DIR / "article.html").read_text()


@pytest.fixture
def product_html():
    return (FIXTURES_DIR / "product.html").read_text()


@pytest.fixture
def blog_html():
    return (FIXTURES_DIR / "blog.html").read_text()


@pytest.fixture
def empty_html():
    return (FIXTURES_DIR / "empty.html").read_text()


@pytest.fixture
def captcha_html():
    return (FIXTURES_DIR / "captcha.html").read_text()


@pytest.fixture
def malformed_html():
    return (FIXTURES_DIR / "malformed.html").read_text()
