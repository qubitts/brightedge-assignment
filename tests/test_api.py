import pytest
import time
from unittest.mock import AsyncMock, patch, MagicMock
from pathlib import Path

from fastapi.testclient import TestClient
import httpx
import respx

from src.main import app
from src.crawler.fetcher import PageFetcher
from src.cache.redis_cache import CrawlCache

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def client():
    """Create test client with mocked dependencies."""
    app.state.fetcher = PageFetcher()
    app.state.cache = CrawlCache()  # Not connected = no caching
    app.state.db = None  # No DB in API tests
    app.state.startup_time = time.time()

    with TestClient(app) as c:
        yield c


class TestHealthEndpoint:
    def test_health(self, client):
        response = client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "healthy"
        assert data["version"] == "1.0.0"
        assert "uptime_seconds" in data


class TestCrawlEndpoint:
    @respx.mock
    def test_crawl_success(self, client):
        html = (FIXTURES_DIR / "article.html").read_text()
        respx.get("https://example.com/article").mock(
            return_value=httpx.Response(200, text=html)
        )

        response = client.post("/crawl", json={
            "url": "https://example.com/article",
            "options": {"classification_mode": "nlp", "max_keywords": 5}
        })
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "success"
        assert data["metadata"]["title"] is not None
        assert data["classification"]["mode"] == "nlp"
        assert len(data["classification"]["keywords"]) > 0
        assert data["crawl_metadata"]["http_status"] == 200

    @respx.mock
    def test_crawl_with_parser_mode_bs4(self, client):
        html = (FIXTURES_DIR / "article.html").read_text()
        respx.get("https://example.com/article").mock(
            return_value=httpx.Response(200, text=html)
        )

        response = client.post("/crawl", json={
            "url": "https://example.com/article",
            "options": {"parser_mode": "bs4", "max_keywords": 3}
        })
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "success"
        assert data["metadata"]["body"] is not None
        assert len(data["metadata"]["body"]) > 0

    @respx.mock
    def test_crawl_with_parser_mode_trafilatura(self, client):
        html = (FIXTURES_DIR / "article.html").read_text()
        respx.get("https://example.com/article").mock(
            return_value=httpx.Response(200, text=html)
        )

        response = client.post("/crawl", json={
            "url": "https://example.com/article",
            "options": {"parser_mode": "trafilatura", "max_keywords": 3}
        })
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "success"

    def test_crawl_invalid_parser_mode(self, client):
        response = client.post("/crawl", json={
            "url": "https://example.com",
            "options": {"parser_mode": "invalid"}
        })
        assert response.status_code == 422

    @respx.mock
    def test_crawl_404(self, client):
        respx.get("https://example.com/missing").mock(
            return_value=httpx.Response(404, text="Not Found")
        )

        response = client.post("/crawl", json={
            "url": "https://example.com/missing"
        })
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "failed"
        assert data["error"]["code"] == "FETCH_CLIENT_ERROR"

    @respx.mock
    def test_crawl_bot_detection(self, client):
        html = (FIXTURES_DIR / "captcha.html").read_text()
        respx.get("https://example.com/blocked").mock(
            return_value=httpx.Response(200, text=html)
        )

        response = client.post("/crawl", json={
            "url": "https://example.com/blocked"
        })
        data = response.json()
        assert data["status"] == "blocked"
        assert data["error"]["code"] == "BLOCKED"
        assert data["metadata"] is not None

    @respx.mock
    def test_crawl_rate_limited(self, client):
        respx.get("https://example.com/limited").mock(
            return_value=httpx.Response(429, text="Too Many Requests")
        )

        response = client.post("/crawl", json={
            "url": "https://example.com/limited"
        })
        data = response.json()
        assert data["status"] == "rate_limited"
        assert data["error"]["code"] == "RATE_LIMITED"
        assert data["error"]["retryable"] is True

    @respx.mock
    def test_crawl_403_blocked(self, client):
        respx.get("https://example.com/forbidden").mock(
            return_value=httpx.Response(403, text="Forbidden")
        )

        response = client.post("/crawl", json={
            "url": "https://example.com/forbidden"
        })
        data = response.json()
        assert data["status"] == "blocked"
        assert data["error"]["code"] == "BLOCKED"

    def test_crawl_invalid_url(self, client):
        response = client.post("/crawl", json={
            "url": "not-a-url"
        })
        assert response.status_code == 422

    def test_crawl_invalid_mode(self, client):
        response = client.post("/crawl", json={
            "url": "https://example.com",
            "options": {"classification_mode": "invalid"}
        })
        assert response.status_code == 422


class TestBatchEndpoint:
    @respx.mock
    def test_batch_crawl(self, client):
        html = (FIXTURES_DIR / "article.html").read_text()
        respx.get("https://example.com/page1").mock(
            return_value=httpx.Response(200, text=html)
        )
        respx.get("https://example.com/page2").mock(
            return_value=httpx.Response(200, text=html)
        )

        response = client.post("/crawl/batch", json={
            "urls": [
                "https://example.com/page1",
                "https://example.com/page2"
            ],
            "options": {"concurrency": 2, "max_keywords": 3}
        })
        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 2
        assert len(data["results"]) == 2
        assert data["batch_id"].startswith("b-")

    @respx.mock
    def test_batch_mixed_results(self, client):
        html = (FIXTURES_DIR / "article.html").read_text()
        respx.get("https://example.com/good").mock(
            return_value=httpx.Response(200, text=html)
        )
        respx.get("https://example.com/bad").mock(
            return_value=httpx.Response(404, text="Not Found")
        )

        response = client.post("/crawl/batch", json={
            "urls": [
                "https://example.com/good",
                "https://example.com/bad"
            ]
        })
        data = response.json()
        assert data["total"] == 2
        statuses = [r["status"] for r in data["results"]]
        assert "success" in statuses
        assert "failed" in statuses

    def test_batch_empty_urls(self, client):
        response = client.post("/crawl/batch", json={
            "urls": []
        })
        assert response.status_code == 422

    def test_batch_too_many_urls(self, client):
        urls = [f"https://example.com/page{i}" for i in range(51)]
        response = client.post("/crawl/batch", json={
            "urls": urls
        })
        assert response.status_code == 422
