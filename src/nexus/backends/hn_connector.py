"""HackerNews connector backend with virtual filesystem mapping.

This connector maps HackerNews API to a virtual filesystem, allowing
AI agents and applications to browse HN using familiar file operations.

Virtual filesystem structure:
    /hn/top/1.json ... 10.json    - Top 10 stories with comments
    /hn/new/1.json ... 10.json    - Newest 10 stories
    /hn/best/1.json ... 10.json   - Best 10 stories
    /hn/ask/1.json ... 10.json    - Ask HN posts
    /hn/show/1.json ... 10.json   - Show HN posts
    /hn/jobs/1.json ... 10.json   - Job listings

Features:
- Read-only access to HackerNews
- Virtual path mapping (stories → JSON files)
- TTL-based caching via CacheConnectorMixin
- Nested comments included in story files
- No authentication required (public API)

HackerNews API:
- Base URL: https://hacker-news.firebaseio.com/v0/
- No rate limit documented
- Items are immutable once created

Example:
    >>> from nexus import NexusFS
    >>> from nexus.backends import HNConnectorBackend
    >>>
    >>> nx = NexusFS(backend=HNConnectorBackend())
    >>>
    >>> # Read top story
    >>> story = nx.read("/hn/top/1.json")
    >>>
    >>> # List all feeds
    >>> nx.ls("/hn/")
"""

import asyncio
import json
import logging
import time
from typing import TYPE_CHECKING, Any

import httpx

from nexus.backends.backend import Backend
from nexus.backends.cache_mixin import CacheConnectorMixin, SyncResult
from nexus.backends.registry import ArgType, ConnectionArg, register_connector
from nexus.connectors.base import SkillDocMixin
from nexus.core.exceptions import BackendError, NexusFileNotFoundError
from nexus.core.response import HandlerResponse

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from nexus.core.permissions import OperationContext

logger = logging.getLogger(__name__)

# HackerNews API base URL
HN_API_BASE = "https://hacker-news.firebaseio.com/v0"

# Cache TTL configuration (in seconds)
DEFAULT_CACHE_TTL = {
    "top": 300,  # 5 minutes - changes frequently
    "new": 60,  # 1 minute - changes very frequently
    "best": 3600,  # 1 hour - relatively stable
    "ask": 300,  # 5 minutes
    "show": 300,  # 5 minutes
    "jobs": 3600,  # 1 hour - changes slowly
}

# Number of stories per feed
DEFAULT_STORIES_PER_FEED = 10

# Maximum comments to fetch (to avoid very long load times)
MAX_COMMENTS_DEPTH = 5
MAX_COMMENTS_TOTAL = 100


