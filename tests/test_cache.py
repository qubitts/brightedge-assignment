import pytest
import json
from unittest.mock import AsyncMock, patch, MagicMock

from src.cache.redis_cache import CrawlCache


@pytest.fixture
def cache():
    return CrawlCache()


class TestCrawlCache:
    @pytest.mark.asyncio
    async def test_get_miss_no_redis(self, cache):
        """When Redis is not connected, get returns None."""
        result = await cache.get("https://example.com")
        assert result is None

    @pytest.mark.asyncio
    async def test_set_no_redis(self, cache):
        """When Redis is not connected, set does nothing."""
        await cache.set("https://example.com", {"data": "test"})
        # Should not raise

    @pytest.mark.asyncio
    async def test_is_connected_false(self, cache):
        assert cache.is_connected is False

    @pytest.mark.asyncio
    async def test_key_generation(self):
        key1 = CrawlCache._key("https://example.com/page1")
        key2 = CrawlCache._key("https://example.com/page2")
        assert key1 != key2
        assert key1.startswith("crawl:")
        assert key2.startswith("crawl:")

    @pytest.mark.asyncio
    async def test_key_deterministic(self):
        key1 = CrawlCache._key("https://example.com/page")
        key2 = CrawlCache._key("https://example.com/page")
        assert key1 == key2

    @pytest.mark.asyncio
    async def test_get_with_mock_redis(self, cache):
        mock_redis = AsyncMock()
        mock_redis.get = AsyncMock(return_value=json.dumps({"url": "https://example.com", "status": "success"}))
        cache._redis = mock_redis

        result = await cache.get("https://example.com")
        assert result is not None
        assert result["status"] == "success"

    @pytest.mark.asyncio
    async def test_get_cache_miss_with_mock_redis(self, cache):
        mock_redis = AsyncMock()
        mock_redis.get = AsyncMock(return_value=None)
        cache._redis = mock_redis

        result = await cache.get("https://example.com")
        assert result is None

    @pytest.mark.asyncio
    async def test_set_with_mock_redis(self, cache):
        mock_redis = AsyncMock()
        mock_redis.set = AsyncMock()
        cache._redis = mock_redis

        await cache.set("https://example.com", {"data": "test"}, ttl=3600)
        mock_redis.set.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_handles_redis_error(self, cache):
        mock_redis = AsyncMock()
        mock_redis.get = AsyncMock(side_effect=Exception("Redis error"))
        cache._redis = mock_redis

        result = await cache.get("https://example.com")
        assert result is None  # Graceful handling

    @pytest.mark.asyncio
    async def test_set_handles_redis_error(self, cache):
        mock_redis = AsyncMock()
        mock_redis.set = AsyncMock(side_effect=Exception("Redis error"))
        cache._redis = mock_redis

        # Should not raise
        await cache.set("https://example.com", {"data": "test"})

    @pytest.mark.asyncio
    async def test_close(self, cache):
        mock_redis = AsyncMock()
        mock_redis.aclose = AsyncMock()
        cache._redis = mock_redis

        await cache.close()
        assert cache._redis is None
        mock_redis.aclose.assert_called_once()
