"""Sync pipeline service extracted from CacheConnectorMixin.

Encapsulates the 7-step content sync process for connector backends.
This was extracted from the 2,295-line CacheConnectorMixin god class to
improve separation of concerns, testability, and readability.

The sync pipeline orchestrates efficient content syncing from external
storage backends (GCS, S3, Gmail, etc.) into a two-level cache:
- L1: In-memory LRU cache (fast, per-process, volatile)
- L2: PostgreSQL content_cache table (persistent, shared, durable)

Steps:
    1. Discover files: List and filter files from backend
    2. Load cache: Bulk load existing cache entries (L1 + L2)
    3. Check versions: Determine which files need syncing
    4. Read backend: Batch read content from backend
    5. Process content: Parse and prepare cache entries
    6. Write cache: Batch write to L1 + L2 in single transaction
    7. Generate embeddings: Optional semantic search indexing

Usage:
    pipeline = SyncPipelineService(connector)
    result = pipeline.execute(mount_point="/mnt/gcs")

Part of: #1461 (backend contract gaps)
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from nexus.core.hash_fast import hash_content
from nexus.core.permissions import OperationContext

if TYPE_CHECKING:
    from nexus.backends.cache_mixin import CacheEntry, SyncResult

logger = logging.getLogger(__name__)

# Backend version constant for immutable content (e.g., Gmail emails that never change)
IMMUTABLE_VERSION = "immutable"


class SyncPipelineService:
    """7-step content sync pipeline for connector backends.

    Takes a reference to a connector (CacheConnectorMixin subclass) and
    orchestrates efficient content syncing to the cache layer.

    The connector must provide:
        - list_dir(path, context) -> list[str]
        - _read_bulk_from_cache(paths, original=False) -> dict[str, CacheEntry]
        - _batch_read_from_backend(paths, contexts) -> dict[str, bytes]
        - _parse_content(path, content) -> tuple[str|None, str|None, dict|None]
        - _batch_write_to_cache(entries) -> None
        - _generate_embeddings(path) -> None
        - MAX_CACHE_FILE_SIZE: int

    Optional (for version checking):
        - get_version(path, context) -> str | None
        - _batch_get_versions(paths, contexts) -> dict[str, str|None]
    """

    def __init__(self, connector: Any) -> None:
        self._connector = connector

    def execute(
        self,
        path: str | None = None,
        mount_point: str | None = None,
        include_patterns: list[str] | None = None,
        exclude_patterns: list[str] | None = None,
        max_file_size: int | None = None,
        generate_embeddings: bool = True,
        context: OperationContext | None = None,
    ) -> SyncResult:
        """Execute the 7-step sync pipeline.

        Args:
            path: Specific path to sync (relative to mount), or None for entire mount
            mount_point: Virtual mount point (e.g., "/mnt/gcs")
            include_patterns: Glob patterns to include (e.g., ["*.py", "*.md"])
            exclude_patterns: Glob patterns to exclude (e.g., ["*.pyc", ".git/*"])
            max_file_size: Maximum file size to cache
            generate_embeddings: Generate embeddings for semantic search
            context: Operation context with zone_id, user, etc.

        Returns:
            SyncResult with statistics
        """
        from nexus.backends.cache_mixin import SyncResult

        result = SyncResult()
        max_size = max_file_size or self._connector.MAX_CACHE_FILE_SIZE

        # STEP 1: Discover and filter files from backend
        logger.info("[CACHE-SYNC] Step 1: Discovering files from backend...")
        files, backend_to_virtual = self._step1_discover_files(
            path, mount_point, include_patterns, exclude_patterns, context, result
        )
        if not files:
            return result

        result.files_scanned = len(files)
        virtual_paths = list(backend_to_virtual.values())

        # STEP 2: Bulk load existing cache entries (L1 + L2)
        logger.info(f"[CACHE-SYNC] Step 2: Bulk loading cache for {len(virtual_paths)} paths...")
        cached_entries = self._step2_load_cache(virtual_paths)

        # STEP 3: Determine which files need backend reads
        logger.info("[CACHE-SYNC] Step 3: Checking versions and filtering cached files...")
        files_needing_backend, file_contexts, file_metadata = self._step3_check_versions(
            files, backend_to_virtual, cached_entries, context, result
        )

        # STEP 4: Batch read content from backend
        logger.info(
            f"[CACHE-SYNC] Step 4: Batch reading {len(files_needing_backend)} files from backend..."
        )
        backend_contents = self._step4_read_backend(files_needing_backend, file_contexts)

        # STEP 5: Process content and prepare cache entries
        logger.info("[CACHE-SYNC] Step 5: Processing and preparing batch cache write...")
        cache_entries_to_write, files_to_embed = self._step5_process_content(
            backend_contents, file_metadata, max_size, generate_embeddings, context, result
        )

        # STEP 6: Batch write to cache (single transaction)
        if cache_entries_to_write:
            logger.info(
                f"[CACHE-SYNC] Step 6: Batch writing {len(cache_entries_to_write)} entries..."
            )
            self._step6_write_cache(cache_entries_to_write, result)

        # STEP 7: Generate embeddings (optional)
        if files_to_embed:
            logger.info(
                f"[CACHE-SYNC] Step 7: Generating embeddings for {len(files_to_embed)} files..."
            )
            self._step7_generate_embeddings(files_to_embed, result)

        logger.info(
            f"[CACHE-SYNC] Complete: synced={result.files_synced}, "
            f"skipped={result.files_skipped}, errors={len(result.errors)}"
        )

        # Notify Zoekt to reindex if files were synced
        if result.files_synced > 0:
            from nexus.search.zoekt_client import notify_zoekt_sync_complete

            notify_zoekt_sync_complete(result.files_synced)

        return result

    # =========================================================================
    # Step implementations
    # =========================================================================

    def _step1_discover_files(
        self,
        path: str | None,
        mount_point: str | None,
        include_patterns: list[str] | None,
        exclude_patterns: list[str] | None,
        context: OperationContext | None,
        result: SyncResult,
    ) -> tuple[list[str], dict[str, str]]:
        """Step 1: Discover and filter files from backend."""
        from nexus.core import glob_fast

        connector = self._connector

        # List files from backend
        try:
            if path:
                backend_path = path.lstrip("/")
                try:
                    entries = (
                        connector.list_dir(backend_path, context)
                        if hasattr(connector, "list_dir")
                        else []
                    )
                    if entries:
                        files = self._list_files_recursive(backend_path, context)
                    else:
                        import os.path as osp

                        files = [backend_path] if osp.splitext(backend_path)[1] else []
                except Exception:
                    files = [backend_path]
            elif hasattr(connector, "list_dir"):
                files = self._list_files_recursive("", context)
            else:
                result.errors.append("Connector does not support list_dir")
                return [], {}
        except Exception as e:
            result.errors.append(f"Failed to list files: {e}")
            return [], {}

        # Build virtual paths mapping
        backend_to_virtual: dict[str, str] = {}
        for backend_path in files:
            if mount_point:
                virtual_path = f"{mount_point.rstrip('/')}/{backend_path.lstrip('/')}"
            else:
                virtual_path = f"/{backend_path.lstrip('/')}"
            backend_to_virtual[backend_path] = virtual_path

        # Apply include/exclude patterns using Rust-accelerated glob matching
        if include_patterns or exclude_patterns:
            all_virtual_paths = list(backend_to_virtual.values())
            filtered_paths = set(
                glob_fast.glob_filter(
                    all_virtual_paths,
                    include_patterns=list(include_patterns) if include_patterns else None,
                    exclude_patterns=list(exclude_patterns) if exclude_patterns else None,
                )
            )
            original_count = len(backend_to_virtual)
            backend_to_virtual = {
                bp: vp for bp, vp in backend_to_virtual.items() if vp in filtered_paths
            }
            result.files_skipped += original_count - len(backend_to_virtual)

        logger.info(
            f"[CACHE-SYNC] Step 1 complete: {len(backend_to_virtual)} files to process "
            f"(filtered {result.files_skipped} by patterns)"
        )
        return list(backend_to_virtual.keys()), backend_to_virtual

    def _step2_load_cache(self, virtual_paths: list[str]) -> dict[str, CacheEntry]:
        """Step 2: Bulk load existing cache entries (L1 + L2)."""
        cached_entries: dict[str, CacheEntry] = self._connector._read_bulk_from_cache(
            virtual_paths, original=True
        )
        logger.info(
            f"[CACHE-SYNC] Step 2 complete: {len(cached_entries)} entries found in cache "
            f"({len(virtual_paths) - len(cached_entries)} not cached)"
        )
        return cached_entries

    def _step3_check_versions(
        self,
        files: list[str],
        backend_to_virtual: dict[str, str],
        cached_entries: dict[str, CacheEntry],
        context: OperationContext | None,
        result: SyncResult,
    ) -> tuple[list[str], dict[str, OperationContext], dict[str, dict]]:
        """Step 3: Check versions and determine which files need syncing."""
        connector = self._connector
        files_needing_backend: list[str] = []
        file_contexts: dict[str, OperationContext] = {}
        file_metadata: dict[str, dict] = {}

        # Prepare contexts for all files
        all_contexts: dict[str, OperationContext] = {}
        for backend_path in files:
            vpath = backend_to_virtual.get(backend_path)
            if vpath is None:
                continue
            read_context = self._create_read_context(
                backend_path=backend_path,
                virtual_path=vpath,
                context=context,
            )
            all_contexts[backend_path] = read_context

        # BATCH VERSION CHECK: Get all versions in one call (10-25x faster)
        versions: dict[str, str | None] = {}
        paths_needing_version_check = []

        for backend_path in files:
            vpath = backend_to_virtual.get(backend_path)
            if vpath is None:
                continue
            cached = cached_entries.get(vpath)

            # Skip immutable cached files (Gmail emails never change)
            if cached and not cached.stale and cached.backend_version == IMMUTABLE_VERSION:
                logger.debug(f"[CACHE] SYNC SKIP (immutable): {vpath}")
                result.files_skipped += 1
                continue

            # Collect paths needing version check
            if hasattr(connector, "get_version") or hasattr(connector, "_batch_get_versions"):
                paths_needing_version_check.append(backend_path)

        # Batch fetch versions if supported
        if paths_needing_version_check and hasattr(connector, "_batch_get_versions"):
            logger.info(
                f"[CACHE-SYNC] Batch fetching versions for "
                f"{len(paths_needing_version_check)} files..."
            )
            try:
                versions = connector._batch_get_versions(paths_needing_version_check, all_contexts)
                logger.info(f"[CACHE-SYNC] Batch version fetch complete: {len(versions)} versions")
            except Exception as e:
                logger.warning(f"[CACHE-SYNC] Batch version fetch failed: {e}")
                versions = {}

        # Filter files based on version checks
        for backend_path in files:
            try:
                vpath = backend_to_virtual.get(backend_path)
                if vpath is None:
                    continue

                if backend_path not in all_contexts:
                    continue
                read_context = all_contexts[backend_path]

                cached = cached_entries.get(vpath)

                # Skip immutable files (already counted above)
                if cached and not cached.stale and cached.backend_version == IMMUTABLE_VERSION:
                    continue

                version = versions.get(backend_path)

                # Skip if cache is fresh and version matches
                if (
                    cached
                    and not cached.stale
                    and hasattr(connector, "get_version")
                    and cached.backend_version
                    and cached.backend_version == version
                ):
                    logger.debug(f"[CACHE] SYNC SKIP (version match): {vpath}")
                    result.files_skipped += 1
                    continue

                # This file needs backend read
                files_needing_backend.append(backend_path)
                file_contexts[backend_path] = read_context
                file_metadata[backend_path] = {
                    "virtual_path": vpath,
                    "cached": cached,
                    "version": version,
                }

            except Exception as e:
                result.errors.append(f"Failed to prepare sync for {backend_path}: {e}")

        logger.info(
            f"[CACHE-SYNC] Step 3 complete: {len(files_needing_backend)} files need backend reads "
            f"({len(files) - len(files_needing_backend)} skipped as fresh)"
        )
        return files_needing_backend, file_contexts, file_metadata

    def _step4_read_backend(
        self,
        files: list[str],
        contexts: dict[str, OperationContext],
    ) -> dict[str, bytes]:
        """Step 4: Batch read content from backend."""
        backend_contents: dict[str, bytes] = self._connector._batch_read_from_backend(
            files, contexts
        )
        logger.info(
            f"[CACHE-SYNC] Step 4 complete: {len(backend_contents)}/{len(files)} files read "
            f"({len(files) - len(backend_contents)} failed)"
        )
        return backend_contents

    def _step5_process_content(
        self,
        backend_contents: dict[str, bytes],
        file_metadata: dict[str, dict],
        max_size: int,
        generate_embeddings: bool,
        context: OperationContext | None,
        result: SyncResult,
    ) -> tuple[list[dict], list[str]]:
        """Step 5: Process content and prepare cache entries."""
        zone_id = getattr(context, "zone_id", None) if context else None
        cache_entries_to_write: list[dict] = []
        files_to_embed: list[str] = []

        for backend_path, content in backend_contents.items():
            try:
                metadata = file_metadata[backend_path]
                vpath = metadata["virtual_path"]
                cached = metadata["cached"]
                version = metadata["version"]

                # Skip if cache is fresh and content matches (hash check)
                if cached and not cached.stale and version is None and cached.content_binary:
                    content_hash = hash_content(content)
                    cached_hash = hash_content(cached.content_binary)
                    if content_hash == cached_hash:
                        logger.debug(f"[CACHE] SYNC SKIP (hash match): {vpath}")
                        result.files_skipped += 1
                        continue

                # Skip if too large
                if len(content) > max_size:
                    logger.debug(f"[CACHE] SYNC SKIP (too large): {vpath} ({len(content)} bytes)")
                    result.files_skipped += 1
                    continue

                # Parse content if supported (PDF, Excel, etc.)
                parsed_text, parsed_from, parse_metadata = self._connector._parse_content(
                    vpath, content
                )

                # Prepare cache entry for batch write
                cache_entries_to_write.append(
                    {
                        "path": vpath,
                        "content": content,
                        "content_text": parsed_text,
                        "content_type": "parsed" if parsed_text else "full",
                        "backend_version": version,
                        "parsed_from": parsed_from,
                        "parse_metadata": parse_metadata,
                        "zone_id": zone_id,
                    }
                )

                result.files_synced += 1
                result.bytes_synced += len(content)

                # Track for embedding generation
                if generate_embeddings:
                    files_to_embed.append(vpath)

            except Exception as e:
                result.errors.append(f"Failed to process {backend_path}: {e}")

        logger.info(
            f"[CACHE-SYNC] Step 5 complete: {len(cache_entries_to_write)} entries prepared "
            f"({result.bytes_synced} bytes total)"
        )
        return cache_entries_to_write, files_to_embed

    def _step6_write_cache(
        self,
        cache_entries: list[dict],
        result: SyncResult,
    ) -> None:
        """Step 6: Batch write to cache (single transaction)."""
        try:
            self._connector._batch_write_to_cache(cache_entries)
            logger.info(f"[CACHE-SYNC] Step 6 complete: {len(cache_entries)} entries written")
        except Exception as e:
            result.errors.append(f"Failed to batch write cache entries: {e}")
            logger.error(f"[CACHE-SYNC] Step 6 failed: {e}")

    def _step7_generate_embeddings(
        self,
        files: list[str],
        result: SyncResult,
    ) -> None:
        """Step 7: Generate embeddings for semantic search (optional)."""
        for vpath in files:
            try:
                self._connector._generate_embeddings(vpath)
                result.embeddings_generated += 1
            except Exception as e:
                result.errors.append(f"Failed to generate embeddings for {vpath}: {e}")

        logger.info(
            f"[CACHE-SYNC] Step 7 complete: {result.embeddings_generated} embeddings generated"
        )

    # =========================================================================
    # Helpers
    # =========================================================================

    def _create_read_context(
        self,
        backend_path: str,
        virtual_path: str,
        context: OperationContext | None = None,
    ) -> OperationContext:
        """Create a context for reading content with proper backend_path set."""
        if context:
            new_context = OperationContext(
                user=context.user,
                groups=context.groups,
                backend_path=backend_path,
                zone_id=getattr(context, "zone_id", None),
                is_system=True,
            )
        else:
            new_context = OperationContext(
                user="system",
                groups=[],
                backend_path=backend_path,
                is_system=True,
            )
        new_context.virtual_path = virtual_path
        return new_context

    def _list_files_recursive(
        self,
        path: str,
        context: OperationContext | None = None,
    ) -> list[str]:
        """Recursively list all files under a path."""
        connector = self._connector
        files: list[str] = []

        if not hasattr(connector, "list_dir"):
            return files

        try:
            entries = connector.list_dir(path, context)
            for entry in entries:
                entry_name = entry.rstrip("/")

                if path == "" or path == "/":
                    full_path = entry_name
                else:
                    full_path = f"{path.rstrip('/')}/{entry_name}"

                if entry.endswith("/"):
                    files.extend(self._list_files_recursive(full_path, context))
                else:
                    files.append(full_path)
        except Exception:
            pass

        return files
