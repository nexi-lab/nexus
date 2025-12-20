"""Zoekt code search client for fast trigram-based search.

This module provides a client for the Zoekt search server, enabling
sub-50ms code search across large codebases using trigram indexing.

Zoekt is optional - if not available, grep falls back to Rust regex.

Usage:
    # Check if Zoekt is available
    if zoekt_client.is_available():
        results = await zoekt_client.search("def authenticate")

    # Build/rebuild index
    await zoekt_client.reindex()

Setup:
    # Start Zoekt with docker-compose
    docker compose --profile zoekt up -d

    # Build initial index
    docker compose --profile zoekt-index up

References:
    - https://github.com/sourcegraph/zoekt
    - https://sourcegraph.com/blog/sourcegraph-accepting-zoekt-maintainership
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

# Zoekt configuration from environment
ZOEKT_URL = os.getenv("ZOEKT_URL", "http://localhost:6070")
ZOEKT_ENABLED = os.getenv("ZOEKT_ENABLED", "false").lower() == "true"
ZOEKT_TIMEOUT = float(os.getenv("ZOEKT_TIMEOUT", "10.0"))  # seconds


@dataclass
class ZoektMatch:
    """A single match from Zoekt search."""

    file: str
    line: int
    content: str
    match: str
    score: float = 0.0


class ZoektClient:
    """Client for Zoekt code search server.

    Provides async interface to Zoekt's HTTP API for fast code search.
    Falls back gracefully if Zoekt is not available.
    """

    def __init__(
        self,
        base_url: str = ZOEKT_URL,
        timeout: float = ZOEKT_TIMEOUT,
        enabled: bool = ZOEKT_ENABLED,
    ):
        """Initialize Zoekt client.

        Args:
            base_url: Zoekt webserver URL (default: http://localhost:6070)
            timeout: Request timeout in seconds (default: 10.0)
            enabled: Whether Zoekt is enabled (default: from ZOEKT_ENABLED env)
        """
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._enabled = enabled
        self._available: bool | None = None  # Cached availability check

    async def is_available(self) -> bool:
        """Check if Zoekt server is available.

        Caches the result to avoid repeated health checks.
        Call reset_availability() to force re-check.
        """
        if not self._enabled:
            return False

        if self._available is not None:
            return self._available

        try:
            import httpx

            async with httpx.AsyncClient(timeout=2.0) as client:
                resp = await client.get(f"{self.base_url}/")
                self._available = resp.status_code == 200
        except Exception as e:
            logger.debug(f"Zoekt not available: {e}")
            self._available = False

        return self._available

    def reset_availability(self) -> None:
        """Reset cached availability status."""
        self._available = None

    async def search(
        self,
        query: str,
        num: int = 100,
        repos: list[str] | None = None,
    ) -> list[ZoektMatch]:
        """Search code using Zoekt.

        Args:
            query: Search query (supports regex, literals, boolean operators)
            num: Maximum number of results to return
            repos: Optional list of repos/paths to limit search to

        Returns:
            List of ZoektMatch objects with file, line, content, match

        Examples:
            # Literal search
            await client.search("authentication")

            # Regex search
            await client.search("def \\w+_handler")

            # Boolean operators
            await client.search("error AND handler")
        """
        if not await self.is_available():
            return []

        try:
            import httpx

            # Build query params
            params: dict[str, Any] = {
                "q": query,
                "num": num,
            }
            if repos:
                # Zoekt uses repo: prefix for filtering
                repo_filter = " OR ".join(f"repo:{r}" for r in repos)
                params["q"] = f"({query}) ({repo_filter})"

            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.get(
                    f"{self.base_url}/api/search",
                    params=params,
                )
                resp.raise_for_status()
                data = resp.json()

            return self._parse_results(data)

        except Exception as e:
            logger.warning(f"Zoekt search failed: {e}")
            return []

    def _parse_results(self, data: dict[str, Any]) -> list[ZoektMatch]:
        """Parse Zoekt API response into ZoektMatch objects."""
        results = []

        # Zoekt response structure:
        # {"Result": {"Files": [{"FileName": "...", "Lines": [...]}]}}
        files = data.get("Result", {}).get("Files", [])

        for file_info in files:
            file_name = file_info.get("FileName", "")
            score = file_info.get("Score", 0.0)

            for line_match in file_info.get("LineMatches", []):
                line_num = line_match.get("LineNumber", 0)
                line_content = line_match.get("Line", "")

                # Decode base64 if needed (Zoekt may encode content)
                if isinstance(line_content, bytes):
                    line_content = line_content.decode("utf-8", errors="replace")

                # Extract the matched fragments
                fragments = line_match.get("LineFragments", [])
                match_text = ""
                if fragments:
                    # Combine fragment matches
                    match_text = "".join(
                        line_content[
                            f.get("LineOffset", 0) : f.get("LineOffset", 0)
                            + f.get("MatchLength", 0)
                        ]
                        for f in fragments
                    )
                else:
                    match_text = line_content.strip()

                results.append(
                    ZoektMatch(
                        file=f"/{file_name}",  # Normalize to absolute path
                        line=line_num,
                        content=line_content,
                        match=match_text,
                        score=score,
                    )
                )

        return results

    async def get_stats(self) -> dict[str, Any]:
        """Get Zoekt index statistics.

        Returns:
            Dict with index stats (file count, size, etc.)
        """
        if not await self.is_available():
            return {"available": False}

        try:
            import httpx

            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.get(f"{self.base_url}/api/stats")
                resp.raise_for_status()
                stats: dict[str, Any] = resp.json()
                stats["available"] = True
                return stats
        except Exception as e:
            logger.warning(f"Failed to get Zoekt stats: {e}")
            return {"available": False, "error": str(e)}


# Global client instance
_client: ZoektClient | None = None


def get_zoekt_client() -> ZoektClient:
    """Get the global Zoekt client instance."""
    global _client
    if _client is None:
        _client = ZoektClient()
    return _client


async def is_zoekt_available() -> bool:
    """Check if Zoekt is available."""
    return await get_zoekt_client().is_available()


async def zoekt_search(
    query: str,
    num: int = 100,
    repos: list[str] | None = None,
) -> list[ZoektMatch]:
    """Search using Zoekt (convenience function)."""
    return await get_zoekt_client().search(query, num, repos)
