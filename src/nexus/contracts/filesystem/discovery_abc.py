"""Discovery sub-ABC for filesystem implementations.

Extracted from core/filesystem.py (Issue #2424) following the
``collections.abc`` composition pattern.

Contains: list, glob, grep
"""

from __future__ import annotations

import builtins
from abc import ABC, abstractmethod
from typing import Any


class DiscoveryABC(ABC):
    """File discovery operations: list, glob, grep."""

    @abstractmethod
    def list(
        self,
        path: str = "/",
        recursive: bool = True,
        details: bool = False,
        show_parsed: bool = True,
        context: Any = None,
    ) -> builtins.list[str] | builtins.list[dict[str, Any]]:
        """List files in a directory.

        Args:
            path: Directory path to list (default: "/")
            recursive: If True, list all files recursively
            details: If True, return detailed metadata
            show_parsed: If True, include virtual _parsed.{ext}.md views
            context: Optional operation context

        Returns:
            List of file paths or metadata dicts
        """
        ...

    @abstractmethod
    def glob(self, pattern: str, path: str = "/", context: Any = None) -> builtins.list[str]:
        """Find files matching a glob pattern.

        Supports: ``*``, ``**``, ``?``, ``[...]``

        Args:
            pattern: Glob pattern to match
            path: Base path to search from (default: "/")
            context: Optional operation context

        Returns:
            List of matching file paths, sorted by name
        """
        ...

    @abstractmethod
    def grep(
        self,
        pattern: str,
        path: str = "/",
        file_pattern: str | None = None,
        ignore_case: bool = False,
        max_results: int = 1000,
        search_mode: str = "auto",
        context: Any = None,
    ) -> builtins.list[dict[str, Any]]:
        """Search file contents using regex patterns.

        Args:
            pattern: Regex pattern to search for
            path: Base path to search from (default: "/")
            file_pattern: Optional glob pattern to filter files
            ignore_case: If True, case-insensitive search
            max_results: Maximum number of results
            search_mode: "auto", "parsed", or "raw"
            context: Optional operation context

        Returns:
            List of match dicts (file, line, content, match, source)
        """
        ...
