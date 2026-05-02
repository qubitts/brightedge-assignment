import httpx
import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Optional

from src.config import settings

logger = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


@dataclass
class FetchResult:
    status_code: int
    headers: dict[str, str]
    body: str
    response_time_ms: float
    final_url: str


class FetchError(Exception):
    def __init__(self, code: str, message: str, retryable: bool):
        self.code = code
        self.message = message
        self.retryable = retryable
        super().__init__(message)


class PageFetcher:
    def __init__(self, client: Optional[httpx.AsyncClient] = None):
        self._client = client
        self._owns_client = client is None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                headers={"User-Agent": USER_AGENT},
                follow_redirects=True,
                max_redirects=5,
                timeout=httpx.Timeout(
                    connect=10.0,
                    read=float(settings.FETCH_TIMEOUT),
                    write=10.0,
                    pool=10.0,
                ),
            )
        return self._client

    async def close(self):
        if self._client and self._owns_client:
            await self._client.aclose()
            self._client = None

    async def fetch(self, url: str, timeout: Optional[int] = None) -> FetchResult:
        client = await self._get_client()
        last_error: Optional[Exception] = None
        max_retries = settings.MAX_RETRIES

        for attempt in range(max_retries):
            if attempt > 0:
                delay = 2 ** (attempt - 1)  # 1s, 2s
                logger.info(f"Retry {attempt}/{max_retries - 1} for {url} after {delay}s")
                await asyncio.sleep(delay)

            try:
                start = time.monotonic()

                request_timeout = None
                if timeout:
                    request_timeout = httpx.Timeout(
                        connect=10.0,
                        read=float(timeout),
                        write=10.0,
                        pool=10.0,
                    )

                # Stream to check content size
                async with client.stream(
                    "GET", url, timeout=request_timeout
                ) as response:
                    content_length = response.headers.get("content-length")
                    if content_length and int(content_length) > settings.MAX_CONTENT_SIZE:
                        raise FetchError(
                            "CONTENT_TOO_LARGE",
                            f"Content size {content_length} exceeds limit {settings.MAX_CONTENT_SIZE}",
                            retryable=False,
                        )

                    # Read body with size limit
                    chunks = []
                    total_size = 0
                    async for chunk in response.aiter_bytes():
                        total_size += len(chunk)
                        if total_size > settings.MAX_CONTENT_SIZE:
                            raise FetchError(
                                "CONTENT_TOO_LARGE",
                                f"Content exceeds {settings.MAX_CONTENT_SIZE} bytes",
                                retryable=False,
                            )
                        chunks.append(chunk)

                    elapsed_ms = (time.monotonic() - start) * 1000
                    body_bytes = b"".join(chunks)

                    # Detect encoding
                    encoding = response.encoding or "utf-8"
                    try:
                        body = body_bytes.decode(encoding)
                    except (UnicodeDecodeError, LookupError):
                        body = body_bytes.decode("utf-8", errors="replace")

                    headers = dict(response.headers)
                    final_url = str(response.url)

                    # Check HTTP status
                    if response.status_code >= 500:
                        raise FetchError(
                            "FETCH_HTTP_ERROR",
                            f"Server error: HTTP {response.status_code}",
                            retryable=True,
                        )
                    if response.status_code == 429:
                        raise FetchError(
                            "RATE_LIMITED",
                            "Rate limited: HTTP 429",
                            retryable=True,
                        )
                    if response.status_code == 403:
                        raise FetchError(
                            "BLOCKED",
                            "Blocked: HTTP 403",
                            retryable=False,
                        )
                    if response.status_code >= 400:
                        raise FetchError(
                            "FETCH_CLIENT_ERROR",
                            f"Client error: HTTP {response.status_code}",
                            retryable=False,
                        )

                    return FetchResult(
                        status_code=response.status_code,
                        headers=headers,
                        body=body,
                        response_time_ms=round(elapsed_ms, 2),
                        final_url=final_url,
                    )

            except FetchError as e:
                if not e.retryable or attempt == max_retries - 1:
                    raise
                last_error = e

            except httpx.TimeoutException:
                last_error = FetchError(
                    "FETCH_TIMEOUT",
                    f"Request timed out after {timeout or settings.FETCH_TIMEOUT}s",
                    retryable=True,
                )
                if attempt == max_retries - 1:
                    raise last_error

            except httpx.ConnectError:
                last_error = FetchError(
                    "FETCH_CONNECTION_ERROR",
                    f"Could not connect to {url}",
                    retryable=True,
                )
                if attempt == max_retries - 1:
                    raise last_error

            except httpx.TooManyRedirects:
                raise FetchError(
                    "FETCH_CLIENT_ERROR",
                    "Too many redirects (max 5)",
                    retryable=False,
                )

            except Exception as e:
                raise FetchError(
                    "FETCH_CONNECTION_ERROR",
                    f"Unexpected error: {str(e)}",
                    retryable=False,
                )

        raise last_error  # type: ignore
