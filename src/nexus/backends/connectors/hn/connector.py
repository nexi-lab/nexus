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

import logging
from typing import TYPE_CHECKING, ClassVar

from nexus.backends.base.path_addressing_engine import PathAddressingEngine
from nexus.backends.base.registry import ArgType, ConnectionArg, register_connector
from nexus.backends.connectors.base import ReadmeDocMixin
from nexus.backends.connectors.hn.transport import VALID_FEEDS, HNTransport
from nexus.contracts.backend_features import BackendFeature
from nexus.contracts.exceptions import BackendError
from nexus.core.object_store import WriteResult

if TYPE_CHECKING:
    from nexus.contracts.types import OperationContext
    from nexus.storage.record_store import RecordStoreABC

logger = logging.getLogger(__name__)


@register_connector("hn_connector")
class PathHNBackend(
    PathAddressingEngine,
    ReadmeDocMixin,
):
    """HackerNews connector: PathAddressingEngine + HNTransport composition.

    Features:
    - Read-only (HN API doesn't support posting)
    - Nested comments included in story files
    - No authentication required (public API)
    """

    _BACKEND_FEATURES: ClassVar[frozenset[BackendFeature]] = frozenset(
        {
            BackendFeature.README_DOC,
        }
    )

    # Skill documentation settings
    SKILL_NAME = "hn"

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

    def generate_readme(self, mount_path: str) -> str:
        """Load README.md from static file."""
        import importlib.resources as resources

        try:
            content = (
                resources.files("nexus.backends.connectors.hn")
                .joinpath("README.md")
                .read_text(encoding="utf-8")
            )
            content = content.replace("/mnt/hn/", mount_path)
            return content
        except Exception:
            return super().generate_readme(mount_path)

    # =================================================================
    # Content operations -- override PathAddressingEngine for HN
    # =================================================================

    def read_content(
        self,
        content_id: str,
        context: "OperationContext | None" = None,
    ) -> bytes:
        """Read content from HN API via virtual path.

        For HN connector, content_id is ignored -- we use backend_path from context.
        """
        if not context or not context.backend_path:
            raise BackendError(
                "HN connector requires context with backend_path",
                backend="hn",
            )

        # Delegate to PathAddressingEngine (which calls transport.fetch)
        return super().read_content(content_id, context)

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
        content_id: str,
        context: "OperationContext | None" = None,
    ) -> None:
        raise BackendError(
            "HN connector is read-only. HackerNews API does not support deletion.",
            backend="hn",
        )

    def content_exists(
        self,
        content_id: str,
        context: "OperationContext | None" = None,
    ) -> bool:
        if not context or not context.backend_path:
            return False
        return super().content_exists(content_id, context)

    def get_content_size(
        self,
        content_id: str,
        context: "OperationContext | None" = None,
    ) -> int:
        """Get content size (approximate estimate)."""
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
