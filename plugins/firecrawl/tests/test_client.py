"""Tests for Firecrawl API client."""

import pytest
from pytest_httpx import HTTPXMock

from nexus_firecrawl.client import CrawlJob, FirecrawlClient, MapResponse, ScrapeResponse


@pytest.mark.asyncio
async def test_scrape_success(httpx_mock: HTTPXMock) -> None:
    """Test successful scrape."""
    httpx_mock.add_response(
        url="https://api.firecrawl.dev/v1/scrape",
        json={
            "success": True,
            "data": {
                "markdown": "# Test Content",
                "html": "<h1>Test Content</h1>",
                "metadata": {"title": "Test Page"},
            },
        },
    )

    async with FirecrawlClient(api_key="test-key") as client:
        result = await client.scrape("https://example.com")

    assert isinstance(result, ScrapeResponse)
    assert result.success is True
    assert result.markdown == "# Test Content"
    assert result.metadata["title"] == "Test Page"


@pytest.mark.asyncio
async def test_scrape_with_options(httpx_mock: HTTPXMock) -> None:
    """Test scrape with custom options."""
    httpx_mock.add_response(
        url="https://api.firecrawl.dev/v1/scrape",
        json={
            "success": True,
            "data": {
                "markdown": "# Content",
            },
        },
    )

    async with FirecrawlClient(api_key="test-key") as client:
        result = await client.scrape(
            "https://example.com",
            formats=["markdown", "html"],
            only_main_content=False,
            include_tags=["article", "main"],
            wait_for=1000,
        )

    assert result.success is True


@pytest.mark.asyncio
async def test_crawl_start(httpx_mock: HTTPXMock) -> None:
    """Test starting a crawl job."""
    httpx_mock.add_response(
        url="https://api.firecrawl.dev/v1/crawl",
        json={
            "id": "job-123",
            "status": "pending",
            "total": 100,
            "completed": 0,
        },
    )

    async with FirecrawlClient(api_key="test-key") as client:
        job = await client.crawl("https://example.com", limit=100)

    assert isinstance(job, CrawlJob)
    assert job.id == "job-123"
    assert job.status == "pending"


@pytest.mark.asyncio
async def test_get_crawl_status(httpx_mock: HTTPXMock) -> None:
    """Test getting crawl job status."""
    httpx_mock.add_response(
        url="https://api.firecrawl.dev/v1/crawl/job-123",
        json={
            "status": "completed",
            "total": 100,
            "completed": 100,
            "data": [
                {"url": "https://example.com/page1", "markdown": "# Page 1"},
                {"url": "https://example.com/page2", "markdown": "# Page 2"},
            ],
        },
    )

    async with FirecrawlClient(api_key="test-key") as client:
        job = await client.get_crawl_status("job-123")

    assert job.status == "completed"
    assert job.total == 100
    assert job.completed == 100
    assert len(job.data) == 2


@pytest.mark.asyncio
async def test_cancel_crawl(httpx_mock: HTTPXMock) -> None:
    """Test cancelling a crawl job."""
    httpx_mock.add_response(
        url="https://api.firecrawl.dev/v1/crawl/job-123",
        method="DELETE",
        json={"success": True},
    )

    async with FirecrawlClient(api_key="test-key") as client:
        result = await client.cancel_crawl("job-123")

    assert result is True


@pytest.mark.asyncio
async def test_map_url(httpx_mock: HTTPXMock) -> None:
    """Test mapping URLs."""
    httpx_mock.add_response(
        url="https://api.firecrawl.dev/v1/map",
        json={
            "success": True,
            "links": [
                "https://example.com/page1",
                "https://example.com/page2",
                "https://example.com/page3",
            ],
        },
    )

    async with FirecrawlClient(api_key="test-key") as client:
        result = await client.map_url("https://example.com")

    assert isinstance(result, MapResponse)
    assert result.success is True
    assert len(result.links) == 3


@pytest.mark.asyncio
async def test_search(httpx_mock: HTTPXMock) -> None:
    """Test web search."""
    httpx_mock.add_response(
        url="https://api.firecrawl.dev/v1/search",
        json={
            "data": [
                {
                    "url": "https://example.com",
                    "title": "Example",
                    "markdown": "# Example content",
                }
            ]
        },
    )

    async with FirecrawlClient(api_key="test-key") as client:
        results = await client.search("test query", limit=10)

    assert len(results) == 1
    assert results[0]["url"] == "https://example.com"


@pytest.mark.asyncio
async def test_extract(httpx_mock: HTTPXMock) -> None:
    """Test structured data extraction."""
    httpx_mock.add_response(
        url="https://api.firecrawl.dev/v1/extract",
        json={
            "data": {
                "title": "Test Page",
                "author": "John Doe",
                "published": "2024-01-01",
            }
        },
    )

    schema = {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "author": {"type": "string"},
            "published": {"type": "string"},
        },
    }

    async with FirecrawlClient(api_key="test-key") as client:
        result = await client.extract("https://example.com", schema)

    assert result["title"] == "Test Page"
    assert result["author"] == "John Doe"


@pytest.mark.asyncio
async def test_client_retry_logic(httpx_mock: HTTPXMock) -> None:
    """Test client retry logic on failure."""
    # First two requests fail, third succeeds
    httpx_mock.add_response(status_code=500)
    httpx_mock.add_response(status_code=500)
    httpx_mock.add_response(
        json={
            "success": True,
            "data": {"markdown": "# Success"},
        }
    )

    async with FirecrawlClient(api_key="test-key", max_retries=3) as client:
        result = await client.scrape("https://example.com")

    assert result.success is True
    assert result.markdown == "# Success"


@pytest.mark.asyncio
async def test_client_custom_timeout(httpx_mock: HTTPXMock) -> None:
    """Test client with custom timeout."""
    httpx_mock.add_response(
        json={
            "success": True,
            "data": {"markdown": "# Content"},
        }
    )

    async with FirecrawlClient(api_key="test-key", timeout=30) as client:
        result = await client.scrape("https://example.com", timeout=15)

    assert result.success is True
