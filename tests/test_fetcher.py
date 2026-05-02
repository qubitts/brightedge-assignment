import pytest
import httpx
import respx

from src.crawler.fetcher import PageFetcher, FetchError


@pytest.fixture
def fetcher():
    return PageFetcher()


@pytest.mark.asyncio
@respx.mock
async def test_fetch_success(fetcher):
    respx.get("https://example.com/page").mock(
        return_value=httpx.Response(200, text="<html><body>Hello</body></html>")
    )
    result = await fetcher.fetch("https://example.com/page")
    assert result.status_code == 200
    assert "Hello" in result.body
    assert result.response_time_ms > 0
    await fetcher.close()


@pytest.mark.asyncio
@respx.mock
async def test_fetch_404(fetcher):
    respx.get("https://example.com/notfound").mock(
        return_value=httpx.Response(404, text="Not Found")
    )
    with pytest.raises(FetchError) as exc_info:
        await fetcher.fetch("https://example.com/notfound")
    assert exc_info.value.code == "FETCH_CLIENT_ERROR"
    assert exc_info.value.retryable is False
    await fetcher.close()


@pytest.mark.asyncio
@respx.mock
async def test_fetch_500_retries(fetcher):
    route = respx.get("https://example.com/error")
    route.mock(return_value=httpx.Response(500, text="Server Error"))

    with pytest.raises(FetchError) as exc_info:
        await fetcher.fetch("https://example.com/error")
    assert exc_info.value.code == "FETCH_HTTP_ERROR"
    assert exc_info.value.retryable is True
    # Should have retried (3 attempts total)
    assert route.call_count == 3
    await fetcher.close()


@pytest.mark.asyncio
@respx.mock
async def test_fetch_timeout(fetcher):
    respx.get("https://example.com/slow").mock(side_effect=httpx.ReadTimeout("timeout"))

    with pytest.raises(FetchError) as exc_info:
        await fetcher.fetch("https://example.com/slow")
    assert exc_info.value.code == "FETCH_TIMEOUT"
    assert exc_info.value.retryable is True
    await fetcher.close()


@pytest.mark.asyncio
@respx.mock
async def test_fetch_connection_error(fetcher):
    respx.get("https://example.com/down").mock(
        side_effect=httpx.ConnectError("Connection refused")
    )

    with pytest.raises(FetchError) as exc_info:
        await fetcher.fetch("https://example.com/down")
    assert exc_info.value.code == "FETCH_CONNECTION_ERROR"
    assert exc_info.value.retryable is True
    await fetcher.close()


@pytest.mark.asyncio
@respx.mock
async def test_fetch_redirect(fetcher):
    respx.get("https://example.com/old").mock(
        return_value=httpx.Response(
            301,
            headers={"Location": "https://example.com/new"},
        )
    )
    respx.get("https://example.com/new").mock(
        return_value=httpx.Response(200, text="<html>New Page</html>")
    )
    result = await fetcher.fetch("https://example.com/old")
    assert result.status_code == 200
    assert "New Page" in result.body
    await fetcher.close()


@pytest.mark.asyncio
@respx.mock
async def test_fetch_content_too_large(fetcher):
    # Simulate large content via Content-Length header
    large_content = "x" * 100
    respx.get("https://example.com/large").mock(
        return_value=httpx.Response(
            200,
            text=large_content,
            headers={"Content-Length": "20000000"},  # 20MB
        )
    )
    with pytest.raises(FetchError) as exc_info:
        await fetcher.fetch("https://example.com/large")
    assert exc_info.value.code == "CONTENT_TOO_LARGE"
    assert exc_info.value.retryable is False
    await fetcher.close()
