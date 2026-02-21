"""Shared utilities for context manifest source executors.

Provides:
- ``resolve_source_template``: DRY template resolution with standardized error handling
- Per-executor source protocols for type-safe field access

References:
    - Issue #1428: Review fix — DRY (5A), typed protocols (6A)
"""

import time
from typing import Protocol, runtime_checkable

from nexus.bricks.context_manifest.models import ContextSourceProtocol, SourceResult
from nexus.bricks.context_manifest.template import resolve_template

# ---------------------------------------------------------------------------
# Per-executor source protocols (6A)
# ---------------------------------------------------------------------------


@runtime_checkable
class FileGlobSourceProtocol(ContextSourceProtocol, Protocol):
    """Typed protocol for file_glob sources."""

    @property
    def pattern(self) -> str: ...

    @property
    def max_files(self) -> int: ...


@runtime_checkable
class MemoryQuerySourceProtocol(ContextSourceProtocol, Protocol):
    """Typed protocol for memory_query sources."""

    @property
    def query(self) -> str: ...

    @property
    def top_k(self) -> int: ...


@runtime_checkable
class WorkspaceSnapshotSourceProtocol(ContextSourceProtocol, Protocol):
    """Typed protocol for workspace_snapshot sources."""

    @property
    def snapshot_id(self) -> str: ...


# ---------------------------------------------------------------------------
# DRY template resolution helper (5A)
# ---------------------------------------------------------------------------


def resolve_source_template(
    field_value: str,
    variables: dict[str, str],
    source: ContextSourceProtocol,
    start: float,
) -> tuple[str, SourceResult | None]:
    """Resolve template variables in a source field value.

    Returns a tuple of (resolved_value, error_result). If resolution
    succeeds, error_result is None. If it fails, error_result contains
    the SourceResult.error() to return immediately.

    Args:
        field_value: The field string that may contain ``{{variable}}`` placeholders.
        variables: Template variable values for substitution.
        source: The source being executed (for type/name metadata).
        start: ``time.monotonic()`` value from execution start (for elapsed_ms).

    Returns:
        Tuple of (resolved_value, None) on success, or
        (original_value, SourceResult.error) on failure.

    Example::

        resolved, err = resolve_source_template(query, variables, source, start)
        if err is not None:
            return err
        # use resolved value
    """
    if "{{" not in field_value:
        return field_value, None

    try:
        resolved = resolve_template(field_value, variables)
        return resolved, None
    except ValueError as exc:
        elapsed_ms = (time.monotonic() - start) * 1000
        return field_value, SourceResult.error(
            source_type=source.type,
            source_name=source.source_name,
            error_message=f"Template resolution failed: {exc}",
            elapsed_ms=elapsed_ms,
        )
