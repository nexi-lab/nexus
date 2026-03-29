"""HackerNews connector backend -- PathAddressingEngine + HNTransport composition.

Architecture (Transport x Addressing):
    PathHNBackend(PathAddressingEngine)
        +-- HNTransport(Transport)
              +-- HN Firebase API calls (I/O)
              +-- No authentication (public API)

This follows the same pattern as PathGmailBackend, PathCalendarBackend:
Transport handles raw I/O; PathAddressingEngine handles addressing,
path security, and content operations.

Virtual filesystem structure:
    /top/1.json ... 10.json    - Top 10 stories with comments
    /new/1.json ... 10.json    - Newest 10 stories
    /best/1.json ... 10.json   - Best 10 stories
    /ask/1.json ... 10.json    - Ask HN posts
    /show/1.json ... 10.json   - Show HN posts
    /jobs/1.json ... 10.json   - Job listings
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, ClassVar

from nexus.backends.base.path_addressing_engine import PathAddressingEngine
from nexus.backends.base.registry import ArgType, ConnectionArg, register_connector
from nexus.backends.connectors.base import SkillDocMixin
from nexus.backends.connectors.hn.transport import VALID_FEEDS, HNTransport
from nexus.backends.wrappers.cache_mixin import CacheConnectorMixin, SyncResult
from nexus.contracts.backend_features import BackendFeature
from nexus.contracts.exceptions import BackendError
from nexus.core.object_store import WriteResult

if TYPE_CHECKING:
    from nexus.contracts.types import OperationContext
    from nexus.storage.record_store import RecordStoreABC

logger = logging.getLogger(__name__)


@register_connector(
    "hn_connector",
    description="HackerNews API (read-only)",
    category="api",
    requires=["httpx"],
    service_name="hackernews",
)
class PathHNBackend(
    PathAddressingEngine,
    CacheConnectorMixin,
    SkillDocMixin,
):
    """HackerNews connector: PathAddressingEngine + HNTransport composition.

    Features:
    - Read-only (HN API doesn't support posting)
    - TTL-based caching via CacheConnectorMixin
    - Nested comments included in story files
    - No authentication required (public API)
    """

    _BACKEND_FEATURES: ClassVar[frozenset[BackendFeature]] = frozenset(
        {
            BackendFeature.CACHE_BULK_READ,
            BackendFeature.CACHE_SYNC,
            BackendFeature.SKILL_DOC,
            BackendFeature.SYNC,
        }
    )

    # Skill documentation settings
    SKILL_NAME = "hn"

    user_scoped = False  # Public API, no per-user auth

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
        record_store: "RecordStoreABC | None" = None,
    ):
        """Initialize HackerNews connector.

        Args:
            cache_ttl: Default cache TTL in seconds (default: 300)
            stories_per_feed: Number of stories per feed, 1-30 (default: 10)
            include_comments: Include nested comments in story files (default: True)
            record_store: Optional RecordStoreABC instance for L2 caching
        """
        self.cache_ttl = cache_ttl
        self.stories_per_feed = min(max(stories_per_feed, 1), 30)
        self.include_comments = include_comments

        # 1. Create HNTransport
        hn_transport = HNTransport(
            stories_per_feed=self.stories_per_feed,
            include_comments=include_comments,
        )
        self._hn_transport = hn_transport

        # 2. Initialize PathAddressingEngine
        PathAddressingEngine.__init__(
            self,
            transport=hn_transport,
            backend_name="hn",
        )

        # 3. Cache setup
        self.session_factory = record_store.session_factory if record_store else None

    # -- Skill docs --

    def generate_skill_doc(self, mount_path: str) -> str:
        """Load SKILL.md from static file."""
        import importlib.resources as resources

        try:
            content = (
                resources.files("nexus.backends.connectors.hn")
                .joinpath("SKILL.md")
                .read_text(encoding="utf-8")
            )
            content = content.replace("/mnt/hn/", mount_path)
            return content
        except Exception:
            return super().generate_skill_doc(mount_path)

    # =================================================================
    # Content operations -- override PathAddressingEngine for HN
    # =================================================================

    def read_content(
        self,
        content_hash: str,
        context: "OperationContext | None" = None,
    ) -> bytes:
        """Read content from HN API via virtual path, with cache check.

        For HN connector, content_hash is ignored -- we use backend_path from context.
        """
        if not context or not context.backend_path:
            raise BackendError(
                "HN connector requires context with backend_path",
                backend="hn",
            )

        path = context.backend_path
        cache_path = self._get_cache_path(context) or path

        # Check cache first (if caching enabled)
        if self._has_caching():
            cached = self._read_from_cache(cache_path, original=True)
            if cached and not cached.stale and cached.content_binary:
                logger.info("[HN] Cache hit: %s", path)
                return cached.content_binary

        # Delegate to PathAddressingEngine (which calls transport.fetch)
        content = super().read_content(content_hash, context)

        # Cache the result
        if self._has_caching():
            try:
                zone_id = getattr(context, "zone_id", None)
                self._write_to_cache(
                    path=cache_path,
                    content=content,
                    backend_version=None,
                    zone_id=zone_id,
                )
            except Exception as e:
                logger.warning("Failed to cache %s: %s", path, e)

        return content

    def write_content(
        self,
        content: bytes,
        content_id: str = "",
        *,
        offset: int = 0,
        context: "OperationContext | None" = None,
    ) -> WriteResult:
        raise BackendError(
            "HN connector is read-only. HackerNews API does not support posting.",
            backend="hn",
        )

    def delete_content(
        self,
        content_hash: str,
        context: "OperationContext | None" = None,
    ) -> None:
        raise BackendError(
            "HN connector is read-only. HackerNews API does not support deletion.",
            backend="hn",
        )

    def content_exists(
        self,
        content_hash: str,
        context: "OperationContext | None" = None,
    ) -> bool:
        if not context or not context.backend_path:
            return False
        return super().content_exists(content_hash, context)

    def get_content_size(
        self,
        content_hash: str,
        context: "OperationContext | None" = None,
    ) -> int:
        """Get content size (cache-first, efficient)."""
        # Cache optimization: check cache first for actual size
        if context and hasattr(context, "virtual_path") and context.virtual_path:
            cached_size = self._get_size_from_cache(context.virtual_path)
            if cached_size is not None:
                return cached_size

        # Fallback: return approximate size estimate
        return 10 * 1024  # 10 KB estimate

    # =================================================================
    # Directory operations -- override for HN virtual directories
    # =================================================================

    def mkdir(
        self,
        path: str,
        parents: bool = False,
        exist_ok: bool = False,
        context: "OperationContext | None" = None,
    ) -> None:
        raise BackendError(
            "HN connector has a fixed virtual structure. mkdir() is not supported.",
            backend="hn",
        )

    def rmdir(
        self,
        path: str,
        recursive: bool = False,
        context: "OperationContext | None" = None,
    ) -> None:
        raise BackendError(
            "HN connector has a fixed virtual structure. rmdir() is not supported.",
            backend="hn",
        )

    def is_directory(
        self,
        path: str,
        context: "OperationContext | None" = None,
    ) -> bool:
        path = path.strip("/")
        if path.startswith("hn/"):
            path = path[3:]
        if path == "" or path == "hn":
            return True
        return path in VALID_FEEDS

    def list_dir(
        self,
        path: str,
        context: "OperationContext | None" = None,
    ) -> list[str]:
        """List virtual directory contents via HNTransport.list_keys()."""
        path = path.strip("/")

        # Handle hn prefix
        if path.startswith("hn/"):
            path = path[3:]

        keys, prefixes = self._transport.list_keys(prefix=path, delimiter="/")

        # Strip prefix from keys to return just filenames
        entries: list[str] = []
        folder_prefix = f"{path}/" if path else ""
        for key in keys:
            name = (
                key[len(folder_prefix) :]
                if folder_prefix and key.startswith(folder_prefix)
                else key
            )
            if name:
                entries.append(name)
        for prefix_entry in prefixes:
            name = (
                prefix_entry[len(folder_prefix) :]
                if folder_prefix and prefix_entry.startswith(folder_prefix)
                else prefix_entry
            )
            if name:
                entries.append(name)

        if not entries and path and path not in VALID_FEEDS and path != "hn":
            raise FileNotFoundError(f"Directory not found: {path}")

        return sorted(entries)

    # =================================================================
    # Sync operation -- pre-fetch stories to cache
    # =================================================================

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
        """Sync HN content to cache.

        For HN connector, sync pre-fetches stories and caches them.
        """
        result = SyncResult()

        # Determine which feeds to sync
        if path:
            path_clean = path.strip("/")
            if path_clean.startswith("hn/"):
                path_clean = path_clean[3:]
            feeds = [path_clean] if path_clean in VALID_FEEDS else []
        else:
            feeds = list(VALID_FEEDS)

        if not feeds:
            return result

        async def _sync_feeds() -> None:
            for feed in feeds:
                try:
                    story_ids = await self._hn_transport._fetch_story_ids(feed)
                    ids_to_sync = story_ids[: self.stories_per_feed]
                    result.files_scanned += len(ids_to_sync)

                    for rank, story_id in enumerate(ids_to_sync, start=1):
                        try:
                            story = await self._hn_transport._fetch_story_with_comments(
                                story_id,
                                include_comments=self.include_comments,
                            )
                            story["_rank"] = rank
                            story["_feed"] = feed

                            content = json.dumps(story, indent=2, ensure_ascii=False).encode(
                                "utf-8"
                            )

                            if self._has_caching():
                                backend_path = f"{feed}/{rank}.json"
                                virtual_path = (
                                    f"{mount_point.rstrip('/')}/{backend_path}"
                                    if mount_point
                                    else f"/{backend_path}"
                                )

                                zone_id = getattr(context, "zone_id", None)
                                self._write_to_cache(
                                    path=virtual_path,
                                    content=content,
                                    backend_version=None,
                                    zone_id=zone_id,
                                )

                            result.files_synced += 1
                            result.bytes_synced += len(content)

                        except Exception as e:
                            result.errors.append(f"Failed to sync {feed}/{rank}: {e}")

                except Exception as e:
                    result.errors.append(f"Failed to sync feed {feed}: {e}")

            await self._hn_transport._close_client()

        from nexus.lib.sync_bridge import run_sync

        run_sync(_sync_feeds())
        return result
