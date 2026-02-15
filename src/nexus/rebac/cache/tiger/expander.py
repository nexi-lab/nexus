"""Directory grant expansion worker for Tiger Cache (Leopard-style).

Processes large directory grants asynchronously by expanding them into
individual file-level bitmap entries in batches, enabling non-blocking
grant operations, progress tracking, and failure recovery.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from sqlalchemy import text

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

    from nexus.rebac.cache.tiger.bitmap_cache import TigerCache

logger = logging.getLogger(__name__)


class DirectoryGrantExpander:
    """Async worker for expanding large directory grants (Leopard-style).

    When a permission is granted on a directory with more than EXPANSION_LIMIT files,
    the grant is recorded as "pending" and this worker processes it in background.

    This enables:
    - Non-blocking grant operations (user doesn't wait for 100k files)
    - Batched processing (memory efficient)
    - Progress tracking (UI can show expansion status)
    - Failure recovery (can resume from last position)

    Usage:
        # Start worker in background thread/process
        expander = DirectoryGrantExpander(engine, tiger_cache, metadata_store)
        asyncio.create_task(expander.run_worker())

        # Or run single expansion cycle
        expanded = expander.process_pending_grants()
    """

    # Number of files to expand per batch
    BATCH_SIZE = 1000

    # How often to check for pending grants (seconds)
    POLL_INTERVAL = 5.0

    def __init__(
        self,
        engine: Engine,
        tiger_cache: TigerCache,
        metadata_store: Any = None,
    ):
        """Initialize the expander.

        Args:
            engine: SQLAlchemy database engine
            tiger_cache: Tiger Cache instance
            metadata_store: Metadata store for listing files (optional)
        """
        self._engine = engine
        self._tiger_cache = tiger_cache
        self._metadata_store = metadata_store
        self._is_postgresql = "postgresql" in str(engine.url)
        self._running = False
        self._stop_event: asyncio.Event | None = None

    def set_metadata_store(self, store: Any) -> None:
        """Set the metadata store for file listing."""
        self._metadata_store = store

    def get_pending_grants(self, limit: int = 10) -> list[dict]:
        """Get pending directory grants to expand.

        Args:
            limit: Maximum number of grants to return

        Returns:
            List of grant dictionaries
        """

        query = text("""
            SELECT grant_id, subject_type, subject_id, permission,
                   directory_path, zone_id, grant_revision,
                   include_future_files, expanded_count, total_count
            FROM tiger_directory_grants
            WHERE expansion_status = 'pending'
            ORDER BY created_at ASC
            LIMIT :limit
        """)

        try:
            with self._engine.connect() as conn:
                result = conn.execute(query, {"limit": limit})
                grants = []
                for row in result:
                    grants.append(
                        {
                            "grant_id": row.grant_id,
                            "subject_type": row.subject_type,
                            "subject_id": row.subject_id,
                            "permission": row.permission,
                            "directory_path": row.directory_path,
                            "zone_id": row.zone_id,
                            "grant_revision": row.grant_revision,
                            "include_future_files": row.include_future_files,
                            "expanded_count": row.expanded_count or 0,
                            "total_count": row.total_count,
                        }
                    )
                return grants
        except Exception as e:
            logger.error(f"[LEOPARD-WORKER] Failed to get pending grants: {e}")
            return []

    def _mark_in_progress(self, grant_id: int, total_count: int) -> bool:
        """Mark a grant as in_progress and set total count.

        Args:
            grant_id: Grant ID
            total_count: Total number of files to expand

        Returns:
            True if updated successfully
        """

        query = text("""
            UPDATE tiger_directory_grants
            SET expansion_status = 'in_progress',
                total_count = :total_count,
                updated_at = CURRENT_TIMESTAMP
            WHERE grant_id = :grant_id
              AND expansion_status = 'pending'
        """)

        try:
            with self._engine.begin() as conn:
                result = conn.execute(
                    query,
                    {
                        "grant_id": grant_id,
                        "total_count": total_count,
                    },
                )
                return result.rowcount > 0
        except Exception as e:
            logger.error(f"[LEOPARD-WORKER] Failed to mark in_progress: {e}")
            return False

    def _update_progress(self, grant_id: int, expanded_count: int) -> bool:
        """Update expansion progress.

        Args:
            grant_id: Grant ID
            expanded_count: Number of files expanded so far

        Returns:
            True if updated successfully
        """

        query = text("""
            UPDATE tiger_directory_grants
            SET expanded_count = :expanded_count,
                updated_at = CURRENT_TIMESTAMP
            WHERE grant_id = :grant_id
        """)

        try:
            with self._engine.begin() as conn:
                result = conn.execute(
                    query,
                    {
                        "grant_id": grant_id,
                        "expanded_count": expanded_count,
                    },
                )
                return result.rowcount > 0
        except Exception as e:
            logger.error(f"[LEOPARD-WORKER] Failed to update progress: {e}")
            return False

    def _mark_completed(self, grant_id: int, expanded_count: int) -> bool:
        """Mark a grant as completed.

        Args:
            grant_id: Grant ID
            expanded_count: Final number of files expanded

        Returns:
            True if updated successfully
        """

        query = text("""
            UPDATE tiger_directory_grants
            SET expansion_status = 'completed',
                expanded_count = :expanded_count,
                completed_at = CURRENT_TIMESTAMP,
                updated_at = CURRENT_TIMESTAMP
            WHERE grant_id = :grant_id
        """)

        try:
            with self._engine.begin() as conn:
                result = conn.execute(
                    query,
                    {
                        "grant_id": grant_id,
                        "expanded_count": expanded_count,
                    },
                )
                return result.rowcount > 0
        except Exception as e:
            logger.error(f"[LEOPARD-WORKER] Failed to mark completed: {e}")
            return False

    def _mark_failed(self, grant_id: int, error_message: str) -> bool:
        """Mark a grant as failed.

        Args:
            grant_id: Grant ID
            error_message: Error description

        Returns:
            True if updated successfully
        """

        query = text("""
            UPDATE tiger_directory_grants
            SET expansion_status = 'failed',
                error_message = :error_message,
                updated_at = CURRENT_TIMESTAMP
            WHERE grant_id = :grant_id
        """)

        try:
            with self._engine.begin() as conn:
                result = conn.execute(
                    query,
                    {
                        "grant_id": grant_id,
                        "error_message": error_message[:1000],  # Truncate long errors
                    },
                )
                return result.rowcount > 0
        except Exception as e:
            logger.error(f"[LEOPARD-WORKER] Failed to mark failed: {e}")
            return False

    def _get_directory_descendants(
        self,
        directory_path: str,
        zone_id: str,
    ) -> list[str]:
        """Get all files under a directory.

        Args:
            directory_path: Directory path (ending with /)
            zone_id: Zone ID

        Returns:
            List of file paths
        """
        if not self._metadata_store:
            logger.warning("[LEOPARD-WORKER] No metadata store, cannot list files")
            return []

        try:
            files = self._metadata_store.list(
                prefix=directory_path,
                recursive=True,
                zone_id=zone_id,
            )
            return [f.path for f in files if f.path]
        except Exception as e:
            logger.error(f"[LEOPARD-WORKER] Failed to list directory: {e}")
            return []

    def expand_grant(self, grant: dict) -> tuple[int, bool]:
        """Expand a single directory grant with batching.

        Args:
            grant: Grant dictionary from get_pending_grants()

        Returns:
            Tuple of (files_expanded, completed_successfully)
        """
        grant_id = grant["grant_id"]
        directory_path = grant["directory_path"]
        zone_id = grant["zone_id"]
        subject_type = grant["subject_type"]
        subject_id = grant["subject_id"]
        permission = grant["permission"]
        grant_revision = grant["grant_revision"]
        already_expanded = grant.get("expanded_count", 0)

        logger.info(
            f"[LEOPARD-WORKER] Starting expansion for grant {grant_id}: "
            f"{directory_path} -> {subject_type}:{subject_id} ({permission})"
        )

        try:
            # Get all descendants
            descendants = self._get_directory_descendants(directory_path, zone_id)

            if not descendants:
                # No files - mark as completed
                self._mark_completed(grant_id, 0)
                logger.info(f"[LEOPARD-WORKER] Grant {grant_id}: no files to expand")
                return 0, True

            total_count = len(descendants)

            # Mark as in_progress with total count
            if not self._mark_in_progress(grant_id, total_count):
                # Someone else is processing this grant
                logger.info(f"[LEOPARD-WORKER] Grant {grant_id} already being processed")
                return 0, False

            # Skip already expanded files (for resume)
            if already_expanded > 0:
                descendants = descendants[already_expanded:]
                logger.info(
                    f"[LEOPARD-WORKER] Grant {grant_id}: resuming from {already_expanded}/{total_count}"
                )

            # Process in batches
            expanded_count = already_expanded
            for i in range(0, len(descendants), self.BATCH_SIZE):
                batch = descendants[i : i + self.BATCH_SIZE]

                # Expand batch
                batch_expanded, _ = self._tiger_cache.expand_directory_grant(
                    subject_type=subject_type,
                    subject_id=subject_id,
                    permission=permission,
                    directory_path=directory_path,
                    zone_id=zone_id,
                    grant_revision=grant_revision,
                    descendants=batch,
                )

                expanded_count += batch_expanded

                # Update progress
                self._update_progress(grant_id, expanded_count)

                logger.debug(
                    f"[LEOPARD-WORKER] Grant {grant_id}: {expanded_count}/{total_count} "
                    f"({100 * expanded_count / total_count:.1f}%)"
                )

            # Mark as completed
            self._mark_completed(grant_id, expanded_count)
            logger.info(
                f"[LEOPARD-WORKER] Grant {grant_id} completed: {expanded_count} files expanded"
            )
            return expanded_count, True

        except Exception as e:
            error_msg = f"Expansion failed: {e}"
            logger.error(f"[LEOPARD-WORKER] Grant {grant_id}: {error_msg}")
            self._mark_failed(grant_id, error_msg)
            return 0, False

    def process_pending_grants(self, limit: int = 10) -> int:
        """Process a batch of pending grants.

        Args:
            limit: Maximum number of grants to process

        Returns:
            Total number of files expanded
        """
        grants = self.get_pending_grants(limit=limit)

        if not grants:
            return 0

        logger.info(f"[LEOPARD-WORKER] Processing {len(grants)} pending grants")

        total_expanded = 0
        for grant in grants:
            expanded, _ = self.expand_grant(grant)
            total_expanded += expanded

        return total_expanded

    async def run_worker(self) -> None:
        """Run the expansion worker continuously.

        This should be started as a background task:
            asyncio.create_task(expander.run_worker())

        Stop with:
            expander.stop()
        """
        import asyncio

        self._running = True
        self._stop_event = asyncio.Event()

        logger.info("[LEOPARD-WORKER] Starting directory grant expansion worker")

        while self._running:
            try:
                # Process pending grants
                expanded = await asyncio.to_thread(self.process_pending_grants)

                if expanded > 0:
                    logger.info(f"[LEOPARD-WORKER] Expanded {expanded} files this cycle")

                # Wait before next poll (or until stopped)
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(),
                        timeout=self.POLL_INTERVAL,
                    )
                    # If we get here, stop was requested
                    break
                except TimeoutError:
                    # Normal timeout - continue polling
                    pass

            except Exception as e:
                logger.error(f"[LEOPARD-WORKER] Worker error: {e}")
                # Wait before retrying on error
                await asyncio.sleep(self.POLL_INTERVAL * 2)

        logger.info("[LEOPARD-WORKER] Worker stopped")

    def stop(self) -> None:
        """Stop the worker gracefully."""
        self._running = False
        if self._stop_event:
            self._stop_event.set()
