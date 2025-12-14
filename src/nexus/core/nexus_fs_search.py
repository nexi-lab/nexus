"""Search operations for NexusFS.

This module contains file search and listing operations:
- list: List files in a directory
- glob: Find files matching glob patterns
- grep: Search file contents using regex
- semantic_search: Search files using semantic similarity
"""

from __future__ import annotations

import builtins
import fnmatch
import re
from typing import TYPE_CHECKING, Any, cast

from nexus.core import glob_fast, grep_fast
from nexus.core.exceptions import PermissionDeniedError
from nexus.core.permissions import Permission
from nexus.core.rpc_decorator import rpc_expose

if TYPE_CHECKING:
    from nexus.core.permissions import OperationContext
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
        _rebac_manager: EnhancedReBACManager

        def _validate_path(self, path: str) -> str: ...
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

    @rpc_expose(description="List files in directory")
    def list(
        self,
        path: str = "/",
        recursive: bool = True,
        details: bool = False,
        prefix: str | None = None,
        show_parsed: bool = True,  # noqa: ARG002
        context: OperationContext | None = None,
    ) -> builtins.list[str] | builtins.list[dict[str, Any]]:
        """
        List files in a directory.

        Supports memory virtual paths since v0.4.0.

        Args:
            path: Directory path to list (default: "/", supports memory paths)
            recursive: If True, list all files recursively; if False, list only direct children (default: True)
            details: If True, return detailed metadata; if False, return paths only (default: False)
            prefix: (Deprecated) Path prefix to filter by - for backward compatibility.
                    When used, lists all files recursively with this prefix.
            show_parsed: If True, include parsed virtual views in listing (default: True).
                        Note: Virtual views are added at the RPC layer, not in this method.
            context: Optional operation context for permission filtering (uses default if not provided)

        Returns:
            List of file paths (if details=False) or list of file metadata dicts (if details=True).
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
        """
        import logging

        logger = logging.getLogger(__name__)
        logger.debug(
            f"[LIST-DEBUG-START] list() called with path={path}, recursive={recursive}, details={details}"
        )

        # Phase 2 Integration (v0.4.0): Intercept memory paths
        from nexus.core.memory_router import MemoryViewRouter

        if path and MemoryViewRouter.is_memory_path(path):
            return self._list_memory_path(path, details)

        # Check if path routes to a dynamic API-backed connector (e.g., x_connector)
        # These connectors have virtual directories that don't exist in metadata
        if path and path != "/":
            logger.debug(f"[LIST-DEBUG] Entering dynamic connector check for path={path}")
            try:
                tenant_id, agent_id, is_admin = self._get_routing_params(context)
                logger.debug(
                    f"[LIST-DEBUG] routing_params: tenant_id={tenant_id}, is_admin={is_admin}"
                )
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
                            # Use rebac_check with correct signature: (subject_tuple, permission, object_tuple, context, tenant_id)
                            has_permission = self._rebac_manager.rebac_check(
                                subject=(context.subject_type, context.subject_id),
                                permission="read",
                                object=("file", mount_path),
                                tenant_id=context.tenant_id,
                            )
                        if not has_permission:
                            raise PermissionDeniedError(
                                f"Access denied: User '{context.user}' does not have READ permission for '{path}'"
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
                                    # Add the directory itself
                                    results.append(full_path)
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
                                    # Non-recursive - just add the directory
                                    results.append(full_path)
                            else:
                                # File - add it
                                results.append(full_path)

                        return results

                    # List directory contents (with recursion if requested)
                    all_paths = list_recursive(path, route.backend_path)

                    # Format results
                    if details:
                        from datetime import datetime

                        # Get actual sizes from file_paths table
                        results_with_details = []
                        for entry_path in all_paths:
                            # Try to get size from metadata (file_paths table)
                            file_meta = self.metadata.get(entry_path)
                            file_size = (
                                file_meta.size if file_meta and hasattr(file_meta, "size") else 0
                            )

                            results_with_details.append(
                                {
                                    "path": entry_path,
                                    "size": file_size,
                                    "modified_at": datetime.now().isoformat(),
                                    "etag": "",
                                }
                            )
                        return results_with_details
                    return all_paths
            except PermissionDeniedError:
                # Re-raise permission errors - don't fall through to metadata listing
                raise
            except Exception as e:
                import traceback

                logger.warning(
                    f"[LIST-DEBUG] Dynamic connector list_dir failed for {path}: {e}\n{traceback.format_exc()}"
                )
                # Fall through to normal metadata-based listing

        # Handle backward compatibility with old 'prefix' parameter
        if prefix is not None:
            # Old API: list(prefix="/path") - always recursive
            if prefix:
                prefix = self._validate_path(prefix)
            all_files = self.metadata.list(prefix)
            results = all_files
        else:
            # New API: list(path="/", recursive=False)
            if path and path != "/":
                path = self._validate_path(path)

            # Ensure path ends with / for directory listing
            if path and not path.endswith("/"):
                path = path + "/"

            # Get all files with this prefix
            all_files = self.metadata.list(path if path != "/" else "")

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

        # Filter by read permission (v0.3.0)
        if self._enforce_permissions:
            import logging
            import time

            logger = logging.getLogger(__name__)

            perm_start = time.time()
            from nexus.core.permissions import OperationContext

            ctx_raw = context or self._default_context
            assert isinstance(ctx_raw, OperationContext), "Context must be OperationContext"
            ctx: OperationContext = ctx_raw
            result_paths = [meta.path for meta in results]

            logger.warning(
                f"[PERF-LIST] Starting permission filter for {len(result_paths)} paths, path={path}, recursive={recursive}"
            )

            filter_start = time.time()
            allowed_paths = self._permission_enforcer.filter_list(result_paths, ctx)
            filter_elapsed = time.time() - filter_start

            logger.warning(
                f"[PERF-LIST] Permission filter completed in {filter_elapsed:.3f}s, allowed {len(allowed_paths)}/{len(result_paths)} paths"
            )

            # Filter results to only include allowed paths
            results = [meta for meta in results if meta.path in allowed_paths]

            perm_total = time.time() - perm_start
            logger.warning(f"[PERF-LIST] Total permission filtering: {perm_total:.3f}s")

        # Sort by path name
        results.sort(key=lambda m: m.path)

        # Add directories to results (infer from file paths + check backend)
        # This ensures empty directories show up in listings
        directories = set()

        # Extract directories from directory marker files in results (v0.3.9+)
        # These are files with mime_type="inode/directory" created by mkdir
        for meta in results:
            if meta.mime_type == "inode/directory":
                directories.add(meta.path)

        if not recursive:
            # For non-recursive listings, infer immediate subdirectories from file paths
            base_path = path if path != "/" else ""

            # OPTIMIZATION (issue #380): Reuse metadata query and avoid double permission check
            # Get all files to infer directories
            all_files_for_dirs = self.metadata.list(base_path)

            # Filter files by permission before inferring directories
            # NOTE: We need to filter ALL files (not just results), because results may be filtered
            # to only include files in the current directory (non-recursive), but we need to see
            # files in subdirectories to infer those subdirectories exist
            if self._enforce_permissions:
                from nexus.core.permissions import OperationContext

                ctx_raw_glob = context or self._default_context
                assert isinstance(ctx_raw_glob, OperationContext), (
                    "Context must be OperationContext"
                )
                ctx_glob: OperationContext = ctx_raw_glob

                # Check if we already have filtered results we can reuse
                # If base_path matches our query path, we can use the already-filtered results
                # plus any additional files found in subdirectories
                if base_path == path or (path == "/" and base_path == ""):
                    # Create a set of already-checked paths to avoid duplicate filtering
                    already_filtered = {meta.path for meta in results}

                    # Only filter paths we haven't already checked
                    unfiltered_paths = [
                        meta.path
                        for meta in all_files_for_dirs
                        if meta.path not in already_filtered
                    ]

                    if unfiltered_paths:
                        # Filter only the new paths
                        allowed_new_paths = self._permission_enforcer.filter_list(
                            unfiltered_paths, ctx_glob
                        )
                        allowed_new_paths_set = set(allowed_new_paths)

                        # Combine already-filtered results with newly-filtered paths
                        all_files_for_dirs = [
                            meta
                            for meta in all_files_for_dirs
                            if meta.path in already_filtered or meta.path in allowed_new_paths_set
                        ]
                    else:
                        # All paths already filtered, reuse results
                        all_files_for_dirs = results
                else:
                    # Different path, need to filter everything
                    all_paths_for_dirs = [meta.path for meta in all_files_for_dirs]
                    allowed_paths_for_dirs = self._permission_enforcer.filter_list(
                        all_paths_for_dirs, ctx_glob
                    )
                    all_files_for_dirs = [
                        meta for meta in all_files_for_dirs if meta.path in allowed_paths_for_dirs
                    ]

            for meta in all_files_for_dirs:
                # Get relative path
                rel_path = meta.path[len(path) :] if path != "/" else meta.path[1:]
                # Check if there's a directory component
                if "/" in rel_path:
                    # Extract first directory component
                    dir_name = rel_path.split("/")[0]
                    dir_path = path + dir_name if path != "/" else "/" + dir_name
                    directories.add(dir_path)

            # Check backend for empty directories (directories with no files)
            # This catches newly created directories using the helper method
            if not self._enforce_permissions:
                # No permissions: add all backend directories
                backend_dirs = self._get_backend_directory_entries(path)
                directories.update(backend_dirs)
            else:
                # With permissions: only show directories if user has access to them OR any descendant
                backend_dirs = self._get_backend_directory_entries(path)
                ctx = context or self._default_context

                # OPTIMIZATION (issue #380): Use bulk check for all backend directories at once
                # Instead of N separate _has_descendant_access() calls (N bulk queries),
                # use _has_descendant_access_bulk() for ONE bulk query
                if hasattr(self, "_has_descendant_access_bulk") and len(backend_dirs) > 1:
                    # Bulk check all backend directories at once
                    access_results = self._has_descendant_access_bulk(
                        list(backend_dirs), Permission.READ, ctx
                    )
                    for dir_path, has_access in access_results.items():
                        if has_access:
                            directories.add(dir_path)
                else:
                    # Fallback to individual checks (for single directory or if method not available)
                    for dir_path in backend_dirs:
                        # Check if user has access to this directory or any of its descendants
                        if self._has_descendant_access(dir_path, Permission.READ, ctx):  # type: ignore[attr-defined]
                            directories.add(dir_path)

        if details:
            # Filter out directory metadata markers to avoid duplicates
            # Directories are already included in dir_results below
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
            all_results = file_results + dir_results
            all_results.sort(key=lambda x: str(x["path"]))
            logger.debug(
                f"[LIST-DEBUG] Returning {len(all_results)} results (details=True), path={path}, recursive={recursive}"
            )
            return all_results
        else:
            # Return paths only (filter out directory metadata markers)
            file_paths = [meta.path for meta in results if meta.mime_type != "inode/directory"]
            all_paths = file_paths + sorted(directories)
            all_paths.sort()
            logger.debug(
                f"[LIST-DEBUG] Returning {len(all_paths)} paths (details=False), path={path}, recursive={recursive}, file_paths={len(file_paths)}, directories={len(directories)}"
            )
            return all_paths

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

        # SECURITY: Filter files by ReBAC permissions FIRST
        # This ensures users only see files they have access to
        accessible_files: list[str] = cast(
            list[str], self.list(path, recursive=True, context=context)
        )

        # Build full pattern
        if not path.endswith("/"):
            path = path + "/"
        if path == "/":
            full_pattern = pattern
            # Auto-prepend **/ for patterns that look relative
            # (don't start with known namespaces and don't already have **)
            # and have path separators (e.g., "src/*.py" vs "*.py")
            if (
                "**" not in full_pattern
                and not full_pattern.startswith(("workspace/", "shared/", "external/"))
                and "/" in full_pattern
            ):
                full_pattern = "**/" + full_pattern
        else:
            # Remove leading / from path for pattern matching
            base_path = path[1:] if path.startswith("/") else path
            full_pattern = base_path + pattern

        # Try Rust acceleration first (10-20x faster)
        # Adjust pattern for Rust: paths have leading /, so pattern should too
        rust_pattern = full_pattern if full_pattern.startswith("/") else "/" + full_pattern
        rust_matches = glob_fast.glob_match_bulk([rust_pattern], accessible_files)
        if rust_matches is not None:
            return sorted(rust_matches)

        # Fallback to Python implementation
        # Match accessible files against pattern
        # Handle ** for recursive matching
        if "**" in full_pattern:
            # Convert glob pattern to regex
            # Split by ** to handle recursive matching
            parts = full_pattern.split("**")

            regex_parts = []
            for i, part in enumerate(parts):
                if i > 0:
                    # ** matches zero or more path segments
                    # This can be empty or ".../", so use (?:.*/)? for optional match
                    regex_parts.append("(?:.*/)?")

                # Escape and convert wildcards in this part
                escaped = re.escape(part)
                escaped = escaped.replace(r"\*", "[^/]*")
                escaped = escaped.replace(r"\?", ".")
                escaped = escaped.replace(r"\[", "[").replace(r"\]", "]")

                # Remove leading / from all parts since it's handled by ** or the anchor
                # Note: re.escape() doesn't escape /, so we check for it directly
                while escaped.startswith("/"):
                    escaped = escaped[1:]

                regex_parts.append(escaped)

            regex_pattern = "^/" + "".join(regex_parts) + "$"

            matches = []
            for file_path in accessible_files:
                if re.match(regex_pattern, file_path):
                    matches.append(file_path)
        else:
            # Use fnmatch for simpler patterns
            matches = []
            for file_path in accessible_files:
                # Remove leading / for matching
                path_for_match = file_path[1:] if file_path.startswith("/") else file_path
                if fnmatch.fnmatch(path_for_match, full_pattern):
                    matches.append(file_path)

        return sorted(matches)

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

        import logging
        import time

        logger = logging.getLogger(__name__)
        grep_start = time.time()

        # Get files to search
        list_start = time.time()
        if file_pattern:
            files = self.glob(file_pattern, path, context=context)
        else:
            # Get all files under path (with ReBAC filtering)
            files = cast(list[str], self.list(path, recursive=True, context=context))
        list_elapsed = time.time() - list_start
        logger.debug(f"[GREP] Phase 1: list() found {len(files)} files in {list_elapsed:.3f}s")

        # Phase 2: Bulk fetch searchable text (from content_cache or file_metadata)
        text_start = time.time()
        searchable_texts = self.metadata.get_searchable_text_bulk(files)
        text_elapsed = time.time() - text_start
        logger.debug(
            f"[GREP] Phase 2: get_searchable_text_bulk() returned {len(searchable_texts)} texts in {text_elapsed:.3f}s"
        )

        # Files that need raw content fallback
        files_needing_raw = [f for f in files if f not in searchable_texts]

        # Phase 3: Search cached texts
        search_start = time.time()
        results: list[dict[str, Any]] = []

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
        logger.debug(f"[GREP] Phase 3: Searched cached texts in {search_elapsed:.3f}s")

        # Phase 4: Fall back to raw content for files without cached text
        if len(results) < max_results and files_needing_raw:
            raw_start = time.time()
            remaining_results = max_results - len(results)

            # Try Rust-accelerated grep if available
            if grep_fast.is_available():
                bulk_results = self.read_bulk(files_needing_raw, context=context, skip_errors=True)
                file_contents: dict[str, bytes] = {
                    fp: content
                    for fp, content in bulk_results.items()
                    if content is not None and isinstance(content, bytes)
                }

                rust_results = grep_fast.grep_bulk(
                    pattern, file_contents, ignore_case=ignore_case, max_results=remaining_results
                )

                if rust_results is not None:
                    results.extend(rust_results)
            else:
                # Python fallback for raw content
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
                f"[GREP] Phase 4: Raw content fallback for {len(files_needing_raw)} files in {raw_elapsed:.3f}s"
            )

        total_elapsed = time.time() - grep_start
        logger.debug(f"[GREP] TOTAL: {total_elapsed:.3f}s, {len(results)} results")

        return results

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

        results = await self._semantic_search.search(
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
            }
            for result in results
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

        # Check if path is a file or directory
        try:
            # Try to read as file
            self.read(path)
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
            files = self.list(path, recursive=False)
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
    ) -> None:
        """
        Initialize semantic search engine.

        This method must be called before using semantic search features.
        Uses existing database (SQLite/PostgreSQL) with native vector extensions.

        Args:
            embedding_provider: Provider name ("openai", "voyage") or None for keyword-only
            embedding_model: Model name (uses provider default if None)
            api_key: API key for the embedding provider (if using remote provider)
            chunk_size: Chunk size in tokens (default: 1024)
            chunk_strategy: Chunking strategy ("fixed", "semantic", "overlapping")

        Examples:
            # Keyword-only search (no embeddings, no extra dependencies)
            await nx.initialize_semantic_search()

            # Semantic search with OpenAI (recommended, lightweight, requires API key)
            await nx.initialize_semantic_search(
                embedding_provider="openai",
                api_key="your-api-key"
            )

            # Semantic search with Voyage AI (specialized embeddings)
            await nx.initialize_semantic_search(
                embedding_provider="voyage",
                api_key="your-api-key"
            )

            # Custom chunk size
            await nx.initialize_semantic_search(
                chunk_size=2048,
                chunk_strategy="overlapping"
            )
        """
        from nexus.search.chunking import ChunkStrategy
        from nexus.search.semantic import SemanticSearch

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

        # Create semantic search instance (uses existing database)
        self._semantic_search = SemanticSearch(
            nx=self,  # type: ignore[arg-type]
            embedding_provider=emb_provider,
            chunk_size=chunk_size,
            chunk_strategy=chunk_strat,
        )

        # Initialize vector extensions and FTS tables
        self._semantic_search.initialize()

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
