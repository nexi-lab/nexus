"""Shared fixtures and factory functions for context manifest tests (Issue #1341)."""

from __future__ import annotations

from typing import Any

import pytest

from nexus.services.context_manifest.models import (
    FileGlobSource,
    MCPToolSource,
    MemoryQuerySource,
    SourceResult,
    WorkspaceSnapshotSource,
)

# ---------------------------------------------------------------------------
# Factory functions
# ---------------------------------------------------------------------------


def make_source(source_type: str = "file_glob", **overrides: Any) -> Any:
    """Create a context source model with sensible defaults.

    Args:
        source_type: One of "mcp_tool", "workspace_snapshot", "file_glob", "memory_query".
        **overrides: Fields to override on the source model.

    Returns:
        A Pydantic source model instance.
    """
    defaults: dict[str, dict[str, Any]] = {
        "mcp_tool": {"tool_name": "search_codebase"},
        "workspace_snapshot": {},
        "file_glob": {"pattern": "src/**/*.py"},
        "memory_query": {"query": "relevant context"},
    }
    fields = {**defaults[source_type], **overrides}
    model_map = {
        "mcp_tool": MCPToolSource,
        "workspace_snapshot": WorkspaceSnapshotSource,
        "file_glob": FileGlobSource,
        "memory_query": MemoryQuerySource,
    }
    return model_map[source_type](**fields)


def make_ok_result(
    source_type: str = "file_glob",
    source_name: str = "src/**/*.py",
    data: Any = None,
    elapsed_ms: float = 10.0,
) -> SourceResult:
    """Create a successful SourceResult."""
    return SourceResult(
        source_type=source_type,
        source_name=source_name,
        status="ok",
        data=data or {"files": ["a.py"]},
        elapsed_ms=elapsed_ms,
    )


def make_error_result(
    source_type: str = "mcp_tool",
    source_name: str = "search",
    error_message: str = "Tool not found",
    elapsed_ms: float = 1.0,
) -> SourceResult:
    """Create a failed SourceResult."""
    return SourceResult(
        source_type=source_type,
        source_name=source_name,
        status="error",
        data=None,
        error_message=error_message,
        elapsed_ms=elapsed_ms,
    )


# ---------------------------------------------------------------------------
# Pytest fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def template_variables() -> dict[str, str]:
    """Standard template variables for testing."""
    return {
        "task.description": "implement auth module",
        "task.id": "task-42",
        "workspace.root": "/workspace/project",
        "workspace.id": "ws-1",
        "agent.id": "agent-7",
        "agent.zone_id": "zone-alpha",
        "agent.owner_id": "user-1",
    }


@pytest.fixture
def sample_sources() -> list[Any]:
    """A list of sample sources covering all 4 types."""
    return [
        make_source("file_glob", pattern="src/**/*.py"),
        make_source("memory_query", query="relevant to {{task.description}}"),
        make_source("workspace_snapshot"),
        make_source("mcp_tool", tool_name="read_file", required=False),
    ]
