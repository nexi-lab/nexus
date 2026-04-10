"""Zoekt code search client for fast trigram-based search.

This module provides a client for the Zoekt search server, enabling
sub-50ms code search across large codebases using trigram indexing.

Zoekt is optional - if not available, grep falls back to Rust regex.

Usage:
    # Create via DI (factory.py creates and injects)
    client = ZoektClient(base_url="http://localhost:6070", enabled=True)
    results = await client.search("def authenticate")

    # Close when done
    await client.close()

Setup:
    # Start Zoekt with docker-compose
    docker compose --profile zoekt up -d

    # Build initial index
    docker compose --profile zoekt-index up

References:
    - https://github.com/sourcegraph/zoekt
    - https://sourcegraph.com/blog/sourcegraph-accepting-zoekt-maintainership
"""

import asyncio
import logging
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


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

    Issue #2188: Accepts all config via constructor (no module-level globals).
    Uses a persistent httpx.AsyncClient for connection pooling.
    """

    def __init__(
        self,
        base_url: str = "http://localhost:6070",
        timeout: float = 10.0,
        enabled: bool = False,
    ):
        """Initialize Zoekt client.

        Args:
            base_url: Zoekt webserver URL
            timeout: Request timeout in seconds
            enabled: Whether Zoekt is enabled
        """
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._enabled = enabled
        self._available: bool | None = None  # Cached availability check
        self._http_client: Any | None = None  # Lazy httpx.AsyncClient

    async def _get_http_client(self) -> Any:
        """Get or create the persistent HTTP client with connection pooling."""
        if self._http_client is None:
            import httpx

            self._http_client = httpx.AsyncClient(
                timeout=self.timeout,
                limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
            )
        return self._http_client

    async def close(self) -> None:
        """Close the HTTP client and release connections."""
        if self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None

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

            # Use a short-lived client for health check (fast timeout)
            async with httpx.AsyncClient(timeout=2.0) as client:
                resp = await client.get(f"{self.base_url}/")
                self._available = resp.status_code == 200
        except Exception as e:
            logger.debug("Zoekt not available: %s", e)
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
        """
        if not await self.is_available():
            return []

        try:
            client = await self._get_http_client()

            params: dict[str, Any] = {
                "q": query,
                "num": num,
                "format": "json",
            }
            if repos:
                repo_filter = " OR ".join(f"repo:{r}" for r in repos)
                params["q"] = f"({query}) ({repo_filter})"

            resp = await client.get(
                f"{self.base_url}/search",
                params=params,
            )
            resp.raise_for_status()
            data = resp.json()

            return self._parse_results(data)

        except Exception as e:
            logger.warning("Zoekt search failed: %s", e)
            return []

    def _parse_results(self, data: dict[str, Any]) -> list[ZoektMatch]:
        """Parse Zoekt API response into ZoektMatch objects."""
        results = []

        result = data.get("result", {})
        file_matches = result.get("FileMatches") or []

        for file_info in file_matches:
            file_name = file_info.get("FileName", "")

            for match in file_info.get("Matches", []):
                line_num = match.get("LineNum", 0)

                fragments = match.get("Fragments", [])
                if fragments:
                    first_frag = fragments[0]
                    pre = first_frag.get("Pre", "")
                    match_text = first_frag.get("Match", "")
                    post = first_frag.get("Post", "")
                    line_content = f"{pre}{match_text}{post}".strip()
                    all_matches = "".join(f.get("Match", "") for f in fragments)
                else:
                    line_content = ""
                    match_text = ""
                    all_matches = ""

                results.append(
                    ZoektMatch(
                        file=f"/{file_name}",
                        line=line_num,
                        content=line_content,
                        match=all_matches or match_text,
                        score=0.0,
                    )
                )

        return results

    async def get_stats(self) -> dict[str, Any]:
        """Get Zoekt index statistics."""
        if not await self.is_available():
            return {"available": False}

        try:
            client = await self._get_http_client()
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
            logger.warning("Failed to get Zoekt stats: %s", e)
            return {"available": False, "error": str(e)}


# =============================================================================
# Zoekt Index Manager - Auto-reindexing with debouncing
# =============================================================================


