"""HackerNews Transport -- raw key->bytes I/O over the HN Firebase API.

Implements the Transport protocol for HackerNews, mapping:
- fetch(key) -> item/{id}.json -> JSON bytes (story with comments)
- list_keys(prefix) -> topstories/newstories/etc -> story file keys
- exists(key) -> feed/rank existence check

Read-only: store/remove/copy_key/create_dir raise BackendError.

No authentication required (public API).

Key schema:
    "top/1.json"       -> top story rank 1
    "new/3.json"       -> new story rank 3
    "best/5.json"      -> best story rank 5
    list_keys("")      -> common_prefixes = ["top/", "new/", ...]
    list_keys("top/")  -> ["top/1.json", "top/2.json", ...]
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Iterator
from typing import Any

import httpx

from nexus.contracts.exceptions import BackendError, NexusFileNotFoundError

logger = logging.getLogger(__name__)

# HackerNews API base URL
HN_API_BASE = "https://hacker-news.firebaseio.com/v0"

# Valid feed names
VALID_FEEDS: frozenset[str] = frozenset({"top", "new", "best", "ask", "show", "jobs"})

# Maximum comments to fetch (to avoid very long load times)
MAX_COMMENTS_DEPTH = 5
MAX_COMMENTS_TOTAL = 100


class HNTransport:
    """HackerNews API transport implementing the Transport protocol.

    Attributes:
        transport_name: ``"hn"`` -- used by PathAddressingEngine to build
            the backend name (``"path-hn"``).
    """

    transport_name: str = "hn"

    def __init__(
        self,
        stories_per_feed: int = 10,
        include_comments: bool = True,
    ) -> None:
        self._stories_per_feed = min(max(stories_per_feed, 1), 30)
        self._include_comments = include_comments
        self._client: httpx.AsyncClient | None = None

    # ------------------------------------------------------------------
    # HTTP client management
    # ------------------------------------------------------------------

    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create HTTP client."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=HN_API_BASE,
                timeout=30.0,
                follow_redirects=True,
            )
        return self._client

    async def _close_client(self) -> None:
        """Close HTTP client."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    # ------------------------------------------------------------------
    # HN API methods
    # ------------------------------------------------------------------

    async def _fetch_item(self, item_id: int) -> dict[str, Any] | None:
        """Fetch a single item from HN API."""
        client = await self._get_client()
        try:
            response = await client.get(f"/item/{item_id}.json")
            response.raise_for_status()
            result: dict[str, Any] | None = response.json()
            return result
        except Exception as e:
            logger.warning("Failed to fetch item %d: %s", item_id, e)
            return None

    async def _fetch_items_batch(self, item_ids: list[int]) -> list[dict[str, Any]]:
        """Fetch multiple items in parallel."""
        tasks = [self._fetch_item(item_id) for item_id in item_ids]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        return [r for r in results if isinstance(r, dict)]

    async def _fetch_story_ids(self, feed: str) -> list[int]:
        """Fetch story IDs for a feed (top, new, best, ask, show, jobs)."""
        client = await self._get_client()
        endpoint_map = {
            "top": "/topstories.json",
            "new": "/newstories.json",
            "best": "/beststories.json",
            "ask": "/askstories.json",
            "show": "/showstories.json",
            "jobs": "/jobstories.json",
        }

        endpoint = endpoint_map.get(feed)
        if not endpoint:
            raise BackendError(f"Unknown feed: {feed}", backend="hn")

        try:
            response = await client.get(endpoint)
            response.raise_for_status()
            result: list[int] = response.json()
            return result
        except Exception as e:
            raise BackendError(
                f"Failed to fetch {feed} stories: {e}",
                backend="hn",
            ) from e

    async def _fetch_comments_recursive(
        self,
        comment_ids: list[int],
        depth: int = 0,
        total_fetched: list[int] | None = None,
    ) -> list[dict[str, Any]]:
        """Recursively fetch comments with depth/count limits."""
        if total_fetched is None:
            total_fetched = [0]

        if depth >= MAX_COMMENTS_DEPTH or total_fetched[0] >= MAX_COMMENTS_TOTAL:
            return []

        if not comment_ids:
            return []

        remaining = MAX_COMMENTS_TOTAL - total_fetched[0]
        ids_to_fetch = comment_ids[:remaining]

        comments = await self._fetch_items_batch(ids_to_fetch)
        total_fetched[0] += len(comments)

        for comment in comments:
            if comment and "kids" in comment and total_fetched[0] < MAX_COMMENTS_TOTAL:
                replies = await self._fetch_comments_recursive(
                    comment["kids"],
                    depth=depth + 1,
                    total_fetched=total_fetched,
                )
                comment["replies"] = replies

        return comments

    async def _fetch_story_with_comments(
        self,
        story_id: int,
        include_comments: bool = True,
    ) -> dict[str, Any]:
        """Fetch a story with all its comments nested."""
        story = await self._fetch_item(story_id)
        if not story:
            raise NexusFileNotFoundError(f"Story {story_id} not found")

        if include_comments and "kids" in story:
            comments = await self._fetch_comments_recursive(story["kids"])
            story["comments"] = comments
        else:
            story["comments"] = []

        return story

    async def _fetch_feed_story(
        self,
        feed: str,
        rank: int,
    ) -> dict[str, Any]:
        """Fetch a story by its rank in a feed."""
        story_ids = await self._fetch_story_ids(feed)

        if rank < 1 or rank > len(story_ids):
            raise NexusFileNotFoundError(f"Rank {rank} out of range (1-{len(story_ids)})")

        story_id = story_ids[rank - 1]
        story = await self._fetch_story_with_comments(
            story_id,
            include_comments=self._include_comments,
        )

        story["_rank"] = rank
        story["_feed"] = feed
        return story

    # ------------------------------------------------------------------
    # Path resolution
    # ------------------------------------------------------------------

    def _resolve_path(self, path: str) -> tuple[str, int | None]:
        """Resolve virtual path to feed and rank.

        Returns:
            Tuple of (feed, rank) where rank is 1-based or None for directory
        """
        path = path.strip("/")
        if not path:
            return ("", None)

        parts = path.split("/")

        # Handle paths with "hn" prefix
        if parts and parts[0] == "hn":
            parts = parts[1:]

        if not parts or parts[0] == "":
            return ("", None)

        feed = parts[0]
        if feed not in VALID_FEEDS:
            raise BackendError(f"Unknown feed: {feed}. Valid: {VALID_FEEDS}", backend="hn")

        if len(parts) == 1:
            return (feed, None)

        if len(parts) == 2:
            filename = parts[1]
            if not filename.endswith(".json"):
                raise BackendError(f"Invalid file: {filename}", backend="hn")
            try:
                rank = int(filename.replace(".json", ""))
                if rank < 1 or rank > self._stories_per_feed:
                    raise BackendError(
                        f"Rank {rank} out of range (1-{self._stories_per_feed})",
                        backend="hn",
                    )
                return (feed, rank)
            except ValueError as e:
                raise BackendError(f"Invalid rank in {filename}", backend="hn") from e

        raise BackendError(f"Invalid path: {path}", backend="hn")

    # ------------------------------------------------------------------
    # Transport protocol methods
    # ------------------------------------------------------------------

    def store(self, key: str, data: bytes, content_type: str = "") -> str | None:
        raise BackendError(
            "HN transport is read-only. HackerNews API does not support posting.",
            backend="hn",
        )

    def fetch(self, key: str, version_id: str | None = None) -> tuple[bytes, str | None]:
        """Fetch a story as JSON bytes by transport key."""
        feed, rank = self._resolve_path(key)

        if rank is None:
            raise BackendError(
                f"Cannot read directory: {key}. Use list_dir() instead.",
                backend="hn",
            )

        logger.info("[HN] Fetching from API: %s/%d", feed, rank)

        async def _fetch() -> bytes:
            try:
                story = await self._fetch_feed_story(feed, rank)
                content = json.dumps(story, indent=2, ensure_ascii=False).encode("utf-8")
                return content
            finally:
                await self._close_client()

        from nexus.lib.sync_bridge import run_sync

        content = run_sync(_fetch())
        return content, None

    def remove(self, key: str) -> None:
        raise BackendError(
            "HN transport is read-only. HackerNews API does not support deletion.",
            backend="hn",
        )

    def exists(self, key: str) -> bool:
        """Check whether a feed/story key exists."""
        try:
            feed, rank = self._resolve_path(key)
            return feed != "" or key.strip("/") == ""
        except BackendError:
            return False

    def get_size(self, key: str) -> int:
        """Return estimated size of the story content."""
        return 10 * 1024  # 10 KB estimate

    def list_keys(self, prefix: str, delimiter: str = "/") -> tuple[list[str], list[str]]:
        """List story keys under *prefix*.

        - ``list_keys("")`` -> ``([], ["top/", "new/", ...])``
        - ``list_keys("top/")`` -> ``(["top/1.json", ...], [])``
        """
        prefix = prefix.strip("/")

        # Handle hn prefix
        if prefix.startswith("hn/"):
            prefix = prefix[3:]

        # Root -> return feed names as common prefixes
        if not prefix or prefix == "hn":
            return [], [f"{feed}/" for feed in ["top", "new", "best", "ask", "show", "jobs"]]

        # Feed directory -> list story files
        if prefix in VALID_FEEDS:
            keys = [f"{prefix}/{i}.json" for i in range(1, self._stories_per_feed + 1)]
            return keys, []

        return [], []

    def copy_key(self, src_key: str, dst_key: str) -> None:
        raise BackendError(
            "HN transport does not support copy.",
            backend="hn",
        )

    def create_dir(self, key: str) -> None:
        raise BackendError(
            "HN transport does not support directory creation. Feed structure is virtual.",
            backend="hn",
        )

    def stream(
        self,
        key: str,
        chunk_size: int = 8192,
        version_id: str | None = None,
    ) -> Iterator[bytes]:
        """Stream story content (small payloads -- fetch then chunk)."""
        data, _ = self.fetch(key, version_id)
        for i in range(0, len(data), chunk_size):
            yield data[i : i + chunk_size]

    def store_chunked(
        self,
        key: str,
        chunks: Iterator[bytes],
        content_type: str = "",
    ) -> str | None:
        raise BackendError(
            "HN transport is read-only. Cannot store content.",
            backend="hn",
        )
