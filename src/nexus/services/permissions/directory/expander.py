"""Directory Permission Expander — Leopard-style pre-materialization.

Handles directory path detection, grant expansion to descendants, and
descendant querying for the Leopard write-amplification pattern.

When a permission is granted on a directory, this expander pre-materializes
the grant to all descendant files so permission checks become O(1) bitmap
lookups instead of O(depth) tree walks.

Related: Issue #1459 Phase 13, Leopard pattern
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from sqlalchemy.exc import OperationalError

from nexus.services.permissions.consistency.revision import get_zone_revision_for_grant

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

    from nexus.services.permissions.cache.tiger.bitmap_cache import TigerCache

logger = logging.getLogger(__name__)

# Write amplification limit: max files to expand synchronously.
# Beyond this, expansion is queued for async processing.
DIRECTORY_EXPANSION_LIMIT = 10_000

# Common file extensions that indicate NOT a directory
_FILE_EXTENSIONS = frozenset(
    {
        "txt",
        "md",
        "json",
        "yaml",
        "yml",
        "xml",
        "csv",
        "tsv",
        "py",
        "js",
        "ts",
        "jsx",
        "tsx",
        "html",
        "css",
        "scss",
        "java",
        "c",
        "cpp",
        "h",
        "hpp",
        "go",
        "rs",
        "rb",
        "php",
        "sql",
        "sh",
        "bash",
        "zsh",
        "ps1",
        "bat",
        "cmd",
        "png",
        "jpg",
        "jpeg",
        "gif",
        "svg",
        "ico",
        "webp",
        "pdf",
        "doc",
        "docx",
        "xls",
        "xlsx",
        "ppt",
        "pptx",
        "zip",
        "tar",
        "gz",
        "bz2",
        "7z",
        "rar",
        "mp3",
        "mp4",
        "wav",
        "avi",
        "mov",
        "mkv",
        "log",
        "ini",
        "conf",
        "cfg",
        "env",
        "lock",
    }
)


class DirectoryExpander:
    """Expands directory permission grants to descendant files.

    Implements the Leopard pattern: when a permission is granted on a
    directory path, expand to ALL descendants so checks become O(1).

    Args:
        engine: SQLAlchemy engine for DB queries
        tiger_cache: Tiger bitmap cache (may be None if disabled)
        metadata_store: Optional metadata store for directory queries
    """

    def __init__(
        self,
        engine: Engine,
        tiger_cache: TigerCache | None = None,
        metadata_store: Any | None = None,
    ) -> None:
        self._engine = engine
        self._tiger_cache = tiger_cache
        self._metadata_store = metadata_store

    def set_metadata_store(self, metadata_store: Any) -> None:
        """Set the metadata store reference for directory queries."""
        self._metadata_store = metadata_store

    # -- Path detection ----------------------------------------------------

    def is_directory_path(self, path: str) -> bool:
        """Check if a path represents a directory.

        Uses heuristics since NexusFS uses implicit directories:
        1. Path ends with /
        2. Path has no file extension in the last component
        3. Files exist under this path (queried from metadata store if available)
        """
        # Explicit directory marker
        if path.endswith("/"):
            return True

        # Root is always a directory
        if path == "/":
            return True

        # Check for common file extensions (not a directory)
        last_component = path.rsplit("/", 1)[-1]
        if "." in last_component:
            extension = last_component.rsplit(".", 1)[-1].lower()
            if extension in _FILE_EXTENSIONS:
                return False

        # If we have a metadata store reference, check for children
        if self._metadata_store:
            try:
                return bool(self._metadata_store.is_implicit_directory(path))
            except (RuntimeError, OperationalError) as e:
                logger.debug("[LEOPARD] Failed to check directory via metadata: %s", e)

        # Default: treat paths without extensions as potential directories
        return "." not in last_component

    # -- Grant expansion ---------------------------------------------------

    def expand_directory_permission_grant(
        self,
        subject: tuple[str, str],
        permissions: list[str],
        directory_path: str,
        zone_id: str,
    ) -> None:
        """Expand a directory permission grant to all descendants (Leopard-style).

        Trade-offs (Zanzibar Leopard pattern):
            - Write amplification: 1 grant -> N bitmap updates
            - Read optimization: O(depth) -> O(1) per file
            - Storage: O(grants) -> O(grants * avg_descendants)
        """
        if not self._tiger_cache:
            return

        # Normalize directory path
        if not directory_path.endswith("/"):
            directory_path = directory_path + "/"

        # Get current revision for consistency (prevents "new enemy" problem)
        grant_revision = get_zone_revision_for_grant(self._engine, zone_id)

        # Get all descendants of the directory
        descendants = self.get_directory_descendants(directory_path, zone_id)

        logger.info(
            "[LEOPARD] Directory grant expansion: %s -> %d descendants for %s:%s",
            directory_path,
            len(descendants),
            subject[0],
            subject[1],
        )

        if not descendants:
            # No descendants - just record the grant for future file integration
            for permission in permissions:
                self._tiger_cache.record_directory_grant(
                    subject_type=subject[0],
                    subject_id=subject[1],
                    permission=permission,
                    directory_path=directory_path,
                    zone_id=zone_id,
                    grant_revision=grant_revision,
                    include_future_files=True,
                )
                # Mark as completed immediately (empty directory)
                self._tiger_cache._update_grant_status(
                    subject[0],
                    subject[1],
                    permission,
                    directory_path,
                    zone_id,
                    status="completed",
                    expanded_count=0,
                    total_count=0,
                )
            return

        # Check write amplification limit
        if len(descendants) > DIRECTORY_EXPANSION_LIMIT:
            logger.warning(
                "[LEOPARD] Directory %s has %d files, exceeds limit %d. Using async expansion.",
                directory_path,
                len(descendants),
                DIRECTORY_EXPANSION_LIMIT,
            )
            # Queue for async expansion
            for permission in permissions:
                self._tiger_cache.record_directory_grant(
                    subject_type=subject[0],
                    subject_id=subject[1],
                    permission=permission,
                    directory_path=directory_path,
                    zone_id=zone_id,
                    grant_revision=grant_revision,
                    include_future_files=True,
                )
                # Status remains "pending" - background worker will process
            return

        # Synchronous expansion for small directories
        for permission in permissions:
            # Record the directory grant first
            self._tiger_cache.record_directory_grant(
                subject_type=subject[0],
                subject_id=subject[1],
                permission=permission,
                directory_path=directory_path,
                zone_id=zone_id,
                grant_revision=grant_revision,
                include_future_files=True,
            )

            # Expand to all descendants
            expanded, completed = self._tiger_cache.expand_directory_grant(
                subject_type=subject[0],
                subject_id=subject[1],
                permission=permission,
                directory_path=directory_path,
                zone_id=zone_id,
                grant_revision=grant_revision,
                descendants=descendants,
            )

            if completed:
                logger.info(
                    "[LEOPARD] Expanded %s on %s: %d files for %s:%s",
                    permission,
                    directory_path,
                    expanded,
                    subject[0],
                    subject[1],
                )
            else:
                logger.error("[LEOPARD] Failed to expand %s on %s", permission, directory_path)

    # -- Descendant queries ------------------------------------------------

    def get_directory_descendants(
        self,
        directory_path: str,
        zone_id: str,
    ) -> list[str]:
        """Get all file paths under a directory.

        Returns:
            List of descendant file paths.
        """
        # Try using metadata store if available
        if self._metadata_store:
            try:
                files = self._metadata_store.list(
                    prefix=directory_path,
                    recursive=True,
                    zone_id=zone_id,
                )
                return [f.path for f in files]
            except (RuntimeError, OperationalError) as e:
                logger.warning("[LEOPARD] Metadata store query failed: %s", e)

        # Fallback: query file_paths table directly
        from sqlalchemy import text

        try:
            query = text("""
                SELECT virtual_path
                FROM file_paths
                WHERE virtual_path LIKE :prefix
                  AND deleted_at IS NULL
                  AND (zone_id = :zone_id OR zone_id = 'default' OR zone_id IS NULL)
            """)

            with self._engine.connect() as conn:
                result = conn.execute(query, {"prefix": f"{directory_path}%", "zone_id": zone_id})
                return [row.virtual_path for row in result]
        except (RuntimeError, OperationalError) as e:
            logger.error("[LEOPARD] Failed to query descendants: %s", e)
            return []