class ZoektIndexManager:
    """Manages Zoekt index updates with debouncing.

    This class handles automatic reindexing when files are written or synced.
    It uses debouncing to batch multiple writes into a single reindex operation.

    Issue #2188: Accepts all config via constructor (no module-level globals).

    Usage:
        manager = ZoektIndexManager(
            index_dir="/app/data/.zoekt-index",
            data_dir="/app/data",
            enabled=True,
        )
        manager.notify_write("/path/to/file.py")
        manager.notify_sync_complete(files_synced=100)
    """

    def __init__(
        self,
        index_dir: str = "/app/data/.zoekt-index",
        data_dir: str = "/app/data",
        debounce_seconds: float = 5.0,
        enabled: bool = False,
        index_binary: str = "zoekt-index",
    ):
        """Initialize the index manager.

        Args:
            index_dir: Directory where Zoekt stores its index
            data_dir: Directory containing files to index
            debounce_seconds: Wait time after last write before reindexing
            enabled: Whether Zoekt indexing is enabled
            index_binary: Path to the zoekt-index binary
        """
        self.index_dir = Path(index_dir)
        self.data_dir = Path(data_dir)
        self.debounce_seconds = debounce_seconds
        self._enabled = enabled
        self._index_binary = index_binary

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

        logger.debug("Zoekt: queued for reindex: %s", path)

    def notify_sync_complete(self, files_synced: int = 0) -> None:
        """Notify that a sync operation completed.

        Args:
            files_synced: Number of files that were synced
        """
        if not self._enabled:
            return

        if files_synced == 0:
            return

        logger.info("Zoekt: sync completed with %d files, scheduling reindex", files_synced)

        with self._lock:
            self._schedule_reindex(debounce_override=1.0)

    def _schedule_reindex(self, debounce_override: float | None = None) -> None:
        """Schedule a debounced reindex operation.

        Must be called with self._lock held.
        """
        if self._timer is not None:
            self._timer.cancel()

        debounce = debounce_override or self.debounce_seconds
        self._timer = threading.Timer(debounce, self._do_reindex)
        self._timer.daemon = True
        self._timer.start()

    def _do_reindex(self) -> None:
        """Execute the reindex operation via zoekt-index subprocess."""
        with self._lock:
            if self._indexing:
                self._schedule_reindex()
                return

            self._indexing = True
            paths = self._pending_paths.copy()
            self._pending_paths.clear()
            self._timer = None

        try:
            logger.info(
                "Zoekt: starting reindex (%d pending paths, index_dir=%s, data_dir=%s)",
                len(paths),
                self.index_dir,
                self.data_dir,
            )

            result = subprocess.run(
                [
                    self._index_binary,
                    "-index",
                    str(self.index_dir),
                    str(self.data_dir),
                ],
                capture_output=True,
                text=True,
                timeout=300,
            )

            if result.returncode == 0:
                logger.info("Zoekt: reindex completed successfully")
                if result.stdout:
                    logger.debug("Zoekt stdout: %s", result.stdout[:500])
            else:
                logger.error("Zoekt: reindex failed with code %d", result.returncode)
                if result.stderr:
                    logger.error("Zoekt stderr: %s", result.stderr[:500])

        except FileNotFoundError:
            logger.warning(
                "Zoekt: %s not found. Install Zoekt or disable zoekt_enabled.",
                self._index_binary,
            )
        except subprocess.TimeoutExpired:
            logger.error("Zoekt: reindex timed out after 5 minutes")
        except Exception as e:
            logger.error("Zoekt: reindex failed: %s", e)
        finally:
            with self._lock:
                self._indexing = False
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
                    self._index_binary,
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
                logger.error("Zoekt: reindex failed: %s", result.stderr[:200])
                return False

        except Exception as e:
            logger.error("Zoekt: reindex failed: %s", e)
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
                self._index_binary,
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
                logger.error("Zoekt: reindex failed: %s", stderr.decode()[:200])
                return False

        except TimeoutError:
            logger.error("Zoekt: async reindex timed out")
            return False
        except Exception as e:
            logger.error("Zoekt: async reindex failed: %s", e)
            return False
