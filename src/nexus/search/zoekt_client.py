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

import asyncio
import logging
import os
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
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
            # Use /search endpoint with format=json (not /api/search which returns HTML)
            params: dict[str, Any] = {
                "q": query,
                "num": num,
                "format": "json",
            }
            if repos:
                # Zoekt uses repo: prefix for filtering
                repo_filter = " OR ".join(f"repo:{r}" for r in repos)
                params["q"] = f"({query}) ({repo_filter})"

            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.get(
                    f"{self.base_url}/search",
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

        # Zoekt /search?format=json response structure:
        # {"result": {"FileMatches": [{"FileName": "...", "Matches": [...]}]}}
        result = data.get("result", {})
        file_matches = result.get("FileMatches", [])

        for file_info in file_matches:
            file_name = file_info.get("FileName", "")

            for match in file_info.get("Matches", []):
                line_num = match.get("LineNum", 0)

                # Build content and match text from fragments
                fragments = match.get("Fragments", [])
                if fragments:
                    # Combine fragments to build full line content
                    first_frag = fragments[0]
                    pre = first_frag.get("Pre", "")
                    match_text = first_frag.get("Match", "")
                    post = first_frag.get("Post", "")
                    line_content = f"{pre}{match_text}{post}".strip()

                    # Combine all match texts
                    all_matches = "".join(f.get("Match", "") for f in fragments)
                else:
                    line_content = ""
                    match_text = ""
                    all_matches = ""

                results.append(
                    ZoektMatch(
                        file=f"/{file_name}",  # Normalize to absolute path
                        line=line_num,
                        content=line_content,
                        match=all_matches or match_text,
                        score=0.0,
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

            # Get stats via a simple search query
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.get(
                    f"{self.base_url}/search",
                    params={"q": ".", "num": 1, "format": "json"},
                )
                resp.raise_for_status()
                data = resp.json()

            result = data.get("result", {})
            stats: dict[str, Any] = result.get("Stats", {})
            stats["available"] = True
            stats["FileCount"] = stats.get("FileCount", 0)
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


# =============================================================================
# Zoekt Index Manager - Auto-reindexing with debouncing
# =============================================================================

# Configuration from environment
ZOEKT_INDEX_DIR = os.getenv("ZOEKT_INDEX_DIR", "/app/data/.zoekt-index")
ZOEKT_DATA_DIR = os.getenv("ZOEKT_DATA_DIR", "/app/data")
ZOEKT_DEBOUNCE_SECONDS = float(os.getenv("ZOEKT_DEBOUNCE_SECONDS", "5.0"))
ZOEKT_INDEX_BINARY = os.getenv("ZOEKT_INDEX_BINARY", "zoekt-index")


class ZoektIndexManager:
    """Manages Zoekt index updates with debouncing.

    This class handles automatic reindexing when files are written or synced.
    It uses debouncing to batch multiple writes into a single reindex operation.

    Usage:
        manager = get_zoekt_index_manager()

        # After writing a file
        manager.notify_write("/path/to/file.py")

        # After sync completes
        manager.notify_sync_complete(files_synced=100)
    """

    def __init__(
        self,
        index_dir: str = ZOEKT_INDEX_DIR,
        data_dir: str = ZOEKT_DATA_DIR,
        debounce_seconds: float = ZOEKT_DEBOUNCE_SECONDS,
        enabled: bool = ZOEKT_ENABLED,
    ):
        """Initialize the index manager.

        Args:
            index_dir: Directory where Zoekt stores its index
            data_dir: Directory containing files to index
            debounce_seconds: Wait time after last write before reindexing
            enabled: Whether Zoekt indexing is enabled
        """
        self.index_dir = Path(index_dir)
        self.data_dir = Path(data_dir)
        self.debounce_seconds = debounce_seconds
        self._enabled = enabled

        # Debouncing state
        self._pending_paths: set[str] = set()
        self._timer: threading.Timer | None = None
        self._lock = threading.Lock()
        self._indexing = False

        # Ensure index directory exists
        if self._enabled:
            self.index_dir.mkdir(parents=True, exist_ok=True)

    @property
    def enabled(self) -> bool:
        """Check if Zoekt indexing is enabled."""
        return self._enabled

    def notify_write(self, path: str) -> None:
        """Notify that a file was written.

        This schedules a debounced reindex operation.

        Args:
            path: Path to the file that was written
        """
        if not self._enabled:
            return

        with self._lock:
            self._pending_paths.add(path)
            self._schedule_reindex()

        logger.debug(f"Zoekt: queued for reindex: {path}")

    def notify_sync_complete(self, files_synced: int = 0) -> None:
        """Notify that a sync operation completed.

        This triggers an immediate reindex (with short debounce).

        Args:
            files_synced: Number of files that were synced
        """
        if not self._enabled:
            return

        if files_synced == 0:
            return

        logger.info(f"Zoekt: sync completed with {files_synced} files, scheduling reindex")

        with self._lock:
            # For sync completion, use a shorter debounce
            self._schedule_reindex(debounce_override=1.0)

    def _schedule_reindex(self, debounce_override: float | None = None) -> None:
        """Schedule a debounced reindex operation.

        Must be called with self._lock held.

        Args:
            debounce_override: Optional override for debounce time
        """
        # Cancel existing timer
        if self._timer is not None:
            self._timer.cancel()

        debounce = debounce_override or self.debounce_seconds
        self._timer = threading.Timer(debounce, self._do_reindex)
        self._timer.daemon = True
        self._timer.start()

    def _do_reindex(self) -> None:
        """Execute the reindex operation.

        This runs zoekt-index as a subprocess.
        """
        with self._lock:
            if self._indexing:
                # Already indexing, reschedule
                self._schedule_reindex()
                return

            self._indexing = True
            paths = self._pending_paths.copy()
            self._pending_paths.clear()
            self._timer = None

        try:
            logger.info(
                f"Zoekt: starting reindex ({len(paths)} pending paths, "
                f"index_dir={self.index_dir}, data_dir={self.data_dir})"
            )

            # Run zoekt-index
            result = subprocess.run(
                [
                    ZOEKT_INDEX_BINARY,
                    "-index",
                    str(self.index_dir),
                    str(self.data_dir),
                ],
                capture_output=True,
                text=True,
                timeout=300,  # 5 minute timeout
            )

            if result.returncode == 0:
                logger.info("Zoekt: reindex completed successfully")
                if result.stdout:
                    logger.debug(f"Zoekt stdout: {result.stdout[:500]}")
            else:
                logger.error(f"Zoekt: reindex failed with code {result.returncode}")
                if result.stderr:
                    logger.error(f"Zoekt stderr: {result.stderr[:500]}")

        except FileNotFoundError:
            logger.warning(
                f"Zoekt: {ZOEKT_INDEX_BINARY} not found. Install Zoekt or disable ZOEKT_ENABLED."
            )
        except subprocess.TimeoutExpired:
            logger.error("Zoekt: reindex timed out after 5 minutes")
        except Exception as e:
            logger.error(f"Zoekt: reindex failed: {e}")
        finally:
            with self._lock:
                self._indexing = False

                # If new paths were added during indexing, schedule another reindex
                if self._pending_paths:
                    self._schedule_reindex()

    def trigger_reindex_sync(self) -> bool:
        """Trigger an immediate synchronous reindex.

        Returns:
            True if reindex succeeded, False otherwise
        """
        if not self._enabled:
            return False

        try:
            logger.info("Zoekt: running synchronous reindex")

            result = subprocess.run(
                [
                    ZOEKT_INDEX_BINARY,
                    "-index",
                    str(self.index_dir),
                    str(self.data_dir),
                ],
                capture_output=True,
                text=True,
                timeout=300,
            )

            if result.returncode == 0:
                logger.info("Zoekt: synchronous reindex completed")
                return True
            else:
                logger.error(f"Zoekt: reindex failed: {result.stderr[:200]}")
                return False

        except Exception as e:
            logger.error(f"Zoekt: reindex failed: {e}")
            return False

    async def trigger_reindex_async(self) -> bool:
        """Trigger an immediate async reindex.

        Returns:
            True if reindex succeeded, False otherwise
        """
        if not self._enabled:
            return False

        try:
            logger.info("Zoekt: running async reindex")

            proc = await asyncio.create_subprocess_exec(
                ZOEKT_INDEX_BINARY,
                "-index",
                str(self.index_dir),
                str(self.data_dir),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=300)

            if proc.returncode == 0:
                logger.info("Zoekt: async reindex completed")
                return True
            else:
                logger.error(f"Zoekt: reindex failed: {stderr.decode()[:200]}")
                return False

        except TimeoutError:
            logger.error("Zoekt: async reindex timed out")
            return False
        except Exception as e:
            logger.error(f"Zoekt: async reindex failed: {e}")
            return False


# Global index manager instance
_index_manager: ZoektIndexManager | None = None


def get_zoekt_index_manager() -> ZoektIndexManager:
    """Get the global Zoekt index manager instance."""
    global _index_manager
    if _index_manager is None:
        _index_manager = ZoektIndexManager()
    return _index_manager


def notify_zoekt_write(path: str) -> None:
    """Convenience function to notify Zoekt of a file write."""
    get_zoekt_index_manager().notify_write(path)


def notify_zoekt_sync_complete(files_synced: int = 0) -> None:
    """Convenience function to notify Zoekt of sync completion."""
    get_zoekt_index_manager().notify_sync_complete(files_synced)
