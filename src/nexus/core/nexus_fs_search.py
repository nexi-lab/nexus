"""Search operations for NexusFS.

This module contains file search and listing operations:
- list: List files in a directory
- glob: Find files matching glob patterns
- grep: Search file contents using regex (with optional Zoekt acceleration)
- semantic_search: Search files using semantic similarity

Issue #929: Adaptive algorithm selection for search operations.
Implements runtime strategy selection similar to ClickHouse's approach,
choosing optimal algorithms based on data characteristics.
"""

from __future__ import annotations

import asyncio
import builtins
import fnmatch
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from enum import StrEnum
from typing import TYPE_CHECKING, Any, cast

from nexus.core import glob_fast, grep_fast
from nexus.core.exceptions import PermissionDeniedError
from nexus.core.permissions import Permission
from nexus.core.rpc_decorator import rpc_expose

# =============================================================================
# Issue #929: Adaptive Algorithm Selection Configuration
# =============================================================================

# Grep strategy thresholds
GREP_SEQUENTIAL_THRESHOLD = 10  # Below this file count, use sequential (no overhead)
GREP_PARALLEL_THRESHOLD = 100  # Above this, consider parallel processing
GREP_ZOEKT_THRESHOLD = 1000  # Above this, prefer Zoekt if available
GREP_PARALLEL_WORKERS = 4  # Thread pool size for parallel grep
GREP_CACHED_TEXT_RATIO = 0.8  # Use cached text path if > 80% files have cached text

# Glob strategy thresholds
GLOB_RUST_THRESHOLD = 50  # Use Rust acceleration above this file count


class SearchStrategy(StrEnum):
    """Strategy for grep operations (Issue #929).

    Selected at runtime based on file count, cached text ratio, and available backends.
    Inspired by ClickHouse's adaptive algorithm selection.
    """

    SEQUENTIAL = "sequential"  # < 10 files, any pattern - no parallelization overhead
    CACHED_TEXT = "cached_text"  # > 80% files have pre-parsed text in cache
    RUST_BULK = "rust_bulk"  # 10-1000 files with Rust available
    PARALLEL_POOL = "parallel_pool"  # 100-10000 files, CPU-bound parallel processing
    ZOEKT_INDEX = "zoekt_index"  # > 1000 files with Zoekt index available


class GlobStrategy(StrEnum):
    """Strategy for glob operations (Issue #929).

    Selected at runtime based on pattern complexity and file count.
    """

    FNMATCH_SIMPLE = "fnmatch_simple"  # Simple patterns without **
    REGEX_COMPILED = "regex_compiled"  # Complex patterns with **
    RUST_BULK = "rust_bulk"  # > 50 files with Rust available
    DIRECTORY_PRUNED = "directory_pruned"  # Pattern has static prefix for pruning


logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from nexus.core.metadata import PaginatedResult
    from nexus.core.permissions import OperationContext
    from nexus.search.async_search import AsyncSemanticSearch
    from nexus.search.semantic import SemanticSearch
    from nexus.storage.metadata_store import SQLAlchemyMetadataStore


