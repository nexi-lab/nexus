"""Firecrawl API client for production-grade web scraping."""

import asyncio
from typing import Any, Optional

import httpx
from pydantic import BaseModel, ConfigDict, Field


class ScrapeResponse(BaseModel):
    """Response from Firecrawl scrape endpoint."""

    success: bool
    data: dict[str, Any] = Field(default_factory=dict)
    markdown: Optional[str] = None
    html: Optional[str] = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class CrawlJob(BaseModel):
    """Crawl job information."""

    model_config = ConfigDict(populate_by_name=True)

    id: str
    status: str
    total: Optional[int] = None
    completed: Optional[int] = None
    credits_used: Optional[int] = Field(default=None, alias="creditsUsed")
    expires_at: Optional[str] = Field(default=None, alias="expiresAt")
    data: list[dict[str, Any]] = Field(default_factory=list)


class MapResponse(BaseModel):
    """Response from Firecrawl map endpoint."""

    success: bool
    links: list[str] = Field(default_factory=list)


class FirecrawlClient:
    """Async client for Firecrawl API.

    Provides production-grade web scraping with:
    - JS rendering (Puppeteer/Playwright)
    - Anti-bot detection handling
    - Proxy rotation
    - PDF/complex document support
    - LLM-ready markdown output
    """

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.firecrawl.dev/v1",
        timeout: int = 60,
        max_retries: int = 3,
    ):
        """Initialize Firecrawl client.

        Args:
            api_key: Firecrawl API key
            base_url: API base URL (default: https://api.firecrawl.dev/v1)
            timeout: Request timeout in seconds (default: 60)
            max_retries: Maximum number of retries (default: 3)
        """
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries

        self._client: Optional[httpx.AsyncClient] = None

    async def __aenter__(self) -> "FirecrawlClient":
        """Async context manager entry."""
        self._client = httpx.AsyncClient(
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            timeout=self.timeout,
        )
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Async context manager exit."""
        if self._client:
            await self._client.aclose()

    def _get_client(self) -> httpx.AsyncClient:
        """Get or create HTTP client."""
        if not self._client:
            self._client = httpx.AsyncClient(
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                timeout=self.timeout,
            )
        return self._client

    async def scrape(
        self,
        url: str,
        formats: Optional[list[str]] = None,
        only_main_content: bool = True,
        include_tags: Optional[list[str]] = None,
        exclude_tags: Optional[list[str]] = None,
        headers: Optional[dict[str, str]] = None,
        wait_for: Optional[int] = None,
        timeout: Optional[int] = None,
    ) -> ScrapeResponse:
        """Scrape a single URL.

        Args:
            url: URL to scrape
            formats: Output formats (e.g., ["markdown", "html"])
            only_main_content: Extract only main content (default: True)
            include_tags: HTML tags to include
            exclude_tags: HTML tags to exclude
            headers: Custom headers for the request
            wait_for: Time to wait for page load (ms)
            timeout: Override default timeout

        Returns:
            ScrapeResponse with scraped content

        Raises:
            httpx.HTTPError: If the request fails
        """
        client = self._get_client()

        payload: dict[str, Any] = {
            "url": url,
            "formats": formats or ["markdown"],
            "onlyMainContent": only_main_content,
        }

        if include_tags:
            payload["includeTags"] = include_tags
        if exclude_tags:
            payload["excludeTags"] = exclude_tags
        if headers:
            payload["headers"] = headers
        if wait_for:
            payload["waitFor"] = wait_for

        request_timeout = timeout or self.timeout

        for attempt in range(self.max_retries):
            try:
                response = await client.post(
                    f"{self.base_url}/scrape",
                    json=payload,
                    timeout=request_timeout,
                )
                response.raise_for_status()
                data = response.json()

                # Extract markdown from data if available
                markdown = None
                if "data" in data and isinstance(data["data"], dict):
                    markdown = data["data"].get("markdown")

                return ScrapeResponse(
                    success=data.get("success", True),
                    data=data.get("data", {}),
                    markdown=markdown,
                    html=data.get("data", {}).get("html") if "data" in data else None,
                    metadata=data.get("data", {}).get("metadata", {}) if "data" in data else {},
                )
            except (httpx.HTTPError, httpx.TimeoutException):
                if attempt == self.max_retries - 1:
                    raise
                await asyncio.sleep(2**attempt)  # Exponential backoff

        raise httpx.HTTPError("Max retries exceeded")

    async def crawl(
        self,
        url: str,
        max_depth: Optional[int] = None,
        limit: int = 100,
        include_paths: Optional[list[str]] = None,
        exclude_paths: Optional[list[str]] = None,
        allow_backward_links: bool = False,
        allow_external_links: bool = False,
    ) -> CrawlJob:
        """Start a crawl job for a website.

        Args:
            url: Base URL to crawl
            max_depth: Maximum crawl depth
            limit: Maximum number of pages (default: 100)
            include_paths: URL patterns to include (e.g., ["/api/**"])
            exclude_paths: URL patterns to exclude (e.g., ["/blog/**"])
            allow_backward_links: Allow crawling backward links
            allow_external_links: Allow crawling external domains

        Returns:
            CrawlJob with job ID and status

        Raises:
            httpx.HTTPError: If the request fails
        """
        client = self._get_client()

        payload: dict[str, Any] = {
            "url": url,
            "limit": limit,
            "scrapeOptions": {
                "formats": ["markdown"],
            },
            "crawlerOptions": {
                "allowBackwardLinks": allow_backward_links,
                "allowExternalLinks": allow_external_links,
            },
        }

        if max_depth:
            payload["crawlerOptions"]["maxDepth"] = max_depth
        if include_paths:
            payload["crawlerOptions"]["includes"] = include_paths
        if exclude_paths:
            payload["crawlerOptions"]["excludes"] = exclude_paths

        response = await client.post(
            f"{self.base_url}/crawl",
            json=payload,
        )
        response.raise_for_status()
        data = response.json()

        return CrawlJob(
            id=data.get("id", ""),
            status=data.get("status", "pending"),
            total=data.get("total"),
            completed=data.get("completed"),
            creditsUsed=data.get("creditsUsed"),
            expiresAt=data.get("expiresAt"),
            data=data.get("data", []),
        )

    async def get_crawl_status(self, job_id: str) -> CrawlJob:
        """Get the status of a crawl job.

        Args:
            job_id: Crawl job ID

        Returns:
            CrawlJob with current status and data

        Raises:
            httpx.HTTPError: If the request fails
        """
        client = self._get_client()

        response = await client.get(f"{self.base_url}/crawl/{job_id}")
        response.raise_for_status()
        data = response.json()

        return CrawlJob(
            id=job_id,
            status=data.get("status", "unknown"),
            total=data.get("total"),
            completed=data.get("completed"),
            creditsUsed=data.get("creditsUsed"),
            expiresAt=data.get("expiresAt"),
            data=data.get("data", []),
        )

    async def cancel_crawl(self, job_id: str) -> bool:
        """Cancel a running crawl job.

        Args:
            job_id: Crawl job ID

        Returns:
            True if cancelled successfully

        Raises:
            httpx.HTTPError: If the request fails
        """
        client = self._get_client()

        response = await client.delete(f"{self.base_url}/crawl/{job_id}")
        response.raise_for_status()
        data = response.json()

        return bool(data.get("success", False))

    async def map_url(
        self,
        url: str,
        search: Optional[str] = None,
        ignore_sitemap: bool = False,
        include_subdomains: bool = False,
        limit: int = 5000,
    ) -> MapResponse:
        """Map all URLs on a website.

        Args:
            url: Base URL to map
            search: Optional search query to filter URLs
            ignore_sitemap: Ignore sitemap.xml
            include_subdomains: Include subdomains
            limit: Maximum number of URLs (default: 5000)

        Returns:
            MapResponse with list of discovered URLs

        Raises:
            httpx.HTTPError: If the request fails
        """
        client = self._get_client()

        payload: dict[str, Any] = {
            "url": url,
            "limit": limit,
        }

        if search:
            payload["search"] = search
        if ignore_sitemap:
            payload["ignoreSitemap"] = ignore_sitemap
        if include_subdomains:
            payload["includeSubdomains"] = include_subdomains

        response = await client.post(
            f"{self.base_url}/map",
            json=payload,
        )
        response.raise_for_status()
        data = response.json()

        return MapResponse(
            success=data.get("success", True),
            links=data.get("links", []),
        )

    async def search(
        self,
        query: str,
        limit: int = 10,
        lang: str = "en",
        country: str = "us",
        scrape_results: bool = True,
    ) -> list[dict[str, Any]]:
        """Search the web and optionally scrape results.

        Args:
            query: Search query
            limit: Number of results (default: 10)
            lang: Language code (default: "en")
            country: Country code (default: "us")
            scrape_results: Whether to scrape each result (default: True)

        Returns:
            List of search results with scraped content

        Raises:
            httpx.HTTPError: If the request fails
        """
        client = self._get_client()

        payload = {
            "query": query,
            "limit": limit,
            "lang": lang,
            "country": country,
            "scrapeOptions": {
                "formats": ["markdown"],
            }
            if scrape_results
            else {},
        }

        response = await client.post(
            f"{self.base_url}/search",
            json=payload,
        )
        response.raise_for_status()
        data = response.json()

        result = data.get("data", [])
        return result if isinstance(result, list) else []

    async def extract(
        self,
        url: str,
        schema: dict[str, Any],
        prompt: Optional[str] = None,
        allow_external_links: bool = False,
    ) -> dict[str, Any]:
        """Extract structured data from a URL using a schema.

        Args:
            url: URL to extract from
            schema: JSON schema for extraction
            prompt: Optional extraction prompt
            allow_external_links: Allow extracting from external links

        Returns:
            Extracted structured data

        Raises:
            httpx.HTTPError: If the request fails
        """
        client = self._get_client()

        payload: dict[str, Any] = {
            "url": url,
            "schema": schema,
        }

        if prompt:
            payload["prompt"] = prompt
        if allow_external_links:
            payload["allowExternalLinks"] = allow_external_links

        response = await client.post(
            f"{self.base_url}/extract",
            json=payload,
        )
        response.raise_for_status()
        data = response.json()

        result = data.get("data", {})
        return result if isinstance(result, dict) else {}

    async def close(self) -> None:
        """Close the HTTP client."""
        if self._client:
            await self._client.aclose()
            self._client = None
