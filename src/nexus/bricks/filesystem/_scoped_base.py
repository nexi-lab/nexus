"""Shared path-scoping logic for ScopedFilesystem.

Phase 6.1 (Issue #2033): DRY extraction of path rewriting helpers.

Moved from core/ → services/filesystem/ → bricks/filesystem/ (Issue #2424).

Usage:
    class ScopedFilesystem(ScopedPathMixin):
        def __init__(self, fs, root):
            super().__init__(root)
            self._fs = fs
"""

import builtins
from typing import Any

# Global namespaces that bypass scoping — shared resources with their own
# ownership/permission structures.  Defined once, used by both variants.
GLOBAL_NAMESPACES: tuple[str, ...] = (
    "/skills/",
    "/__sys__/",
    "/mnt/",
    "/memory/",
    "/objs/",
)


class ScopedPathMixin:
    """Mixin that provides path scoping (rebase + un-rebase) helpers.

    Subclasses only need to call ``super().__init__(root)`` and then use
    ``_scope_path`` / ``_unscope_path`` everywhere.
    """

    __slots__ = ("_root",)

    def __init__(self, root: str) -> None:
        self._root: str = "/" + root.strip("/") if root.strip("/") else ""

    # ------------------------------------------------------------------
    # Path rewriting
    # ------------------------------------------------------------------

    def _scope_path(self, path: str) -> str:
        """Prepend the root prefix (skip global namespaces)."""
        if not path.startswith("/"):
            path = "/" + path
        for ns in GLOBAL_NAMESPACES:
            if path.startswith(ns):
                return path
        return f"{self._root}{path}"

    def _unscope_path(self, path: str) -> str:
        """Strip the root prefix (skip global namespaces)."""
        for ns in GLOBAL_NAMESPACES:
            if path.startswith(ns):
                return path
        if self._root and path.startswith(self._root):
            result = path[len(self._root) :]
            return result if result else "/"
        return path

    def _unscope_paths(self, paths: builtins.list[str]) -> builtins.list[str]:
        return [self._unscope_path(p) for p in paths]

    def _unscope_dict(self, d: dict[str, Any], path_keys: builtins.list[str]) -> dict[str, Any]:
        result = d.copy()
        for key in path_keys:
            if key in result and isinstance(result[key], str):
                result[key] = self._unscope_path(result[key])
        return result

    @property
    def root(self) -> str:
        return self._root