@register_connector(
    "hn_connector",
    description="HackerNews API (read-only)",
    category="api",
    requires=["httpx"],
)
class HNConnectorBackend(Backend, CacheConnectorMixin, SkillDocMixin):
    """
    HackerNews connector backend with virtual filesystem mapping.

    Maps HN API to virtual filesystem:
    - /hn/top/1.json ... 10.json → Top stories with comments
    - /hn/new/1.json ... 10.json → New stories
    - /hn/best/1.json ... 10.json → Best stories
    - /hn/ask/1.json ... 10.json → Ask HN posts
    - /hn/show/1.json ... 10.json → Show HN posts
    - /hn/jobs/1.json ... 10.json → Job listings

    Features:
    - Read-only (HN API doesn't support posting)
    - TTL-based caching via CacheConnectorMixin
    - Nested comments included in story files
    - No authentication required

    Limitations:
    - Read-only (no write/delete operations)
    - Fixed virtual directory structure
    - External article content not included (just URLs)
    """

    # Skill documentation settings
    SKILL_NAME = "hn"

    user_scoped = False  # Public API, no per-user auth
    has_virtual_filesystem = True  # Uses virtual directory structure, not metadata-backed

    CONNECTION_ARGS: dict[str, ConnectionArg] = {
        "cache_ttl": ConnectionArg(
            type=ArgType.INTEGER,
            description="Default cache TTL in seconds",
            required=False,
            default=300,
        ),
        "stories_per_feed": ConnectionArg(
            type=ArgType.INTEGER,
            description="Number of stories per feed (1-30)",
            required=False,
            default=10,
        ),
        "include_comments": ConnectionArg(
            type=ArgType.BOOLEAN,
            description="Include nested comments in story files",
            required=False,
            default=True,
        ),
    }

    def __init__(
        self,
        cache_ttl: int = 300,
        stories_per_feed: int = 10,
        include_comments: bool = True,
        # Database session for L2 caching (optional)
        session_factory: "type[Session] | None" = None,
    ):
        """
        Initialize HackerNews connector.

        Args:
            cache_ttl: Default cache TTL in seconds (default: 300)
            stories_per_feed: Number of stories per feed, 1-30 (default: 10)
            include_comments: Include nested comments in story files (default: True)
            session_factory: Optional session factory for L2 caching
        """
        self.cache_ttl = cache_ttl
        self.stories_per_feed = min(max(stories_per_feed, 1), 30)
        self.include_comments = include_comments
        self.session_factory = session_factory

        # HTTP client for HN API
        self._client: httpx.AsyncClient | None = None

    @property
    def name(self) -> str:
        """Backend identifier name."""
        return "hn"

    def generate_skill_doc(self, mount_path: str) -> str:
        """Load SKILL.md from static file.

        Args:
            mount_path: The mount path for this connector instance

        Returns:
            SKILL.md content with mount path substituted
        """
        import importlib.resources as resources

        try:
            content = (
                resources.files("nexus.connectors.hn")
                .joinpath("SKILL.md")
                .read_text(encoding="utf-8")
            )
            # Replace mount path placeholder
            content = content.replace("/mnt/hn/", mount_path)
            return content
        except Exception:
            return super().generate_skill_doc(mount_path)

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

    # === HN API Methods ===

    async def _fetch_item(self, item_id: int) -> dict[str, Any] | None:
        """Fetch a single item from HN API."""
        client = await self._get_client()
        try:
            response = await client.get(f"/item/{item_id}.json")
            response.raise_for_status()
            result: dict[str, Any] | None = response.json()
            return result
        except Exception as e:
            logger.warning(f"Failed to fetch item {item_id}: {e}")
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

        # Limit how many we fetch at this level
        remaining = MAX_COMMENTS_TOTAL - total_fetched[0]
        ids_to_fetch = comment_ids[:remaining]

        comments = await self._fetch_items_batch(ids_to_fetch)
        total_fetched[0] += len(comments)

        # Recursively fetch replies
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

        # Fetch comments if requested
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
            include_comments=self.include_comments,
        )

        # Add rank metadata
        story["_rank"] = rank
        story["_feed"] = feed

        return story

    # === Path Resolution ===

    def _resolve_path(self, path: str) -> tuple[str, int | None]:
        """
        Resolve virtual path to feed and rank.

        Args:
            path: Virtual path (e.g., "top/1.json", "new/3.json")

        Returns:
            Tuple of (feed, rank) where rank is 1-based or None for directory

        Raises:
            BackendError: If path is invalid
        """
        path = path.strip("/")
        if not path:
            # Root directory
            return ("", None)

        parts = path.split("/")

        # Handle paths with "hn" prefix
        if parts and parts[0] == "hn":
            parts = parts[1:]

        if not parts or parts[0] == "":
            # Root directory (after removing hn prefix)
            return ("", None)

        feed = parts[0]
        valid_feeds = {"top", "new", "best", "ask", "show", "jobs"}

        if feed not in valid_feeds:
            raise BackendError(f"Unknown feed: {feed}. Valid: {valid_feeds}", backend="hn")

        if len(parts) == 1:
            # Feed directory (e.g., /hn/top/)
            return (feed, None)

        if len(parts) == 2:
            # Story file (e.g., /hn/top/1.json)
            filename = parts[1]
            if not filename.endswith(".json"):
                raise BackendError(f"Invalid file: {filename}", backend="hn")

            try:
                rank = int(filename.replace(".json", ""))
                if rank < 1 or rank > self.stories_per_feed:
                    raise BackendError(
                        f"Rank {rank} out of range (1-{self.stories_per_feed})",
                        backend="hn",
                    )
                return (feed, rank)
            except ValueError as e:
                raise BackendError(f"Invalid rank in {filename}", backend="hn") from e

        raise BackendError(f"Invalid path: {path}", backend="hn")

    # === Backend Interface Implementation ===

    def read_content(
        self,
        content_hash: str,
        context: "OperationContext | None" = None,
    ) -> HandlerResponse[bytes]:
        """
        Read content from HN API via virtual path.

        For HN connector, content_hash is ignored - we use backend_path from context.

        Args:
            content_hash: Ignored for HN connector
            context: Operation context with backend_path

        Returns:
            HandlerResponse with JSON content as bytes in data field
        """
        start_time = time.perf_counter()

        if not context or not context.backend_path:
            return HandlerResponse.error(
                message="HN connector requires context with backend_path",
                code=400,
                is_expected=True,
                execution_time_ms=(time.perf_counter() - start_time) * 1000,
                backend_name=self.name,
            )

        path = context.backend_path
        cache_path = self._get_cache_path(context) or path

        try:
            # Check cache first (if caching enabled)
            if self._has_caching():
                cached = self._read_from_cache(cache_path, original=True)
                if cached and not cached.stale and cached.content_binary:
                    logger.info(f"[HN] Cache hit: {path}")
                    return HandlerResponse.ok(
                        data=cached.content_binary,
                        execution_time_ms=(time.perf_counter() - start_time) * 1000,
                        backend_name=self.name,
                        path=path,
                    )

            # Resolve path
            feed, rank = self._resolve_path(path)

            if rank is None:
                return HandlerResponse.error(
                    message=f"Cannot read directory: {path}. Use list_dir() instead.",
                    code=400,
                    is_expected=True,
                    execution_time_ms=(time.perf_counter() - start_time) * 1000,
                    backend_name=self.name,
                    path=path,
                )

            # Fetch from HN API
            logger.info(f"[HN] Fetching from API: {feed}/{rank}")

            async def _fetch() -> bytes:
                try:
                    story = await self._fetch_feed_story(feed, rank)
                    content = json.dumps(story, indent=2, ensure_ascii=False).encode("utf-8")
                    return content
                finally:
                    await self._close_client()

            content = asyncio.run(_fetch())

            # Cache the result
            if self._has_caching():
                try:
                    tenant_id = getattr(context, "tenant_id", None)
                    self._write_to_cache(
                        path=cache_path,
                        content=content,
                        backend_version=None,  # No versioning for HN
                        tenant_id=tenant_id,
                    )
                except Exception as e:
                    logger.warning(f"Failed to cache {path}: {e}")

            return HandlerResponse.ok(
                data=content,
                execution_time_ms=(time.perf_counter() - start_time) * 1000,
                backend_name=self.name,
                path=path,
            )

        except Exception as e:
            return HandlerResponse.from_exception(
                e,
                execution_time_ms=(time.perf_counter() - start_time) * 1000,
                backend_name=self.name,
                path=path,
            )

    def write_content(
        self,
        content: bytes,
        context: "OperationContext | None" = None,
    ) -> HandlerResponse[str]:
        """Write content (not supported - HN is read-only)."""
        return HandlerResponse.error(
            message="HN connector is read-only. HackerNews API does not support posting.",
            code=405,
            is_expected=True,
            execution_time_ms=0.0,
            backend_name=self.name,
        )

    def delete_content(
        self,
        content_hash: str,
        context: "OperationContext | None" = None,
    ) -> HandlerResponse[None]:
        """Delete content (not supported - HN is read-only)."""
        return HandlerResponse.error(
            message="HN connector is read-only. HackerNews API does not support deletion.",
            code=405,
            is_expected=True,
            execution_time_ms=0.0,
            backend_name=self.name,
        )

    def content_exists(
        self,
        content_hash: str,
        context: "OperationContext | None" = None,
    ) -> HandlerResponse[bool]:
        """Check if content exists."""
        start_time = time.perf_counter()

        if not context or not context.backend_path:
            return HandlerResponse.ok(
                data=False,
                execution_time_ms=(time.perf_counter() - start_time) * 1000,
                backend_name=self.name,
            )

        try:
            feed, rank = self._resolve_path(context.backend_path)
            exists = feed != "" and (rank is None or 1 <= rank <= self.stories_per_feed)
            return HandlerResponse.ok(
                data=exists,
                execution_time_ms=(time.perf_counter() - start_time) * 1000,
                backend_name=self.name,
                path=context.backend_path,
            )
        except BackendError:
            return HandlerResponse.ok(
                data=False,
                execution_time_ms=(time.perf_counter() - start_time) * 1000,
                backend_name=self.name,
            )

    def get_content_size(
        self,
        content_hash: str,
        context: "OperationContext | None" = None,
    ) -> HandlerResponse[int]:
        """Get content size (cache-first, efficient).

        Performance optimization: Checks cache first for actual size.
        Falls back to 10KB estimate if not cached.
        """
        start_time = time.perf_counter()

        # OPTIMIZATION: Check cache first for actual size
        if context and hasattr(context, "virtual_path") and context.virtual_path:
            cached_size = self._get_size_from_cache(context.virtual_path)
            if cached_size is not None:
                return HandlerResponse.ok(
                    data=cached_size,
                    execution_time_ms=(time.perf_counter() - start_time) * 1000,
                    backend_name=self.name,
                )

        # Fallback: Return approximate size estimate
        return HandlerResponse.ok(
            data=10 * 1024,  # 10 KB estimate
            execution_time_ms=(time.perf_counter() - start_time) * 1000,
            backend_name=self.name,
        )

    def get_ref_count(
        self,
        content_hash: str,
        context: "OperationContext | None" = None,
    ) -> HandlerResponse[int]:
        """Get reference count (always 1 for HN connector)."""
        return HandlerResponse.ok(
            data=1,
            execution_time_ms=0.0,
            backend_name=self.name,
        )

    # === Directory Operations ===

    def mkdir(
        self,
        path: str,
        parents: bool = False,
        exist_ok: bool = False,
        context: "OperationContext | None" = None,
    ) -> HandlerResponse[None]:
        """Create directory (not supported - fixed structure)."""
        return HandlerResponse.error(
            message="HN connector has a fixed virtual structure. mkdir() is not supported.",
            code=405,
            is_expected=True,
            execution_time_ms=0.0,
            backend_name=self.name,
            path=path,
        )

    def rmdir(
        self,
        path: str,
        recursive: bool = False,
        context: "OperationContext | None" = None,
    ) -> HandlerResponse[None]:
        """Remove directory (not supported - fixed structure)."""
        return HandlerResponse.error(
            message="HN connector has a fixed virtual structure. rmdir() is not supported.",
            code=405,
            is_expected=True,
            execution_time_ms=0.0,
            backend_name=self.name,
            path=path,
        )

    def is_directory(
        self,
        path: str,
        context: "OperationContext | None" = None,
    ) -> HandlerResponse[bool]:
        """Check if path is a directory."""
        start_time = time.perf_counter()

        path = path.strip("/")

        # Handle hn prefix
        if path.startswith("hn/"):
            path = path[3:]

        # Root or feed directories
        if path == "" or path == "hn":
            return HandlerResponse.ok(
                data=True,
                execution_time_ms=(time.perf_counter() - start_time) * 1000,
                backend_name=self.name,
                path=path,
            )

        is_dir = path in {"top", "new", "best", "ask", "show", "jobs"}
        return HandlerResponse.ok(
            data=is_dir,
            execution_time_ms=(time.perf_counter() - start_time) * 1000,
            backend_name=self.name,
            path=path,
        )

    def list_dir(
        self,
        path: str,
        context: "OperationContext | None" = None,
    ) -> list[str]:
        """
        List virtual directory contents.

        Args:
            path: Directory path to list
            context: Operation context

        Returns:
            List of entry names (directories have trailing '/')

        Raises:
            FileNotFoundError: If directory doesn't exist
        """
        path = path.strip("/")

        # Handle hn prefix
        if path.startswith("hn/"):
            path = path[3:]

        # Root directory
        if path == "" or path == "hn":
            return [
                "top/",
                "new/",
                "best/",
                "ask/",
                "show/",
                "jobs/",
            ]

        # Feed directory
        if path in {"top", "new", "best", "ask", "show", "jobs"}:
            return [f"{i}.json" for i in range(1, self.stories_per_feed + 1)]

        raise FileNotFoundError(f"Directory not found: {path}")

    # === Sync Operation ===

    def sync(
        self,
        path: str | None = None,
        mount_point: str | None = None,
        include_patterns: list[str] | None = None,
        exclude_patterns: list[str] | None = None,
        max_file_size: int | None = None,
        generate_embeddings: bool = False,
        context: "OperationContext | None" = None,
    ) -> SyncResult:
        """
        Sync HN content to cache.

        For HN connector, sync pre-fetches stories and caches them.

        Args:
            path: Specific feed to sync (e.g., "top") or None for all
            mount_point: Virtual mount point
            include_patterns: Not used for HN
            exclude_patterns: Not used for HN
            max_file_size: Not used for HN
            generate_embeddings: Generate embeddings for stories
            context: Operation context

        Returns:
            SyncResult with statistics
        """
        result = SyncResult()

        # Determine which feeds to sync
        if path:
            path = path.strip("/")
            if path.startswith("hn/"):
                path = path[3:]
            feeds = [path] if path in {"top", "new", "best", "ask", "show", "jobs"} else []
        else:
            feeds = ["top", "new", "best", "ask", "show", "jobs"]

        if not feeds:
            return result

        async def _sync_feeds() -> None:
            for feed in feeds:
                try:
                    # Fetch story IDs
                    story_ids = await self._fetch_story_ids(feed)
                    ids_to_sync = story_ids[: self.stories_per_feed]
                    result.files_scanned += len(ids_to_sync)

                    # Fetch each story
                    for rank, story_id in enumerate(ids_to_sync, start=1):
                        try:
                            story = await self._fetch_story_with_comments(
                                story_id,
                                include_comments=self.include_comments,
                            )
                            story["_rank"] = rank
                            story["_feed"] = feed

                            content = json.dumps(story, indent=2, ensure_ascii=False).encode(
                                "utf-8"
                            )

                            # Cache if enabled
                            if self._has_caching():
                                backend_path = f"{feed}/{rank}.json"
                                virtual_path = (
                                    f"{mount_point.rstrip('/')}/{backend_path}"
                                    if mount_point
                                    else f"/{backend_path}"
                                )

                                tenant_id = getattr(context, "tenant_id", None)
                                self._write_to_cache(
                                    path=virtual_path,
                                    content=content,
                                    backend_version=None,
                                    tenant_id=tenant_id,
                                )

                            result.files_synced += 1
                            result.bytes_synced += len(content)

                        except Exception as e:
                            result.errors.append(f"Failed to sync {feed}/{rank}: {e}")

                except Exception as e:
                    result.errors.append(f"Failed to sync feed {feed}: {e}")

            await self._close_client()

        asyncio.run(_sync_feeds())
        return result