class NexusFSSearchMixin:
    """Mixin providing search operations for NexusFS."""

    # Type hints for attributes that will be provided by NexusFS parent class
    if TYPE_CHECKING:
        from nexus.core.mount_router import MountRouter
        from nexus.core.permissions import PermissionEnforcer
        from nexus.core.rebac_manager_enhanced import EnhancedReBACManager

        metadata: SQLAlchemyMetadataStore
        router: MountRouter
        _enforce_permissions: bool
        _default_context: OperationContext
        _permission_enforcer: PermissionEnforcer
        _semantic_search: SemanticSearch | None
        _async_search: AsyncSemanticSearch | None
        _rebac_manager: EnhancedReBACManager

        def _validate_path(self, path: str) -> str: ...

        def _has_descendant_access(
            self, path: str, permission: Permission, context: OperationContext
        ) -> bool: ...
        def _get_backend_directory_entries(self, path: str) -> set[str]: ...
        def _get_routing_params(
            self, context: OperationContext | None
        ) -> tuple[str | None, str | None, bool]: ...
        def read(
            self, path: str, context: OperationContext | None = None, return_metadata: bool = False
        ) -> bytes | dict[str, Any]: ...
        def read_bulk(
            self,
            paths: builtins.list[str],
            context: OperationContext | None = None,
            return_metadata: bool = False,
            skip_errors: bool = True,
        ) -> dict[str, bytes | dict[str, Any] | None]: ...
        async def ls(
            self, path: str = "/", recursive: bool = False
        ) -> builtins.list[str] | builtins.list[dict[str, Any]]: ...

    def _get_cross_tenant_shared_paths(
        self,
        subject_type: str,
        subject_id: str,
        tenant_id: str,
        prefix: str = "",
    ) -> list[str]:
        """Fetch file paths shared with a user from other tenants.

        Issue #904: This method fetches cross-tenant shared file paths to include
        in list() results. Uses the idx_rebac_cross_tenant_shares index.

        Args:
            subject_type: Subject type (e.g., "user")
            subject_id: Subject ID (e.g., user ID)
            tenant_id: Current tenant ID (to exclude from results)
            prefix: Path prefix filter (optional)

        Returns:
            List of file paths shared with this subject from other tenants
        """
        from datetime import UTC, datetime

        from nexus.core.rebac import CROSS_TENANT_ALLOWED_RELATIONS

        try:
            # Use the rebac manager's connection to query cross-tenant shares
            with self._rebac_manager._connection() as conn:
                cursor = self._rebac_manager._create_cursor(conn)

                cross_tenant_relations = list(CROSS_TENANT_ALLOWED_RELATIONS)
                placeholders = ", ".join("?" * len(cross_tenant_relations))

                # Query for file objects shared with this subject from other tenants
                query = f"""
                    SELECT DISTINCT object_id
                    FROM rebac_tuples
                    WHERE relation IN ({placeholders})
                      AND subject_type = ? AND subject_id = ?
                      AND object_type = 'file'
                      AND tenant_id != ?
                      AND (expires_at IS NULL OR expires_at > ?)
                """

                # Add prefix filter if provided
                if prefix:
                    query += " AND object_id LIKE ?"
                    params = (
                        *cross_tenant_relations,
                        subject_type,
                        subject_id,
                        tenant_id,
                        datetime.now(UTC).isoformat(),
                        f"{prefix}%",
                    )
                else:
                    params = (
                        *cross_tenant_relations,
                        subject_type,
                        subject_id,
                        tenant_id,
                        datetime.now(UTC).isoformat(),
                    )

                cursor.execute(
                    self._rebac_manager._fix_sql_placeholders(query),
                    params,
                )

                paths = []
                for row in cursor.fetchall():
                    path = row["object_id"] if isinstance(row, dict) else row[0]
                    paths.append(path)

                if paths:
                    logger.debug(
                        f"[CROSS-TENANT] Found {len(paths)} shared paths for {subject_type}:{subject_id}"
                    )

                return paths

        except Exception as e:
            logger.warning(f"Failed to fetch cross-tenant shared paths: {e}")
            return []

    @rpc_expose(description="List files in directory")
    def list(
        self,
        path: str = "/",
        recursive: bool = True,
        details: bool = False,
        prefix: str | None = None,
        show_parsed: bool = True,  # noqa: ARG002
        context: OperationContext | None = None,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> builtins.list[str] | builtins.list[dict[str, Any]] | PaginatedResult:
        """
        List files in a directory.

        Supports memory virtual paths since v0.4.0.
        Supports cursor-based pagination since Issue #937.

        Args:
            path: Directory path to list (default: "/", supports memory paths)
            recursive: If True, list all files recursively; if False, list only direct children (default: True)
            details: If True, return detailed metadata; if False, return paths only (default: False)
            prefix: (Deprecated) Path prefix to filter by - for backward compatibility.
                    When used, lists all files recursively with this prefix.
            show_parsed: If True, include parsed virtual views in listing (default: True).
                        Note: Virtual views are added at the RPC layer, not in this method.
            context: Optional operation context for permission filtering (uses default if not provided)
            limit: Max items per page (Issue #937). When provided, enables pagination mode
                   and returns PaginatedResult instead of list. Range: 1-10000.
            cursor: Continuation token from previous page's next_cursor (Issue #937).

        Returns:
            - If limit is None: List of file paths (details=False) or list of metadata dicts (details=True)
            - If limit is provided: PaginatedResult with items, next_cursor, and has_more
            Each metadata dict contains: path, size, modified_at, etag
            Results are filtered by read permission.

        Examples:
            # List all files recursively (default)
            fs.list()  # Returns: ["/file1.txt", "/dir/file2.txt", "/dir/subdir/file3.txt"]

            # List files in root directory only (non-recursive)
            fs.list("/", recursive=False)  # Returns: ["/file1.txt"]

            # List files recursively with details
            fs.list(details=True)  # Returns: [{"path": "/file1.txt", "size": 100, ...}, ...]

            # Old API (deprecated but supported)
            fs.list(prefix="/dir")  # Returns all files under /dir recursively

            # List memories (v0.4.0)
            fs.list("/memory/by-user/alice")  # Returns memory paths for user alice
            fs.list("/workspace/alice/agent1/memory")  # Returns memories for agent1

            # Paginated listing (Issue #937)
            result = fs.list("/workspace/", limit=1000)  # Returns PaginatedResult
            while result.has_more:
                process(result.items)
                result = fs.list("/workspace/", limit=1000, cursor=result.next_cursor)
        """
        # Issue #937: Pagination mode - use dedicated paginated implementation
        if limit is not None:
            return self._list_paginated(
                path=path,
                recursive=recursive,
                details=details,
                limit=limit,
                cursor=cursor,
                context=context,
            )
        # Phase 2 Integration (v0.4.0): Intercept memory paths
        from nexus.core.memory_router import MemoryViewRouter

        if path and MemoryViewRouter.is_memory_path(path):
            return self._list_memory_path(path, details)

        # Check if path routes to a dynamic API-backed connector (e.g., x_connector)
        # These connectors have virtual directories that don't exist in metadata
        if path and path != "/":
            try:
                tenant_id, agent_id, is_admin = self._get_routing_params(context)
                route = self.router.route(
                    path,
                    tenant_id=tenant_id,
                    agent_id=agent_id,
                    is_admin=is_admin,
                    check_write=False,
                )
                # Check if backend is a dynamic API-backed connector or virtual filesystem
                # We check for user_scoped=True explicitly (not just truthy) to avoid Mock objects
                # Also check has_virtual_filesystem for connectors like HN that have virtual directories
                is_dynamic_connector = (
                    getattr(route.backend, "user_scoped", None) is True
                    and getattr(route.backend, "token_manager", None) is not None
                ) or getattr(route.backend, "has_virtual_filesystem", None) is True

                if is_dynamic_connector:
                    # Check permission on the mount path BEFORE listing
                    # This ensures user has access to the virtual filesystem mount
                    if self._enforce_permissions and context:
                        mount_path = route.mount_point.rstrip("/")
                        if not mount_path:
                            mount_path = path.rstrip("/")
                        # Admin users always have access
                        if context.is_admin:
                            has_permission = True
                        elif context.subject_id is None:
                            # No subject_id means we can't verify permissions
                            has_permission = False
                        else:
                            # Use TRAVERSE permission for directory listing (Unix-like behavior)
                            # TRAVERSE allows navigation, and results will be filtered by filter_list()
                            # Try direct TRAVERSE permission first
                            has_permission = self._permission_enforcer.check(
                                mount_path, Permission.TRAVERSE, context
                            )
                            # If TRAVERSE fails, check if user has READ on any descendant
                            # This enables Unix-like behavior: users can traverse parent dirs
                            # if they have READ on any file inside
                            if not has_permission:
                                has_permission = self._has_descendant_access(
                                    mount_path, Permission.READ, context
                                )
                        if not has_permission:
                            raise PermissionDeniedError(
                                f"Access denied: User '{context.user}' does not have TRAVERSE permission for '{path}'"
                            )

                    # Use the backend's list_dir method directly
                    from dataclasses import replace

                    if context:
                        list_context = replace(context, backend_path=route.backend_path)
                    else:
                        from nexus.core.permissions import OperationContext

                        list_context = OperationContext(
                            user="anonymous", groups=[], backend_path=route.backend_path
                        )

                    # Helper function to recursively list directory contents
                    def list_recursive(current_path: str, backend_path: str) -> builtins.list[str]:
                        """Recursively list all files in directory tree."""
                        results: builtins.list[str] = []

                        # Get entries at current level
                        entries = route.backend.list_dir(backend_path, context=list_context)

                        for entry in entries:
                            full_path = f"{current_path.rstrip('/')}/{entry}"

                            if entry.endswith("/"):
                                # Directory - recurse into it if recursive=True
                                if recursive:
                                    # Add the directory itself (strip trailing slash for permission checks)
                                    results.append(full_path.rstrip("/"))
                                    # Recurse into subdirectory
                                    subdir_backend_path = (
                                        f"{backend_path.rstrip('/')}/{entry.rstrip('/')}"
                                        if backend_path
                                        else entry.rstrip("/")
                                    )
                                    results.extend(
                                        list_recursive(full_path.rstrip("/"), subdir_backend_path)
                                    )
                                else:
                                    # Non-recursive - add directory (strip trailing slash for permission checks)
                                    results.append(full_path.rstrip("/"))
                            else:
                                # File - add it
                                results.append(full_path)

                        return results

                    # List directory contents (with recursion if requested)
                    all_paths = list_recursive(path, route.backend_path)

                    # FIX #958: Apply permission filtering with proper directory handling
                    # For directories, check has_accessible_descendants() (TRAVERSE semantics)
                    # For files, check READ permission via filter_list()
                    # Without this, connector directories (Gmail labels, etc.) are incorrectly
                    # filtered out because users have READ on files inside, not on directories
                    if self._enforce_permissions and context:
                        from nexus.core.permissions import OperationContext

                        filter_ctx = (
                            context
                            if isinstance(context, OperationContext)
                            else self._default_context
                        )

                        # Separate directories from files
                        dir_paths = [p for p in all_paths if p.endswith("/")]
                        file_paths = [p for p in all_paths if not p.endswith("/")]

                        # Filter files by READ permission
                        filtered_files = self._permission_enforcer.filter_list(
                            file_paths, filter_ctx
                        )

                        # Filter directories by has_accessible_descendants
                        # This matches the behavior in regular FS listing (line 538)
                        filtered_dirs = [
                            d
                            for d in dir_paths
                            if self._permission_enforcer.has_accessible_descendants(
                                d.rstrip("/"), filter_ctx
                            )
                        ]

                        all_paths = filtered_dirs + filtered_files

                    # Format results
                    # FIX: Use correct field names for FUSE client compatibility:
                    # - "name" (not "path") - entry name without full path
                    # - "type" - "directory" or "file" (was missing)
                    # - "updated_at" (not "modified_at") - matches FUSE FileEntry struct
                    if details:
                        results_with_details = []
                        for entry_path in all_paths:
                            # Try to get metadata from file_paths table
                            file_meta = self.metadata.get(entry_path)
                            # Check if directory by looking at metadata mime_type
                            is_dir = (
                                file_meta
                                and hasattr(file_meta, "mime_type")
                                and file_meta.mime_type == "inode/directory"
                            )
                            # Extract just the name from the full path
                            name = entry_path.rstrip("/").split("/")[-1]

                            results_with_details.append(
                                {
                                    # New format fields (primary)
                                    "path": entry_path,
                                    "size": file_meta.size
                                    if file_meta and hasattr(file_meta, "size")
                                    else 0,
                                    "modified_at": file_meta.updated_at.isoformat()
                                    if file_meta
                                    and hasattr(file_meta, "updated_at")
                                    and file_meta.updated_at
                                    else None,
                                    "created_at": file_meta.created_at.isoformat()
                                    if file_meta
                                    and hasattr(file_meta, "created_at")
                                    and file_meta.created_at
                                    else None,
                                    "etag": file_meta.etag
                                    if file_meta and hasattr(file_meta, "etag")
                                    else None,
                                    "mime_type": file_meta.mime_type
                                    if file_meta and hasattr(file_meta, "mime_type")
                                    else None,
                                    "is_directory": is_dir,
                                    # Legacy fields for backward compatibility
                                    "name": name,
                                    "type": "directory" if is_dir else "file",
                                    "updated_at": file_meta.updated_at.isoformat()
                                    if file_meta
                                    and hasattr(file_meta, "updated_at")
                                    and file_meta.updated_at
                                    else None,
                                }
                            )
                        return results_with_details
                    return all_paths
            except PermissionDeniedError:
                # Re-raise permission errors - don't fall through to metadata listing
                raise
            except Exception as e:
                import traceback

                logger.debug(
                    f"Dynamic connector list_dir failed for {path}: {e}\n{traceback.format_exc()}"
                )
                # Fall through to normal metadata-based listing

        # Issue #904: Extract tenant_id for PREWHERE-style DB filtering
        # This filters files at the database level, reducing rows loaded by 30-90%
        # NOTE: Only apply tenant filtering when permissions are enforced,
        # because files are only stored with tenant_id when permissions are active.
        list_tenant_id: str | None = None
        subject_type: str | None = None
        subject_id: str | None = None
        if self._enforce_permissions and context:
            if hasattr(context, "tenant_id"):
                list_tenant_id = context.tenant_id
            # Extract subject info for cross-tenant share lookup
            if hasattr(context, "subject_type") and hasattr(context, "subject_id"):
                subject_type = context.subject_type
                subject_id = context.subject_id or context.user_id
            elif hasattr(context, "user_id"):
                subject_type = "user"
                subject_id = context.user_id

        # Handle backward compatibility with old 'prefix' parameter
        import time as _time

        _list_start = _time.time()
        _preapproved_dirs: set[str] = (
            set()
        )  # Dirs approved by has_accessible_descendants() in fast path

        if prefix is not None:
            # Old API: list(prefix="/path") - always recursive
            if prefix:
                prefix = self._validate_path(prefix)
            _meta_start = _time.time()
            all_files = self.metadata.list(prefix, tenant_id=list_tenant_id)
            logger.info(
                f"[LIST-TIMING] metadata.list(): {(_time.time() - _meta_start) * 1000:.1f}ms, {len(all_files)} files"
            )
            list_prefix = prefix or ""
        else:
            # New API: list(path="/", recursive=False)
            if path and path != "/":
                path = self._validate_path(path)

            # Ensure path ends with / for directory listing
            if path and not path.endswith("/"):
                path = path + "/"

            list_prefix = path if path != "/" else ""

            # OPTIMIZATION: For non-recursive listings, try sparse directory index + Tiger bitmap
            # This avoids loading 6000+ files by using:
            # 1. Sparse index for directory names (O(1))
            # 2. Tiger bitmap prefix check for accessible descendants (O(allowed_paths))
            _use_fast_path = False
            logger.info(
                f"[LIST-DEBUG] START path={path}, recursive={recursive}, tenant={list_tenant_id}, has_list_dir_entries={hasattr(self.metadata, 'list_directory_entries')}, has_context={context is not None}"
            )
            if not recursive and hasattr(self.metadata, "list_directory_entries") and context:
                _idx_start = _time.time()
                dir_entries = self.metadata.list_directory_entries(path, tenant_id=list_tenant_id)
                _idx_elapsed = (_time.time() - _idx_start) * 1000

                if dir_entries is not None:
                    logger.info(
                        f"[LIST-TIMING] list_directory_entries(): {_idx_elapsed:.1f}ms, {len(dir_entries)} entries (sparse index HIT)"
                    )

                    # Use Tiger bitmap to check which directories have accessible descendants
                    from nexus.core.metadata import FileMetadata

                    all_files = []
                    _perm_start = _time.time()

                    for entry in dir_entries:
                        entry_path = f"{path.rstrip('/')}/{entry['name']}"

                        if entry["type"] == "directory":
                            # Check if user has accessible descendants (uses Tiger bitmap)
                            if self._permission_enforcer.has_accessible_descendants(
                                entry_path, context
                            ):
                                _preapproved_dirs.add(entry_path)  # Track for allowed_set later
                                all_files.append(
                                    FileMetadata(
                                        path=entry_path,
                                        backend_name="",
                                        physical_path="",
                                        size=0,
                                        created_at=entry.get("created_at"),
                                        etag=None,
                                        mime_type="inode/directory",
                                    )
                                )
                        else:
                            # File entry - will be permission filtered normally
                            all_files.append(
                                FileMetadata(
                                    path=entry_path,
                                    backend_name="",
                                    physical_path="",
                                    size=0,
                                    created_at=entry.get("created_at"),
                                    etag=None,
                                    mime_type=None,
                                )
                            )

                    _perm_elapsed = (_time.time() - _perm_start) * 1000
                    logger.info(
                        f"[LIST-TIMING] has_accessible_descendants(): {_perm_elapsed:.1f}ms for {len(dir_entries)} entries"
                    )
                    logger.info(
                        "[LIST-TIMING] metadata.list(): SKIPPED (using sparse index + Tiger bitmap)"
                    )
                    logger.info(f"[LIST-DEBUG] preapproved_dirs: {list(_preapproved_dirs)[:5]}")
                    _use_fast_path = True
                else:
                    logger.info(
                        f"[LIST-TIMING] list_directory_entries(): {_idx_elapsed:.1f}ms (sparse index MISS - using fallback)"
                    )

            if not _use_fast_path:
                # Fallback: full recursive scan for permission filtering
                _meta_start = _time.time()
                all_files = self.metadata.list(list_prefix, tenant_id=list_tenant_id)
                logger.info(
                    f"[LIST-TIMING] metadata.list(): {(_time.time() - _meta_start) * 1000:.1f}ms, {len(all_files)} files"
                )
                # Debug: show sample paths from fallback
                sample_paths = [m.path for m in all_files[:5]]
                logger.info(f"[LIST-DEBUG] FALLBACK all_files sample: {sample_paths}")

        # Issue #904: Fetch cross-tenant shared files
        # If user has files shared from other tenants, include them in the listing
        if list_tenant_id and subject_type and subject_id:
            _ct_start = _time.time()
            cross_tenant_paths = self._get_cross_tenant_shared_paths(
                subject_type=subject_type,
                subject_id=subject_id,
                tenant_id=list_tenant_id,
                prefix=list_prefix,
            )
            logger.info(
                f"[LIST-TIMING] cross_tenant_lookup: {(_time.time() - _ct_start) * 1000:.1f}ms, {len(cross_tenant_paths) if cross_tenant_paths else 0} paths"
            )
            if cross_tenant_paths:
                # Fetch metadata for cross-tenant shared paths
                # Use get_batch if available, otherwise fetch individually
                existing_paths = {meta.path for meta in all_files}
                for ct_path in cross_tenant_paths:
                    if ct_path not in existing_paths:
                        try:
                            ct_meta = self.metadata.get(ct_path)
                            if ct_meta:
                                all_files.append(ct_meta)
                        except Exception:
                            # Path may have been deleted, skip it
                            pass

        # Apply recursive filter if needed
        if prefix is not None:
            results = all_files
        else:
            if recursive:
                # Include all files under this path
                results = all_files
            else:
                # Only include files directly in this directory (no subdirectories)
                results = []
                for meta in all_files:
                    # Remove the prefix to get relative path
                    rel_path = meta.path[len(path) :] if path != "/" else meta.path[1:]
                    # If there's no "/" in the relative path, it's in this directory
                    if "/" not in rel_path:
                        results.append(meta)
                logger.info(
                    f"[LIST-DEBUG] after non-recursive filter: {len(results)} results (from {len(all_files)} all_files)"
                )
                logger.info(f"[LIST-DEBUG] results sample: {[m.path for m in results[:5]]}")

        # =======================================================================
        # OPTIMIZATION (Issue #900): Single Permission Pass
        # Instead of multiple filter_list() calls, collect ALL candidate paths
        # upfront and make ONE permission check. Then reuse allowed_set everywhere.
        # =======================================================================
        allowed_set: set[str] = set()
        backend_dirs: set[str] = set()

        if self._enforce_permissions:
            import time

            perm_start = time.time()
            from nexus.core.permissions import OperationContext

            ctx_raw = context or self._default_context
            assert isinstance(ctx_raw, OperationContext), "Context must be OperationContext"
            ctx: OperationContext = ctx_raw

            # Step 1: Collect ALL candidate paths for single permission check
            candidate_paths: set[str] = set()

            # Add all metadata result paths
            candidate_paths.update(meta.path for meta in all_files)

            # For non-recursive, also get backend directories
            if not recursive:
                backend_dirs = self._get_backend_directory_entries(path)
                candidate_paths.update(backend_dirs)

            logger.debug(
                f"[PERF-LIST] Issue #900: Single permission pass for {len(candidate_paths)} candidates"
            )

            # Step 2: SINGLE permission filter call
            filter_start = time.time()
            allowed_list = self._permission_enforcer.filter_list(list(candidate_paths), ctx)
            allowed_set = set(allowed_list)
            filter_elapsed = time.time() - filter_start

            # Add pre-approved directories from fast path (already checked has_accessible_descendants)
            if _preapproved_dirs:
                allowed_set.update(_preapproved_dirs)
                logger.debug(
                    f"[PERF-LIST] Added {len(_preapproved_dirs)} pre-approved dirs to allowed_set"
                )

            logger.debug(
                f"[PERF-LIST] Permission filter: {filter_elapsed:.3f}s, allowed {len(allowed_set)}/{len(candidate_paths)} paths"
            )

            # Step 3: Filter results using allowed_set (O(1) lookups, no permission calls)
            results_before = len(results)
            results = [meta for meta in results if meta.path in allowed_set]
            logger.info(
                f"[LIST-DEBUG] after perm filter: {len(results)} results (was {results_before}), allowed_set={len(allowed_set)}, preapproved={len(_preapproved_dirs)}"
            )
            logger.info(f"[LIST-DEBUG] allowed_set sample: {list(allowed_set)[:5]}")

            perm_total = time.time() - perm_start
            logger.debug(f"[PERF-LIST] Total permission filtering: {perm_total:.3f}s")
        else:
            # No permission enforcement - get backend dirs for later
            if not recursive:
                backend_dirs = self._get_backend_directory_entries(path)

        # Sort by path name
        _sort_start = _time.time()
        results.sort(key=lambda m: m.path)
        logger.info(
            f"[LIST-TIMING] sort_results: {(_time.time() - _sort_start) * 1000:.1f}ms, {len(results)} results"
        )

        # Add directories to results (infer from file paths + check backend)
        # This ensures empty directories show up in listings
        _dir_start = _time.time()
        directories: set[str] = set()

        # Extract directories from directory marker files in results (v0.3.9+)
        # These are files with mime_type="inode/directory" created by mkdir
        for meta in results:
            if meta.mime_type == "inode/directory":
                directories.add(meta.path)

        logger.info(f"[LIST-TIMING] recursive={recursive}, results_count={len(results)}")
        if not recursive:
            # For non-recursive listings, infer immediate subdirectories from file paths
            # Use allowed_set to filter (already computed above, no new permission checks)
            if self._enforce_permissions:
                # Use all_files filtered by allowed_set for directory inference
                for meta in all_files:
                    if meta.path in allowed_set:
                        # Get relative path
                        rel_path = meta.path[len(path) :] if path != "/" else meta.path[1:]
                        # Check if there's a directory component
                        if "/" in rel_path:
                            # Extract first directory component
                            dir_name = rel_path.split("/")[0]
                            dir_path = path + dir_name if path != "/" else "/" + dir_name
                            directories.add(dir_path)

                # Check backend directories for access
                # A directory is accessible if:
                # 1. User has TRAVERSE permission (can navigate into it), OR
                # 2. User has READ permission (in allowed_set), OR
                # 3. Any file under it is in allowed_set (has accessible descendants)
                logger.info(
                    f"[LIST-TIMING] backend_dirs count: {len(backend_dirs)}, allowed_set size: {len(allowed_set)}"
                )
                # Debug: log backend_dirs and sample of allowed_set prefixes
                logger.info(f"[LIST-DEBUG] backend_dirs: {list(backend_dirs)[:10]}")
                # Get unique top-level prefixes from allowed_set
                top_level_prefixes = set()
                for p in allowed_set:
                    parts = p.strip("/").split("/")
                    if parts and parts[0]:
                        top_level_prefixes.add("/" + parts[0])
                logger.info(
                    f"[LIST-DEBUG] allowed_set top-level prefixes: {sorted(top_level_prefixes)}"
                )
                _bd_start = _time.time()
                _traverse_checks = 0
                _prefix_checks = 0
                dirs_needing_traverse: list[str] = []

                for dir_path in backend_dirs:
                    # Fast path 1: check if already in allowed_set (READ permission)
                    if dir_path in allowed_set:
                        directories.add(dir_path)
                        continue

                    # Fast path 2: check if any allowed path starts with this directory
                    # (has accessible descendants) - do this BEFORE slow TRAVERSE check
                    dir_prefix = dir_path.rstrip("/") + "/"
                    if any(p.startswith(dir_prefix) for p in allowed_set):
                        _prefix_checks += 1
                        directories.add(dir_path)
                        continue

                    # Collect dirs needing TRAVERSE check for batch processing
                    dirs_needing_traverse.append(dir_path)

                # SKIP TRAVERSE CHECK: Empty dirs without accessible files are rarely useful
                # This saves 200-300ms per dir that would need TRAVERSE permission check
                # Users can still navigate into these dirs if they have the direct URL
                _traverse_checks = len(dirs_needing_traverse)
                # Uncomment below to enable TRAVERSE checks (slow):
                # for dir_path in dirs_needing_traverse:
                #     if self._permission_enforcer.check(dir_path, Permission.TRAVERSE, ctx):
                #         directories.add(dir_path)

                logger.info(
                    f"[LIST-TIMING] backend_dir_checks: {(_time.time() - _bd_start) * 1000:.1f}ms, traverse_checks={_traverse_checks}, prefix_checks={_prefix_checks}"
                )
            else:
                # No permissions: infer directories from all files
                for meta in all_files:
                    rel_path = meta.path[len(path) :] if path != "/" else meta.path[1:]
                    if "/" in rel_path:
                        dir_name = rel_path.split("/")[0]
                        dir_path = path + dir_name if path != "/" else "/" + dir_name
                        directories.add(dir_path)

                # Add all backend directories
                directories.update(backend_dirs)

        logger.info(
            f"[LIST-TIMING] dir_processing: {(_time.time() - _dir_start) * 1000:.1f}ms, {len(directories)} dirs"
        )
        logger.info(f"[LIST-DEBUG] FINAL directories: {sorted(directories)[:10]}")
        logger.info(
            f"[LIST-DEBUG] FINAL results: {len(results)} items, sample: {[m.path for m in results[:5]]}"
        )

        if details:
            # Filter out directory metadata markers to avoid duplicates
            # Directories are already included in dir_results below
            _details_start = _time.time()
            file_results = [
                {
                    "path": meta.path,
                    "size": meta.size,
                    "modified_at": meta.modified_at,
                    "created_at": meta.created_at,
                    "etag": meta.etag,
                    "mime_type": meta.mime_type,
                    "is_directory": False,
                }
                for meta in results
                if meta.mime_type != "inode/directory"  # Exclude directory metadata markers
            ]
            logger.info(
                f"[LIST-TIMING] build_file_results: {(_time.time() - _details_start) * 1000:.1f}ms, {len(file_results)} files"
            )

            # Add directory entries
            dir_results = [
                {
                    "path": dir_path,
                    "size": 0,
                    "modified_at": None,
                    "created_at": None,
                    "etag": None,
                    "mime_type": None,
                    "is_directory": True,
                }
                for dir_path in sorted(directories)
            ]

            # Combine and sort
            _build_start = _time.time()
            all_results = file_results + dir_results
            all_results.sort(key=lambda x: str(x["path"]))
            logger.info(
                f"[LIST-TIMING] build_details_response: {(_time.time() - _build_start) * 1000:.1f}ms, {len(all_results)} results"
            )
            logger.info(
                f"[LIST-TIMING] TOTAL: {(_time.time() - _list_start) * 1000:.1f}ms for path={path}"
            )
            return all_results
        else:
            # Return paths only (filter out directory metadata markers)
            _build_start = _time.time()
            file_paths = [meta.path for meta in results if meta.mime_type != "inode/directory"]
            all_paths = file_paths + sorted(directories)
            all_paths.sort()
            logger.info(
                f"[LIST-TIMING] build_paths_response: {(_time.time() - _build_start) * 1000:.1f}ms, {len(all_paths)} paths"
            )
            logger.info(
                f"[LIST-TIMING] TOTAL: {(_time.time() - _list_start) * 1000:.1f}ms for path={path}"
            )
            return all_paths

    def _list_paginated(
        self,
        path: str,
        recursive: bool,
        details: bool,
        limit: int,
        cursor: str | None,
        context: OperationContext | None,
    ) -> PaginatedResult:
        """Internal paginated list implementation (Issue #937).

        Handles permission filtering with over-fetch strategy:
        1. Fetch limit * 1.5 items from DB
        2. Filter by permissions
        3. If not enough items, continue fetching until limit reached or no more data

        Args:
            path: Directory path to list
            recursive: Include nested subdirectories
            details: Return metadata dicts vs paths
            limit: Max items per page
            cursor: Continuation token from previous page
            context: Operation context for permission filtering

        Returns:
            PaginatedResult with items, next_cursor, and has_more
        """
        from nexus.core.metadata import PaginatedResult
        from nexus.core.pagination import encode_cursor

        context = context or self._default_context
        import time as _time

        _start = _time.time()

        # Extract tenant_id for PREWHERE-style DB filtering (Issue #904)
        list_tenant_id: str | None = None
        if self._enforce_permissions and context and hasattr(context, "tenant_id"):
            list_tenant_id = context.tenant_id

        logger.info(
            f"[LIST-PAGINATED] START path={path}, recursive={recursive}, limit={limit}, cursor={cursor}, tenant={list_tenant_id}"
        )

        # Normalize path to prefix for metadata query
        if path and path != "/":
            path = self._validate_path(path)
        if path and not path.endswith("/"):
            path = path + "/"
        list_prefix = path if path != "/" else ""

        # Over-fetch strategy for permission filtering
        # Fetch 1.5x to account for items that get filtered out
        buffer_multiplier = 1.5
        fetch_limit = int(limit * buffer_multiplier)

        collected_items: builtins.list[Any] = []
        current_cursor = cursor
        has_more = True

        while len(collected_items) < limit and has_more:
            # Fetch batch from metadata store using keyset pagination
            _db_start = _time.time()
            batch = self.metadata.list_paginated(
                prefix=list_prefix,
                recursive=recursive,
                limit=fetch_limit,
                cursor=current_cursor,
                tenant_id=list_tenant_id,
            )
            _db_elapsed = (_time.time() - _db_start) * 1000
            sample_paths = [item.path for item in batch.items[:5]]
            logger.info(
                f"[LIST-PAGINATED] DB batch: {len(batch.items)} items in {_db_elapsed:.1f}ms, sample: {sample_paths}"
            )

            # Filter by permissions
            if self._enforce_permissions and context:
                paths = [item.path for item in batch.items]
                allowed_paths = set(self._permission_enforcer.filter_list(paths, context))
                filtered_items = [item for item in batch.items if item.path in allowed_paths]
                logger.info(
                    f"[LIST-PAGINATED] After perm filter: {len(filtered_items)}/{len(batch.items)} allowed"
                )
            else:
                filtered_items = batch.items

            collected_items.extend(filtered_items)
            has_more = batch.has_more
            current_cursor = batch.next_cursor

            # Avoid infinite loop if no items pass filter
            if not batch.items:
                break

        # Trim to requested limit
        result_items = collected_items[:limit]
        final_has_more = has_more or len(collected_items) > limit

        # Generate cursor for next page
        next_cursor = None
        if final_has_more and result_items:
            last_item = result_items[-1]
            filters = {
                "prefix": list_prefix,
                "recursive": recursive,
                "tenant_id": list_tenant_id,
            }
            # Note: path_id is internal to metadata store, we pass None here
            # The actual cursor will use the path as the primary key
            next_cursor = encode_cursor(
                last_path=last_item.path,
                last_path_id=None,  # SQLAlchemy store handles this internally
                filters=filters,
            )

        # Convert to output format
        if details:
            items_output = [
                {
                    "path": meta.path,
                    "size": meta.size,
                    "modified_at": meta.modified_at,
                    "created_at": meta.created_at,
                    "etag": meta.etag,
                    "mime_type": meta.mime_type,
                    "is_directory": meta.is_directory if hasattr(meta, "is_directory") else False,
                }
                for meta in result_items
            ]
        else:
            items_output = [meta.path for meta in result_items]

        return PaginatedResult(
            items=items_output,
            next_cursor=next_cursor,
            has_more=final_has_more,
            total_count=None,  # Skip expensive COUNT(*) at scale
        )

    @rpc_expose(description="Find files by glob pattern")
    def glob(self, pattern: str, path: str = "/", context: Any = None) -> builtins.list[str]:
        """
        Find files matching a glob pattern.

        Supports standard glob patterns:
        - `*` matches any sequence of characters (except `/`)
        - `**` matches any sequence of characters including `/` (recursive)
        - `?` matches any single character
        - `[...]` matches any character in the brackets

        Args:
            pattern: Glob pattern to match (e.g., "**/*.py", "data/*.csv", "test_*.py")
            path: Base path to search from (default: "/")

        Returns:
            List of matching file paths, sorted by name

        Examples:
            # Find all Python files recursively
            fs.glob("**/*.py")  # Returns: ["/src/main.py", "/tests/test_foo.py", ...]

            # Find all CSV files in data directory
            fs.glob("*.csv", "/data")  # Returns: ["/data/file1.csv", "/data/file2.csv"]

            # Find all test files
            fs.glob("test_*.py")  # Returns: ["/test_foo.py", "/test_bar.py"]
        """
        if path and path != "/":
            path = self._validate_path(path)

        import time

        glob_start = time.time()

        # Phase 1: Directory-level pruning optimization (Issue #929: DIRECTORY_PRUNED strategy)
        # Extract static prefix from pattern to limit directory traversal.
        # For "src/components/**/*.tsx", only list files under "src/components/"
        # instead of the entire tree. This can provide 10-100x speedup.
        search_path = path
        static_prefix = None
        if path == "/" or path == "":
            static_prefix = glob_fast.extract_static_prefix(pattern)
            if static_prefix:
                if static_prefix.startswith("/"):
                    search_path = static_prefix.rstrip("/")
                else:
                    search_path = "/" + static_prefix.rstrip("/")
                logger.debug(
                    f"[GLOB] Directory pruning: pattern='{pattern}' -> search_path='{search_path}'"
                )

        # Phase 2: Get accessible files (with ReBAC permission filtering)
        list_start = time.time()
        accessible_files: list[str] = cast(
            list[str], self.list(search_path, recursive=True, context=context)
        )
        list_elapsed = time.time() - list_start
        logger.debug(
            f"[GLOB] Phase 1: list() found {len(accessible_files)} files in {list_elapsed:.3f}s"
        )

        if not accessible_files:
            return []

        # Phase 3: Select strategy based on pattern and file count (Issue #929)
        strategy = self._select_glob_strategy(pattern, len(accessible_files))
        logger.debug(
            f"[GLOB] Strategy selected: {strategy.value} "
            f"(pattern='{pattern}', files={len(accessible_files)})"
        )

        # Build full pattern for matching
        if not path.endswith("/"):
            path = path + "/"
        if path == "/":
            full_pattern = pattern
            # Auto-prepend **/ for patterns that look relative
            if (
                "**" not in full_pattern
                and not full_pattern.startswith(("workspace/", "shared/", "external/"))
                and "/" in full_pattern
            ):
                full_pattern = "**/" + full_pattern
        else:
            base_path = path[1:] if path.startswith("/") else path
            full_pattern = base_path + pattern

        # Phase 4: Execute strategy-specific matching
        match_start = time.time()
        matches: list[str] = []

        # =====================================================================
        # Strategy: RUST_BULK or DIRECTORY_PRUNED - Use Rust acceleration
        # =====================================================================
        if strategy in (GlobStrategy.RUST_BULK, GlobStrategy.DIRECTORY_PRUNED):
            rust_pattern = full_pattern if full_pattern.startswith("/") else "/" + full_pattern
            rust_matches = glob_fast.glob_match_bulk([rust_pattern], accessible_files)
            if rust_matches is not None:
                matches = rust_matches
            else:
                # Fall through to Python implementation
                logger.debug("[GLOB] Rust acceleration failed, falling back to Python")
                strategy = (
                    GlobStrategy.REGEX_COMPILED
                    if "**" in full_pattern
                    else GlobStrategy.FNMATCH_SIMPLE
                )

        # =====================================================================
        # Strategy: REGEX_COMPILED - Use regex for complex patterns with **
        # =====================================================================
        if strategy == GlobStrategy.REGEX_COMPILED and not matches:
            parts = full_pattern.split("**")
            regex_parts = []
            for i, part in enumerate(parts):
                if i > 0:
                    regex_parts.append("(?:.*/)?")
                escaped = re.escape(part)
                escaped = escaped.replace(r"\*", "[^/]*")
                escaped = escaped.replace(r"\?", ".")
                escaped = escaped.replace(r"\[", "[").replace(r"\]", "]")
                while escaped.startswith("/"):
                    escaped = escaped[1:]
                regex_parts.append(escaped)

            regex_pattern = "^/" + "".join(regex_parts) + "$"
            compiled_regex = re.compile(regex_pattern)

            matches = [fp for fp in accessible_files if compiled_regex.match(fp)]

        # =====================================================================
        # Strategy: FNMATCH_SIMPLE - Use fnmatch for simple patterns
        # =====================================================================
        if strategy == GlobStrategy.FNMATCH_SIMPLE and not matches:
            for file_path in accessible_files:
                path_for_match = file_path[1:] if file_path.startswith("/") else file_path
                if fnmatch.fnmatch(path_for_match, full_pattern):
                    matches.append(file_path)

        match_elapsed = time.time() - match_start
        total_elapsed = time.time() - glob_start
        logger.debug(
            f"[GLOB] {strategy.value}: matched {len(matches)}/{len(accessible_files)} files "
            f"in {match_elapsed:.3f}s (total: {total_elapsed:.3f}s)"
        )

        return sorted(matches)

    @rpc_expose(description="Execute multiple glob patterns in single call")
    def glob_batch(
        self, patterns: builtins.list[str], path: str = "/", context: Any = None
    ) -> dict[str, builtins.list[str]]:
        """
        Execute multiple glob patterns in a single call (Issue #859).

        This reduces network round trips when matching many patterns at once.
        Processing 10 patterns requires 1 round trip instead of 10.

        Args:
            patterns: List of glob patterns to match
            path: Base path to search from (default: "/")
            context: Operation context for permission filtering

        Returns:
            Dictionary mapping each pattern to its list of matching file paths.
            Empty list for patterns with no matches.

        Performance:
            - Single RPC call instead of N calls
            - 10x fewer round trips for multi-pattern operations
            - Shares file listing across all patterns (major optimization)

        Examples:
            >>> results = nx.glob_batch(["**/*.py", "**/*.js", "*.txt"])
            >>> print(results["**/*.py"])
            ["/src/main.py", "/tests/test_foo.py"]
            >>> print(results["**/*.js"])
            ["/static/app.js"]
            >>> print(results["*.txt"])
            ["/README.txt"]
        """
        results: dict[str, list[str]] = {}

        # Get all accessible files once (shared across all patterns)
        # This is the major optimization - we list files only once
        try:
            if path and path != "/":
                path = self._validate_path(path)
            accessible_files: builtins.list[str] = cast(
                builtins.list[str], self.list(path, recursive=True, context=context)
            )
        except Exception:
            # If listing fails, return empty results for all patterns
            for pattern in patterns:
                results[pattern] = []
            return results

        # Process each pattern using Rust acceleration when available
        for pattern in patterns:
            try:
                # Build full pattern
                search_path = path
                if not search_path.endswith("/"):
                    search_path = search_path + "/"
                if search_path == "/":
                    full_pattern = pattern
                    # Auto-prepend **/ for patterns that look relative
                    if (
                        "**" not in full_pattern
                        and not full_pattern.startswith(("workspace/", "shared/", "external/"))
                        and "/" in full_pattern
                    ):
                        full_pattern = "**/" + full_pattern
                else:
                    base_path = search_path[1:] if search_path.startswith("/") else search_path
                    full_pattern = base_path + pattern

                # Try Rust acceleration first (10-20x faster)
                rust_pattern = full_pattern if full_pattern.startswith("/") else "/" + full_pattern
                rust_matches = glob_fast.glob_match_bulk([rust_pattern], accessible_files)
                if rust_matches is not None:
                    results[pattern] = sorted(rust_matches)
                else:
                    # Fallback to Python implementation
                    if "**" in full_pattern:
                        # Convert glob pattern to regex for ** matching
                        parts = full_pattern.split("**")
                        regex_parts = []
                        for i, part in enumerate(parts):
                            if i > 0:
                                regex_parts.append("(?:.*/)?")
                            escaped = re.escape(part)
                            escaped = escaped.replace(r"\*", "[^/]*")
                            escaped = escaped.replace(r"\?", ".")
                            escaped = escaped.replace(r"\[", "[").replace(r"\]", "]")
                            while escaped.startswith("/"):
                                escaped = escaped[1:]
                            regex_parts.append(escaped)
                        regex_pattern = "^/" + "".join(regex_parts) + "$"

                        matches = [
                            file_path
                            for file_path in accessible_files
                            if re.match(regex_pattern, file_path)
                        ]
                    else:
                        # Use fnmatch for simpler patterns
                        matches = []
                        for file_path in accessible_files:
                            path_for_match = (
                                file_path[1:] if file_path.startswith("/") else file_path
                            )
                            if fnmatch.fnmatch(path_for_match, full_pattern):
                                matches.append(file_path)

                    results[pattern] = sorted(matches)
            except Exception:
                results[pattern] = []

        return results

    @rpc_expose(description="Search file contents")
    def grep(
        self,
        pattern: str,
        path: str = "/",
        file_pattern: str | None = None,
        ignore_case: bool = False,
        max_results: int = 100,  # Reduced from 1000 for faster first response
        search_mode: str = "auto",  # noqa: ARG002 - Kept for backward compatibility, but ignored
        context: Any = None,
    ) -> builtins.list[dict[str, Any]]:
        r"""
        Search file contents using regex patterns.

        Searches use pre-parsed/cached text when available:
        - For connector files (GCS, S3, etc.): Uses content_cache.content_text
        - For local files: Uses file_metadata.parsed_text
        - Falls back to raw file content if no cached text available

        Args:
            pattern: Regex pattern to search for in file contents
            path: Base path to search from (default: "/")
            file_pattern: Optional glob pattern to filter files (e.g., "*.py")
            ignore_case: If True, perform case-insensitive search (default: False)
            max_results: Maximum number of results to return (default: 100)
            search_mode: Deprecated, kept for backward compatibility (ignored)
            context: Operation context for permission filtering

        Returns:
            List of match dicts, each containing:
            - file: File path
            - line: Line number (1-indexed)
            - content: Matched line content
            - match: The matched text

        Examples:
            # Search for "TODO" in all files
            fs.grep("TODO")

            # Search for function definitions in Python files
            fs.grep(r"def \w+", file_pattern="**/*.py")

            # Search in PDFs (uses cached parsed text)
            fs.grep("revenue", file_pattern="**/*.pdf")

            # Case-insensitive search
            fs.grep("error", ignore_case=True)
        """
        if path and path != "/":
            path = self._validate_path(path)

        # Compile regex pattern
        flags = re.IGNORECASE if ignore_case else 0
        try:
            regex = re.compile(pattern, flags)
        except re.error as e:
            raise ValueError(f"Invalid regex pattern: {e}") from e

        import time

        grep_start = time.time()

        # Phase 1: Get files to search
        list_start = time.time()
        if file_pattern:
            files = self.glob(file_pattern, path, context=context)
        else:
            files = cast(list[str], self.list(path, recursive=True, context=context))
        list_elapsed = time.time() - list_start
        logger.debug(f"[GREP] Phase 1: list() found {len(files)} files in {list_elapsed:.3f}s")

        if not files:
            return []

        # Phase 2: Bulk fetch searchable text (from content_cache or file_metadata)
        text_start = time.time()
        searchable_texts = self.metadata.get_searchable_text_bulk(files)
        text_elapsed = time.time() - text_start
        logger.debug(
            f"[GREP] Phase 2: get_searchable_text_bulk() returned "
            f"{len(searchable_texts)} texts in {text_elapsed:.3f}s"
        )

        # Calculate cached text ratio for strategy selection (Issue #929)
        cached_text_ratio = len(searchable_texts) / len(files) if files else 0.0
        files_needing_raw = [f for f in files if f not in searchable_texts]

        # Phase 3: Select strategy based on data characteristics (Issue #929)
        strategy = self._select_grep_strategy(
            file_count=len(files),
            cached_text_ratio=cached_text_ratio,
        )
        logger.debug(
            f"[GREP] Strategy selected: {strategy.value} "
            f"(files={len(files)}, cached_ratio={cached_text_ratio:.2f})"
        )

        # Phase 4: Execute strategy-specific search
        results: list[dict[str, Any]] = []

        # =====================================================================
        # Strategy: ZOEKT_INDEX - Use Zoekt trigram index for large codebases
        # =====================================================================
        if strategy == SearchStrategy.ZOEKT_INDEX:
            zoekt_results = self._try_grep_with_zoekt(
                pattern=pattern,
                path=path,
                file_pattern=file_pattern,
                ignore_case=ignore_case,
                max_results=max_results,
                context=context,
            )
            if zoekt_results is not None:
                total_elapsed = time.time() - grep_start
                logger.debug(
                    f"[GREP] ZOEKT_INDEX completed: {total_elapsed:.3f}s, "
                    f"{len(zoekt_results)} results"
                )
                return zoekt_results
            # Fall through to other strategies if Zoekt fails
            logger.debug("[GREP] ZOEKT_INDEX failed, falling back to RUST_BULK")
            strategy = SearchStrategy.RUST_BULK

        # =====================================================================
        # Strategy: CACHED_TEXT - Search pre-parsed text (fastest path)
        # =====================================================================
        if strategy == SearchStrategy.CACHED_TEXT or searchable_texts:
            search_start = time.time()
            for file_path, text in searchable_texts.items():
                if len(results) >= max_results:
                    break
                for line_num, line in enumerate(text.splitlines(), start=1):
                    if len(results) >= max_results:
                        break
                    match_obj = regex.search(line)
                    if match_obj:
                        results.append(
                            {
                                "file": file_path,
                                "line": line_num,
                                "content": line,
                                "match": match_obj.group(0),
                            }
                        )
            search_elapsed = time.time() - search_start
            logger.debug(f"[GREP] Cached text search: {search_elapsed:.3f}s")

            # If CACHED_TEXT strategy and we have enough results, return early
            if strategy == SearchStrategy.CACHED_TEXT and len(results) >= max_results:
                total_elapsed = time.time() - grep_start
                logger.debug(
                    f"[GREP] CACHED_TEXT completed: {total_elapsed:.3f}s, {len(results)} results"
                )
                return results[:max_results]

        # If we have enough results from cached text, return
        if len(results) >= max_results:
            total_elapsed = time.time() - grep_start
            logger.debug(f"[GREP] TOTAL: {total_elapsed:.3f}s, {len(results)} results")
            return results[:max_results]

        # =====================================================================
        # Process remaining files that need raw content
        # =====================================================================
        if not files_needing_raw:
            total_elapsed = time.time() - grep_start
            logger.debug(f"[GREP] TOTAL: {total_elapsed:.3f}s, {len(results)} results")
            return results

        remaining_results = max_results - len(results)
        raw_start = time.time()

        # =====================================================================
        # Strategy: PARALLEL_POOL - Parallel processing for medium-large sets
        # =====================================================================
        if strategy == SearchStrategy.PARALLEL_POOL:
            parallel_results = self._grep_parallel(
                regex=regex,
                files=files_needing_raw,
                max_results=remaining_results,
                context=context,
            )
            results.extend(parallel_results)

        # =====================================================================
        # Strategy: RUST_BULK or SEQUENTIAL - Process remaining files
        # =====================================================================
        elif strategy in (SearchStrategy.RUST_BULK, SearchStrategy.SEQUENTIAL):
            # Try mmap-accelerated grep first (Issue #893)
            mmap_used = False
            if grep_fast.is_mmap_available():
                try:
                    from nexus.storage.file_cache import get_file_cache

                    tenant_id, _, _ = self._get_routing_params(context)
                    if tenant_id:
                        file_cache = get_file_cache()
                        disk_paths = file_cache.get_disk_paths_bulk(tenant_id, files_needing_raw)

                        if disk_paths:
                            disk_to_virtual = {dp: vp for vp, dp in disk_paths.items()}
                            disk_path_list = list(disk_paths.values())

                            mmap_results = grep_fast.grep_files_mmap(
                                pattern,
                                disk_path_list,
                                ignore_case=ignore_case,
                                max_results=remaining_results,
                            )

                            if mmap_results is not None:
                                for match in mmap_results:
                                    disk_path = match.get("file", "")
                                    virtual_path = disk_to_virtual.get(disk_path, disk_path)
                                    match["file"] = virtual_path
                                results.extend(mmap_results)
                                mmap_used = True

                                files_needing_raw = [
                                    f for f in files_needing_raw if f not in disk_paths
                                ]
                                remaining_results = max_results - len(results)

                except Exception as e:
                    logger.debug(f"[GREP] Mmap optimization failed: {e}")

            # Try Rust-accelerated grep for remaining files
            if (
                strategy == SearchStrategy.RUST_BULK
                and grep_fast.is_available()
                and remaining_results > 0
                and files_needing_raw
            ):
                bulk_results = self.read_bulk(files_needing_raw, context=context, skip_errors=True)
                file_contents: dict[str, bytes] = {
                    fp: content
                    for fp, content in bulk_results.items()
                    if content is not None and isinstance(content, bytes)
                }

                rust_results = grep_fast.grep_bulk(
                    pattern,
                    file_contents,
                    ignore_case=ignore_case,
                    max_results=remaining_results,
                )

                if rust_results is not None:
                    results.extend(rust_results)

            # Python sequential fallback
            elif not mmap_used and files_needing_raw:
                for file_path in files_needing_raw:
                    if len(results) >= max_results:
                        break

                    try:
                        read_result = self.read(file_path, context=context)
                        if not isinstance(read_result, bytes):
                            continue

                        try:
                            text = read_result.decode("utf-8")
                        except UnicodeDecodeError:
                            continue

                        for line_num, line in enumerate(text.splitlines(), start=1):
                            if len(results) >= max_results:
                                break

                            match_obj = regex.search(line)
                            if match_obj:
                                results.append(
                                    {
                                        "file": file_path,
                                        "line": line_num,
                                        "content": line,
                                        "match": match_obj.group(0),
                                    }
                                )
                    except Exception:
                        continue

        raw_elapsed = time.time() - raw_start
        logger.debug(
            f"[GREP] Raw content processing ({strategy.value}): "
            f"{len(files_needing_raw)} files in {raw_elapsed:.3f}s"
        )

        total_elapsed = time.time() - grep_start
        logger.debug(f"[GREP] TOTAL: {total_elapsed:.3f}s, {len(results)} results")

        return results[:max_results]

    def _try_grep_with_zoekt(
        self,
        pattern: str,
        path: str,
        file_pattern: str | None,
        ignore_case: bool,
        max_results: int,
        context: Any,
    ) -> builtins.list[dict[str, Any]] | None:
        """Try to use Zoekt for grep (returns None if not available).

        Zoekt provides sub-50ms search on large codebases using trigram indexing.
        This is an optional optimization - grep falls back to Rust regex if
        Zoekt is not available.

        Args:
            pattern: Regex pattern to search for
            path: Base path to search from
            file_pattern: Optional glob pattern to filter files
            ignore_case: If True, perform case-insensitive search
            max_results: Maximum number of results
            context: Operation context for permission filtering

        Returns:
            List of match dicts if Zoekt succeeded, None to fall back to standard grep
        """
        import asyncio
        import logging

        logger = logging.getLogger(__name__)

        try:
            # Import Zoekt client (may not be available)
            from nexus.search.zoekt_client import get_zoekt_client
        except ImportError:
            return None

        client = get_zoekt_client()

        # Check if Zoekt is available (sync wrapper for async check)
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # We're in an async context, can't use run_until_complete
                # Fall back to standard grep
                return None
            is_available = loop.run_until_complete(client.is_available())
        except RuntimeError:
            # No event loop, create one
            is_available = asyncio.run(client.is_available())

        if not is_available:
            return None

        logger.debug("[GREP] Using Zoekt for accelerated search")

        try:
            # Build Zoekt query
            zoekt_query = pattern
            if ignore_case:
                zoekt_query = f"(?i){pattern}"
            if path and path != "/":
                # Limit search to path prefix
                zoekt_query = f"file:{path.lstrip('/')}/ {zoekt_query}"

            # Run Zoekt search
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    return None  # Can't run async in sync context
                matches = loop.run_until_complete(client.search(zoekt_query, num=max_results * 3))
            except RuntimeError:
                matches = asyncio.run(client.search(zoekt_query, num=max_results * 3))

            if not matches:
                # Zoekt returned no results - let standard grep try
                # (Zoekt may not have indexed all files)
                return None

            # Apply file_pattern filter if specified
            if file_pattern:
                matches = [m for m in matches if glob_fast.glob_match(m.file, [file_pattern])]

            # Extract unique file paths for permission check
            unique_files = list({m.file for m in matches})

            # Filter by ReBAC permissions using existing filter_list
            if hasattr(self, "_permission_enforcer") and context:
                permitted_files = set(self._permission_enforcer.filter_list(unique_files, context))
            else:
                permitted_files = set(unique_files)

            # Build results (only permitted files)
            results = []
            for match in matches:
                if match.file in permitted_files:
                    results.append(
                        {
                            "file": match.file,
                            "line": match.line,
                            "content": match.content,
                            "match": match.match,
                        }
                    )
                    if len(results) >= max_results:
                        break

            logger.debug(
                f"[GREP] Zoekt: {len(matches)} raw matches, "
                f"{len(permitted_files)}/{len(unique_files)} permitted, "
                f"{len(results)} final results"
            )

            return results

        except Exception as e:
            logger.warning(f"[GREP] Zoekt search failed, falling back: {e}")
            return None

    # =========================================================================
    # Issue #929: Adaptive Algorithm Selection
    # =========================================================================

    def _is_zoekt_available(self) -> bool:
        """Check if Zoekt indexing service is available (cached check)."""
        try:
            from nexus.search.zoekt_client import get_zoekt_client

            client = get_zoekt_client()
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    return False
                return loop.run_until_complete(client.is_available())
            except RuntimeError:
                return asyncio.run(client.is_available())
        except ImportError:
            return False
        except Exception:
            return False

    def _select_grep_strategy(
        self,
        file_count: int,
        cached_text_ratio: float,
        zoekt_available: bool | None = None,
    ) -> SearchStrategy:
        """Select optimal grep strategy based on data characteristics (Issue #929).

        Decision tree inspired by ClickHouse's adaptive algorithm selection:
        1. Check if most files have cached text (fastest path)
        2. For small file sets, use sequential (no parallelization overhead)
        3. For large file sets with Zoekt, use indexed search
        4. For medium file sets, use Rust bulk or parallel processing

        Args:
            file_count: Number of files to search
            cached_text_ratio: Ratio of files with pre-parsed text (0.0-1.0)
            zoekt_available: Whether Zoekt is available (None = check lazily)

        Returns:
            SearchStrategy enum indicating optimal approach
        """
        # If most files have cached text, use that path (fastest - no I/O)
        if cached_text_ratio >= GREP_CACHED_TEXT_RATIO:
            logger.debug(
                f"[GREP-STRATEGY] CACHED_TEXT selected: "
                f"cached_ratio={cached_text_ratio:.2f} >= {GREP_CACHED_TEXT_RATIO}"
            )
            return SearchStrategy.CACHED_TEXT

        # Small file sets - sequential is fastest (no overhead)
        if file_count < GREP_SEQUENTIAL_THRESHOLD:
            logger.debug(
                f"[GREP-STRATEGY] SEQUENTIAL selected: "
                f"file_count={file_count} < {GREP_SEQUENTIAL_THRESHOLD}"
            )
            return SearchStrategy.SEQUENTIAL

        # Large file sets with Zoekt available - use indexed search
        if file_count > GREP_ZOEKT_THRESHOLD:
            # Lazy check Zoekt availability if not provided
            if zoekt_available is None:
                zoekt_available = self._is_zoekt_available()
            if zoekt_available:
                logger.debug(
                    f"[GREP-STRATEGY] ZOEKT_INDEX selected: "
                    f"file_count={file_count} > {GREP_ZOEKT_THRESHOLD}"
                )
                return SearchStrategy.ZOEKT_INDEX

        # Medium-large file sets - use parallel if beneficial
        if file_count >= GREP_PARALLEL_THRESHOLD and file_count <= 10000:
            logger.debug(
                f"[GREP-STRATEGY] PARALLEL_POOL selected: "
                f"{GREP_PARALLEL_THRESHOLD} <= file_count={file_count} <= 10000"
            )
            return SearchStrategy.PARALLEL_POOL

        # Default: Rust bulk for medium sets
        if grep_fast.is_available():
            logger.debug(
                f"[GREP-STRATEGY] RUST_BULK selected: file_count={file_count}, Rust available"
            )
            return SearchStrategy.RUST_BULK

        # Fallback to sequential
        logger.debug(f"[GREP-STRATEGY] SEQUENTIAL selected (fallback): file_count={file_count}")
        return SearchStrategy.SEQUENTIAL

    def _select_glob_strategy(
        self,
        pattern: str,
        file_count: int,
    ) -> GlobStrategy:
        """Select optimal glob strategy based on pattern and file count (Issue #929).

        Args:
            pattern: Glob pattern to match
            file_count: Number of files to match against

        Returns:
            GlobStrategy enum indicating optimal approach
        """
        # Check for static prefix (directory pruning optimization)
        static_prefix = glob_fast.extract_static_prefix(pattern)
        if static_prefix:
            logger.debug(
                f"[GLOB-STRATEGY] DIRECTORY_PRUNED selected: static_prefix='{static_prefix}'"
            )
            return GlobStrategy.DIRECTORY_PRUNED

        # Large file sets with Rust available
        if file_count > GLOB_RUST_THRESHOLD and glob_fast.is_available():
            logger.debug(
                f"[GLOB-STRATEGY] RUST_BULK selected: "
                f"file_count={file_count} > {GLOB_RUST_THRESHOLD}"
            )
            return GlobStrategy.RUST_BULK

        # Complex patterns with ** need regex
        if "**" in pattern:
            logger.debug("[GLOB-STRATEGY] REGEX_COMPILED selected: pattern contains '**'")
            return GlobStrategy.REGEX_COMPILED

        # Simple patterns - fnmatch is sufficient
        logger.debug(
            f"[GLOB-STRATEGY] FNMATCH_SIMPLE selected: simple pattern, file_count={file_count}"
        )
        return GlobStrategy.FNMATCH_SIMPLE

    def _grep_parallel(
        self,
        regex: re.Pattern[str],
        files: builtins.list[str],
        max_results: int,
        context: Any,
    ) -> builtins.list[dict[str, Any]]:
        """Parallel grep using ThreadPoolExecutor (Issue #929).

        Splits files across worker threads for CPU-bound regex matching.
        Best for 100-10000 files where parallelization overhead is worthwhile.

        Args:
            regex: Compiled regex pattern
            files: List of file paths to search
            max_results: Maximum results to return
            context: Operation context for file reading

        Returns:
            List of match dicts with file, line, content, match keys
        """
        import time

        start_time = time.time()

        # Split files into chunks for parallel processing
        chunk_size = max(1, len(files) // GREP_PARALLEL_WORKERS)
        file_chunks = [files[i : i + chunk_size] for i in range(0, len(files), chunk_size)]

        all_results: builtins.list[dict[str, Any]] = []

        def search_chunk(chunk_files: builtins.list[str]) -> builtins.list[dict[str, Any]]:
            """Search a chunk of files."""
            chunk_results: builtins.list[dict[str, Any]] = []
            for file_path in chunk_files:
                # Early exit if we have enough results globally
                if len(all_results) >= max_results:
                    break

                try:
                    read_result = self.read(file_path, context=context)
                    if not isinstance(read_result, bytes):
                        continue

                    try:
                        text = read_result.decode("utf-8")
                    except UnicodeDecodeError:
                        continue

                    for line_num, line in enumerate(text.splitlines(), start=1):
                        match_obj = regex.search(line)
                        if match_obj:
                            chunk_results.append(
                                {
                                    "file": file_path,
                                    "line": line_num,
                                    "content": line,
                                    "match": match_obj.group(0),
                                }
                            )
                            if len(chunk_results) >= max_results:
                                break
                except Exception:
                    continue

            return chunk_results

        # Execute chunks in parallel
        with ThreadPoolExecutor(max_workers=GREP_PARALLEL_WORKERS) as executor:
            futures = [executor.submit(search_chunk, chunk) for chunk in file_chunks]

            for future in futures:
                try:
                    chunk_results = future.result(timeout=30)
                    all_results.extend(chunk_results)
                    if len(all_results) >= max_results:
                        break
                except Exception as e:
                    logger.debug(f"[GREP-PARALLEL] Chunk failed: {e}")

        elapsed = time.time() - start_time
        logger.debug(
            f"[GREP-PARALLEL] Completed: {len(files)} files, "
            f"{len(all_results)} results in {elapsed:.3f}s"
        )

        return all_results[:max_results]

    # Semantic Search Methods (v0.4.0)

    @rpc_expose(description="Search documents using natural language queries")
    async def semantic_search(
        self,
        query: str,
        path: str = "/",
        limit: int = 10,
        filters: dict[str, Any] | None = None,
        search_mode: str = "semantic",
    ) -> builtins.list[dict[str, Any]]:
        """
        Search documents using natural language queries.

        Supports three search modes:
        - "keyword": Fast keyword search using FTS (no embeddings needed)
        - "semantic": Semantic search using vector embeddings (requires embedding provider)
        - "hybrid": Combines keyword + semantic for best results (requires embedding provider)

        Args:
            query: Natural language query (e.g., "How does authentication work?")
            path: Root path to search (default: all files)
            limit: Maximum number of results (default: 10)
            filters: Optional filters (file_type, etc.)
            search_mode: Search mode - "keyword", "semantic", or "hybrid" (default: "semantic")

        Returns:
            List of search result dicts, each containing:
            - path: File path
            - chunk_index: Index of the chunk in the document
            - chunk_text: Text content of the chunk
            - score: Relevance score (0.0 to 1.0)
            - start_offset: Start offset in the document (optional)
            - end_offset: End offset in the document (optional)

        Examples:
            # Search for information about authentication
            results = await nx.semantic_search("How does authentication work?")

            # Search only in documentation directory
            results = await nx.semantic_search(
                "database migration",
                path="/docs",
                limit=5
            )

            # Search with filters
            results = await nx.semantic_search(
                "error handling",
                filters={"file_type": "python"}
            )

        Raises:
            ValueError: If semantic search is not initialized
        """
        if not hasattr(self, "_semantic_search") or self._semantic_search is None:
            raise ValueError(
                "Semantic search is not initialized. "
                "Initialize with: await nx.initialize_semantic_search()"
            )

        # Use async search for non-blocking DB operations (high throughput)
        if hasattr(self, "_async_search") and self._async_search is not None:
            results = await self._async_search.search(
                query=query,
                limit=limit,
                path_filter=path if path != "/" else None,
                search_mode=search_mode,
            )
            return [
                {
                    "path": result.path,
                    "chunk_index": result.chunk_index,
                    "chunk_text": result.chunk_text,
                    "score": result.score,
                    "start_offset": result.start_offset,
                    "end_offset": result.end_offset,
                    "line_start": result.line_start,
                    "line_end": result.line_end,
                }
                for result in results
            ]

        # Fallback to sync search
        sync_results = await self._semantic_search.search(
            query=query, path=path, limit=limit, filters=filters, search_mode=search_mode
        )

        return [
            {
                "path": result.path,
                "chunk_index": result.chunk_index,
                "chunk_text": result.chunk_text,
                "score": result.score,
                "start_offset": result.start_offset,
                "end_offset": result.end_offset,
                "line_start": result.line_start,
                "line_end": result.line_end,
            }
            for result in sync_results
        ]

    @rpc_expose(description="Index documents for semantic search")
    async def semantic_search_index(
        self, path: str = "/", recursive: bool = True
    ) -> dict[str, int]:
        """
        Index documents for semantic search.

        This method chunks documents and generates embeddings for semantic search.
        You need to run this before using semantic_search().

        Args:
            path: Path to index (file or directory)
            recursive: If True, index directory recursively (default: True)

        Returns:
            Dictionary mapping file paths to number of chunks indexed

        Examples:
            # Index all documents
            await nx.semantic_search_index()

            # Index specific directory
            await nx.semantic_search_index("/docs")

            # Index single file
            await nx.semantic_search_index("/docs/README.md")

        Raises:
            ValueError: If semantic search is not initialized
        """
        if not hasattr(self, "_semantic_search") or self._semantic_search is None:
            raise ValueError(
                "Semantic search is not initialized. "
                "Initialize with: await nx.initialize_semantic_search()"
            )

        # Use async indexing for high throughput
        if hasattr(self, "_async_search") and self._async_search is not None:
            return await self._async_index_documents(path, recursive)

        # Fallback to sync indexing
        # Check if path is a file or directory
        try:
            # Try to read as file
            await asyncio.to_thread(self.read, path)
            # It's a file, index it
            num_chunks = await self._semantic_search.index_document(path)
            return {path: num_chunks}
        except Exception:
            # It's a directory or doesn't exist
            pass

        # Index directory
        if recursive:
            return await self._semantic_search.index_directory(path)
        else:
            # Index only direct files in directory
            files_result = await asyncio.to_thread(self.list, path, recursive=False)
            # Handle PaginatedResult if returned
            files = files_result.items if hasattr(files_result, "items") else files_result
            results: dict[str, int] = {}
            for item in files:
                file_path = item["name"] if isinstance(item, dict) else item
                if not file_path.endswith("/"):  # Skip directories
                    try:
                        num_chunks = await self._semantic_search.index_document(file_path)
                        results[file_path] = num_chunks
                    except Exception:
                        results[file_path] = -1  # Indicate error
            return results

    async def _async_index_documents(self, path: str, recursive: bool) -> dict[str, int]:
        """Index documents using async backend for high throughput.

        Args:
            path: Path to index
            recursive: Index recursively

        Returns:
            Dict mapping path to number of chunks
        """
        from sqlalchemy import select

        from nexus.storage.models import FilePathModel

        # Collect files to index
        files_to_index: list[str] = []

        try:
            # Check if it's a file
            await asyncio.to_thread(self.read, path)
            files_to_index = [path]
        except Exception:
            # It's a directory
            file_list = await asyncio.to_thread(self.list, path, recursive=recursive)
            if hasattr(file_list, "items"):
                # PaginatedResult - use .items
                file_list = file_list.items
            for item in file_list:
                file_path = item if isinstance(item, str) else item.get("path", "")
                if file_path and not file_path.endswith("/"):
                    files_to_index.append(file_path)

        if not files_to_index:
            return {}

        # Prepare documents for bulk indexing
        documents: list[tuple[str, str, str]] = []

        def _prepare_documents_sync() -> list[tuple[str, str, str]]:
            """Synchronous helper to prepare documents inside a session."""
            docs: list[tuple[str, str, str]] = []
            with self.metadata.SessionLocal() as session:
                for file_path in files_to_index:
                    try:
                        # Get content
                        content = self.metadata.get_searchable_text(file_path)
                        if content is None:
                            content_raw = self.read(file_path)
                            if isinstance(content_raw, bytes):
                                content = content_raw.decode("utf-8", errors="ignore")
                            else:
                                content = str(content_raw)

                        # Get path_id
                        stmt = select(FilePathModel).where(
                            FilePathModel.virtual_path == file_path,
                            FilePathModel.deleted_at.is_(None),
                        )
                        result = session.execute(stmt)
                        file_model = result.scalar_one_or_none()

                        if file_model and content:
                            docs.append((file_path, content, file_model.path_id))
                    except Exception as e:
                        import logging

                        logging.getLogger(__name__).warning(
                            f"Failed to prepare {file_path} for indexing: {e}"
                        )
            return docs

        documents = await asyncio.to_thread(_prepare_documents_sync)

        if not documents:
            return {}

        # Bulk index using async backend
        assert self._async_search is not None
        return await self._async_search.index_documents_bulk(documents)

    @rpc_expose(description="Get semantic search indexing statistics")
    async def semantic_search_stats(self) -> dict[str, Any]:
        """
        Get semantic search indexing statistics.

        Returns:
            Dictionary with statistics:
            - total_chunks: Total number of indexed chunks
            - indexed_files: Number of indexed files
            - collection_name: Name of the vector collection
            - embedding_model: Name of the embedding model
            - chunk_size: Chunk size in tokens
            - chunk_strategy: Chunking strategy

        Examples:
            stats = await nx.semantic_search_stats()
            print(f"Indexed {stats['indexed_files']} files with {stats['total_chunks']} chunks")

        Raises:
            ValueError: If semantic search is not initialized
        """
        if not hasattr(self, "_semantic_search") or self._semantic_search is None:
            raise ValueError(
                "Semantic search is not initialized. "
                "Initialize with: await nx.initialize_semantic_search()"
            )

        return await self._semantic_search.get_index_stats()

    @rpc_expose(description="Initialize semantic search engine")
    async def initialize_semantic_search(
        self,
        embedding_provider: str | None = None,
        embedding_model: str | None = None,
        api_key: str | None = None,
        chunk_size: int = 1024,
        chunk_strategy: str = "semantic",
        async_mode: bool = True,
    ) -> None:
        """
        Initialize semantic search engine.

        This method must be called before using semantic search features.
        Uses existing database (SQLite/PostgreSQL) with native vector extensions.

        Args:
            embedding_provider: Provider name ("openai", "voyage", "voyage-lite", "fastembed")
                               or None for keyword-only
            embedding_model: Model name (uses provider default if None)
            api_key: API key for the embedding provider (if using remote provider)
            chunk_size: Chunk size in tokens (default: 1024)
            chunk_strategy: Chunking strategy ("fixed", "semantic", "overlapping")
            async_mode: Use async DB operations for high throughput (default: True)

        Examples:
            # Keyword-only search (no embeddings, no extra dependencies)
            await nx.initialize_semantic_search()

            # Semantic search with OpenAI (recommended, lightweight, requires API key)
            await nx.initialize_semantic_search(
                embedding_provider="openai",
                api_key="your-api-key"
            )

            # Semantic search with Voyage AI (fast, cost-effective)
            await nx.initialize_semantic_search(
                embedding_provider="voyage",  # or "voyage-lite" for fastest
                api_key="your-api-key"
            )

            # Local embeddings (no API, free)
            await nx.initialize_semantic_search(
                embedding_provider="fastembed"
            )

            # Custom chunk size
            await nx.initialize_semantic_search(
                chunk_size=2048,
                chunk_strategy="overlapping"
            )
        """
        from nexus.search.chunking import ChunkStrategy

        # Create embedding provider (optional)
        emb_provider = None
        if embedding_provider:
            from nexus.search.embeddings import create_embedding_provider

            emb_provider = create_embedding_provider(
                provider=embedding_provider, model=embedding_model, api_key=api_key
            )

        # Map string to enum
        strategy_map = {
            "fixed": ChunkStrategy.FIXED,
            "semantic": ChunkStrategy.SEMANTIC,
            "overlapping": ChunkStrategy.OVERLAPPING,
        }
        chunk_strat = strategy_map.get(chunk_strategy, ChunkStrategy.SEMANTIC)

        # Get database URL from metadata store
        database_url = str(self.metadata.engine.url)

        if async_mode:
            # Use async search for high-throughput (non-blocking DB operations)
            from nexus.search.async_search import AsyncSemanticSearch

            self._async_search = AsyncSemanticSearch(
                database_url=database_url,
                embedding_provider=emb_provider,
                chunk_size=chunk_size,
                chunk_strategy=chunk_strat,
            )
            await self._async_search.initialize()

            # Also create sync search for backward compatibility
            from nexus.search.semantic import SemanticSearch

            self._semantic_search = SemanticSearch(
                nx=self,  # type: ignore[arg-type]
                embedding_provider=emb_provider,
                chunk_size=chunk_size,
                chunk_strategy=chunk_strat,
            )
            self._semantic_search.initialize()
        else:
            # Use sync search (original behavior)
            from nexus.search.semantic import SemanticSearch

            self._semantic_search = SemanticSearch(
                nx=self,  # type: ignore[arg-type]
                embedding_provider=emb_provider,
                chunk_size=chunk_size,
                chunk_strategy=chunk_strat,
            )
            self._semantic_search.initialize()
            self._async_search = None

    def _list_memory_path(
        self, path: str, details: bool = False
    ) -> builtins.list[str] | builtins.list[dict[str, Any]]:
        """List memories via virtual path (Phase 2 Integration v0.4.0).

        Args:
            path: Memory virtual path.
            details: If True, return detailed metadata.

        Returns:
            List of memory paths or metadata dicts.
        """
        from nexus.core.entity_registry import EntityRegistry
        from nexus.core.memory_router import MemoryViewRouter

        # Parse path to extract filters
        parts = [p for p in path.split("/") if p]

        # Extract entity IDs from path
        session = self.metadata.SessionLocal()
        try:
            registry = EntityRegistry(session)
            router = MemoryViewRouter(session, registry)

            # Extract IDs using entity registry
            ids = registry.extract_ids_from_path_parts(parts)

            # Query memories
            memories = router.query_memories(
                tenant_id=ids.get("tenant_id"),
                user_id=ids.get("user_id"),
                agent_id=ids.get("agent_id"),
            )

            if details:
                # Return detailed metadata
                detail_results: builtins.list[dict[str, Any]] = []
                for mem in memories:
                    # Use first virtual path as canonical
                    paths = router.get_virtual_paths(mem)
                    mem_path = paths[0] if paths else f"/objs/memory/{mem.memory_id}"

                    detail_results.append(
                        {
                            "path": mem_path,
                            "size": len(self.backend.read_content(mem.content_hash)),  # type: ignore[attr-defined]
                            "modified_at": mem.created_at,
                            "etag": mem.content_hash,
                        }
                    )
                return detail_results
            else:
                # Return paths only
                path_results: builtins.list[str] = []
                for mem in memories:
                    paths = router.get_virtual_paths(mem)
                    # Return the most relevant path based on query
                    if paths:
                        path_results.append(paths[0])
                return path_results

        finally:
            session.close()
