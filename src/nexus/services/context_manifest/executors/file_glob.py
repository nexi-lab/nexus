"""FileGlobExecutor — resolve file glob patterns against a workspace root (Issue #1427).

Security:
    - All resolved paths validated to stay under workspace_root.
    - Absolute patterns and '..' traversal rejected before globbing.
    - Symlinks pointing outside workspace_root are excluded.

Performance:
    - Two-phase approach: glob paths → cap at max_files → read contents.
    - Paths sorted by mtime (newest first) before capping.
    - Blocking I/O runs in thread pool to avoid blocking the event loop.
"""

from __future__ import annotations

import asyncio
import glob as glob_module
import logging
import os
import re
import time
from pathlib import Path
from typing import Any

from nexus.services.context_manifest.models import ContextSourceProtocol, SourceResult
from nexus.services.context_manifest.template import resolve_template

logger = logging.getLogger(__name__)


class FileGlobExecutor:
    """Execute file_glob sources by resolving glob patterns against workspace root.

    Args:
        workspace_root: Root directory that glob patterns are evaluated against.
            All resolved paths must stay under this directory.
    """

    def __init__(self, workspace_root: Path) -> None:
        self._workspace_root = workspace_root.resolve()

    async def execute(
        self,
        source: ContextSourceProtocol,
        variables: dict[str, str],
    ) -> SourceResult:
        """Resolve a file_glob source by globbing and reading matching files.

        Delegates to thread pool to avoid blocking the event loop with
        synchronous filesystem I/O (glob, stat, read).

        Args:
            source: A FileGlobSource instance (accessed via protocol).
            variables: Template variables for pattern substitution.

        Returns:
            SourceResult with file contents dict, or error on validation failure.
        """
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._execute_sync, source, variables)

    def _execute_sync(
        self,
        source: ContextSourceProtocol,
        variables: dict[str, str],
    ) -> SourceResult:
        """Synchronous implementation of file glob resolution."""
        start = time.monotonic()
        source_type = source.type
        source_name = source.source_name

        # Extract pattern and max_files from source
        pattern: str = getattr(source, "pattern", "")
        max_files: int = getattr(source, "max_files", 50)

        # Resolve template variables in pattern
        if "{{" in pattern:
            try:
                pattern = resolve_template(pattern, variables)
            except ValueError as exc:
                elapsed_ms = (time.monotonic() - start) * 1000
                return SourceResult.error(
                    source_type=source_type,
                    source_name=source_name,
                    error_message=f"Template resolution failed: {exc}",
                    elapsed_ms=elapsed_ms,
                )

        # Security: reject absolute paths and '..' traversal
        # Check both native and POSIX absolute (/ prefix) for cross-platform safety
        if os.path.isabs(pattern) or pattern.startswith("/"):
            elapsed_ms = (time.monotonic() - start) * 1000
            return SourceResult.error(
                source_type=source_type,
                source_name=source_name,
                error_message="Absolute paths are not allowed in glob patterns",
                elapsed_ms=elapsed_ms,
            )

        # Split on both / and \ to catch cross-platform traversal
        if ".." in re.split(r"[/\\]", pattern):
            elapsed_ms = (time.monotonic() - start) * 1000
            return SourceResult.error(
                source_type=source_type,
                source_name=source_name,
                error_message="Path traversal ('..') is not allowed in glob patterns",
                elapsed_ms=elapsed_ms,
            )

        # Validate workspace root exists
        if not self._workspace_root.is_dir():
            elapsed_ms = (time.monotonic() - start) * 1000
            return SourceResult.error(
                source_type=source_type,
                source_name=source_name,
                error_message=f"Workspace root does not exist: {self._workspace_root}",
                elapsed_ms=elapsed_ms,
            )

        # Phase 1: Glob for paths
        full_pattern = str(self._workspace_root / pattern)
        matched_paths = glob_module.glob(full_pattern, recursive=True)

        # Filter: only regular files, validate each path is under workspace_root
        safe_paths: list[Path] = []
        for p_str in matched_paths:
            p = Path(p_str)
            try:
                resolved = p.resolve()
            except OSError:
                continue  # skip unresolvable paths

            # Security: must be under workspace_root
            if not _is_under(resolved, self._workspace_root):
                logger.debug("Excluding path outside workspace root: %s", resolved)
                continue

            # Skip directories, only include regular files
            if not resolved.is_file():
                continue

            safe_paths.append(resolved)

        total_matched = len(safe_paths)

        # Sort by mtime (newest first) for deterministic cap selection.
        # Use safe accessor to handle TOCTOU race (file deleted between check and sort).
        safe_paths.sort(key=_safe_mtime, reverse=True)

        # Phase 2: Cap at max_files
        capped_paths = safe_paths[:max_files]

        # Read file contents
        files: dict[str, str] = {}
        for fp in capped_paths:
            rel = fp.relative_to(self._workspace_root)
            try:
                files[str(rel)] = fp.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                logger.debug("Could not read %s: %s", fp, exc)
                continue

        elapsed_ms = (time.monotonic() - start) * 1000

        metadata: dict[str, Any] = {
            "files": files,
            "total_matched": total_matched,
            "returned": len(files),
        }

        return SourceResult.ok(
            source_type=source_type,
            source_name=source_name,
            data=metadata,
            elapsed_ms=elapsed_ms,
        )


def _safe_mtime(p: Path) -> float:
    """Get file mtime safely, returning 0.0 if file was deleted (TOCTOU)."""
    try:
        return p.stat().st_mtime
    except OSError:
        return 0.0


def _is_under(path: Path, root: Path) -> bool:
    """Check whether *path* is under *root* (resolved, no symlink escape)."""
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False
