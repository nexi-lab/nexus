"""Search operations for NexusFS.

This module contains file search and listing operations:
- list: List files in a directory
- glob: Find files matching glob patterns
- grep: Search file contents using regex
"""

from __future__ import annotations

import builtins
import fnmatch
import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nexus.core.permissions import OperationContext, PermissionEnforcer
    from nexus.storage.metadata_store import SQLAlchemyMetadataStore


class NexusFSSearchMixin:
    """Mixin providing search operations for NexusFS."""

    # Type hints for attributes that will be provided by NexusFS parent class
    if TYPE_CHECKING:
        metadata: SQLAlchemyMetadataStore
        _enforce_permissions: bool
        _default_context: OperationContext
        _permission_enforcer: PermissionEnforcer

        def _validate_path(self, path: str) -> str: ...
        def _get_backend_directory_entries(self, path: str) -> set[str]: ...
        def read(
            self, path: str, context: OperationContext | None = None, return_metadata: bool = False
        ) -> bytes | dict[str, Any]: ...

    def list(
        self,
        path: str = "/",
        recursive: bool = True,
        details: bool = False,
        prefix: str | None = None,
        context: OperationContext | None = None,
    ) -> builtins.list[str] | builtins.list[dict[str, Any]]:
        """
        List files in a directory.

        Args:
            path: Directory path to list (default: "/")
            recursive: If True, list all files recursively; if False, list only direct children (default: True)
            details: If True, return detailed metadata; if False, return paths only (default: False)
            prefix: (Deprecated) Path prefix to filter by - for backward compatibility.
                    When used, lists all files recursively with this prefix.
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
        """
        # Handle backward compatibility with old 'prefix' parameter
        if prefix is not None:
            # Old API: list(prefix="/path") - always recursive
            if prefix:
                prefix = self._validate_path(prefix)
            all_files = self.metadata.list(prefix)
            results = all_files
        else:
            # New API: list(path="/", recursive=False)
            if path:
                path = self._validate_path(path)

            # Ensure path ends with / for directory listing
            if not path.endswith("/"):
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
            ctx = context or self._default_context
            result_paths = [meta.path for meta in results]
            allowed_paths = self._permission_enforcer.filter_list(result_paths, ctx)
            # Filter results to only include allowed paths
            results = [meta for meta in results if meta.path in allowed_paths]

        # Sort by path name
        results.sort(key=lambda m: m.path)

        # Add directories to results (infer from file paths + check backend)
        # This ensures empty directories show up in listings
        directories = set()

        if not recursive:
            # For non-recursive listings, infer immediate subdirectories from file paths
            base_path = path if path != "/" else ""

            # Get all files to infer directories
            all_files_for_dirs = self.metadata.list(base_path)
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
            backend_dirs = self._get_backend_directory_entries(path)
            directories.update(backend_dirs)

        if details:
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
            return all_results
        else:
            # Return paths only
            all_paths = [meta.path for meta in results] + sorted(directories)
            all_paths.sort()
            return all_paths

    def glob(self, pattern: str, path: str = "/") -> builtins.list[str]:
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
        if path:
            path = self._validate_path(path)

        # Get all files
        all_files = self.metadata.list("")

        # Build full pattern
        if not path.endswith("/"):
            path = path + "/"
        if path == "/":
            full_pattern = pattern
        else:
            # Remove leading / from path for pattern matching
            base_path = path[1:] if path.startswith("/") else path
            full_pattern = base_path + pattern

        # Match files against pattern
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
            for meta in all_files:
                if re.match(regex_pattern, meta.path):
                    matches.append(meta.path)
        else:
            # Use fnmatch for simpler patterns
            matches = []
            for meta in all_files:
                # Remove leading / for matching
                file_path = meta.path[1:] if meta.path.startswith("/") else meta.path
                if fnmatch.fnmatch(file_path, full_pattern):
                    matches.append(meta.path)

        return sorted(matches)

    def grep(
        self,
        pattern: str,
        path: str = "/",
        file_pattern: str | None = None,
        ignore_case: bool = False,
        max_results: int = 1000,
        search_mode: str = "auto",
    ) -> builtins.list[dict[str, Any]]:
        r"""
        Search file contents using regex patterns.

        Args:
            pattern: Regex pattern to search for in file contents
            path: Base path to search from (default: "/")
            file_pattern: Optional glob pattern to filter files (e.g., "*.py")
            ignore_case: If True, perform case-insensitive search (default: False)
            max_results: Maximum number of results to return (default: 1000)
            search_mode: Content search mode (default: "auto")
                - "auto": Try parsed text first, fallback to raw (default)
                - "parsed": Only search parsed text (skip files without parsed content)
                - "raw": Only search raw file content (skip parsing)

        Returns:
            List of match dicts, each containing:
            - file: File path
            - line: Line number (1-indexed)
            - content: Matched line content
            - match: The matched text
            - source: Source type - "parsed" or "raw"

        Examples:
            # Search for "TODO" in all files (auto mode - tries parsed first)
            fs.grep("TODO")
            # Returns: [{"file": "/main.py", "line": 42, "content": "...", "source": "raw"}, ...]

            # Search for function definitions in Python files
            fs.grep(r"def \w+", file_pattern="**/*.py")

            # Search only parsed text from PDFs
            fs.grep("revenue", file_pattern="**/*.pdf", search_mode="parsed")

            # Search only raw content (skip parsing)
            fs.grep("TODO", search_mode="raw")

            # Case-insensitive search
            fs.grep("error", ignore_case=True)
        """
        if path:
            path = self._validate_path(path)

        # Validate search_mode
        valid_modes = {"auto", "parsed", "raw"}
        if search_mode not in valid_modes:
            raise ValueError(
                f"Invalid search_mode: {search_mode}. Must be one of: {', '.join(valid_modes)}"
            )

        # Compile regex pattern
        flags = re.IGNORECASE if ignore_case else 0
        try:
            regex = re.compile(pattern, flags)
        except re.error as e:
            raise ValueError(f"Invalid regex pattern: {e}") from e

        # Get files to search
        files: list[str]
        if file_pattern:
            files = self.glob(file_pattern, path)
        else:
            # Get all files under path
            if not path.endswith("/"):
                path = path + "/"
            prefix = path if path != "/" else ""
            all_files = self.metadata.list(prefix)
            files = [meta.path for meta in all_files]

        # Search through files
        results: list[dict[str, Any]] = []
        for file_path in files:
            if len(results) >= max_results:
                break

            try:
                text: str | None = None
                source: str = "raw"

                # Get parsed text if needed
                if search_mode in ("auto", "parsed"):
                    parsed_text = self.metadata.get_file_metadata(file_path, "parsed_text")
                    if parsed_text:
                        text = parsed_text
                        source = "parsed"

                # Get raw text if needed
                if text is None and search_mode in ("auto", "raw"):
                    # Read raw content
                    content = self.read(file_path)

                    # Type narrowing: when return_metadata=False (default), result is bytes
                    assert isinstance(content, bytes), "Expected bytes from read()"

                    # Try to decode as text
                    try:
                        text = content.decode("utf-8")
                        source = "raw"
                    except UnicodeDecodeError:
                        # Skip binary files
                        continue

                # Skip if no text available
                if text is None:
                    continue

                # Search line by line
                for line_num, line in enumerate(text.splitlines(), start=1):
                    if len(results) >= max_results:
                        break

                    match = regex.search(line)
                    if match:
                        results.append(
                            {
                                "file": file_path,
                                "line": line_num,
                                "content": line,
                                "match": match.group(0),
                                "source": source,
                            }
                        )

            except Exception:
                # Skip files that can't be read
                continue

        return results
