import hashlib
import json
import logging
from typing import Optional

import redis.asyncio as redis

from src.config import settings

logger = logging.getLogger(__name__)


class CrawlCache:
    def __init__(self):
        self._redis: Optional[redis.Redis] = None

    async def connect(self):
        try:
            self._redis = redis.from_url(
                settings.REDIS_URL,
                decode_responses=True,
            )
            await self._redis.ping()
            logger.info("Connected to Redis")
        except Exception as e:
            logger.warning(f"Redis connection failed: {e}. Cache disabled.")
            self._redis = None

    async def close(self):
        if self._redis:
            await self._redis.aclose()
            self._redis = None

    @staticmethod
    def _key(url: str) -> str:
        url_hash = hashlib.sha256(url.encode()).hexdigest()
        return f"crawl:{url_hash}"

    async def get(self, url: str) -> Optional[dict]:
        if not self._redis:
            return None
        try:
            key = self._key(url)
            data = await self._redis.get(key)
            if data:
                return json.loads(data)
            return None
        except Exception as e:
            logger.warning(f"Cache get failed: {e}")
            return None

    async def set(self, url: str, result: dict, ttl: Optional[int] = None) -> None:
        if not self._redis:
            return
        try:
            key = self._key(url)
            ttl = ttl or settings.CACHE_TTL_SECONDS
            await self._redis.set(key, json.dumps(result), ex=ttl)
        except Exception as e:
            logger.warning(f"Cache set failed: {e}")

    @property
    def is_connected(self) -> bool:
        return self._redis is not None
