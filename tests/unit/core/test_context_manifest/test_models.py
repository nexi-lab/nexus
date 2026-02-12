"""Tests for context manifest Pydantic models (Issue #1341).

TDD Phase 1 — RED: Write tests before implementation.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from typing import Any

import pytest
from pydantic import ValidationError


# ---------------------------------------------------------------------------
# Import targets — these don't exist yet (RED phase)
# ---------------------------------------------------------------------------
from nexus.core.context_manifest.models import (
    ContextSource,
    FileGlobSource,
    ManifestResolutionError,
    ManifestResult,
    MCPToolSource,
    MemoryQuerySource,
    SourceResult,
    WorkspaceSnapshotSource,
)


# ===========================================================================
# MCPToolSource
# ===========================================================================


class TestMCPToolSource:
    """Tests for the MCP tool source model."""

    def test_minimal_construction(self) -> None:
        source = MCPToolSource(tool_name="search_codebase")
        assert source.type == "mcp_tool"
        assert source.tool_name == "search_codebase"
        assert source.args == {}
        assert source.pre_exec is True
        assert source.required is True
        assert source.timeout_seconds == 30.0
        assert source.max_result_bytes == 1_048_576

    def test_full_construction(self) -> None:
        source = MCPToolSource(
            tool_name="read_file",
            args={"path": "/README.md"},
            pre_exec=False,
            required=False,
            timeout_seconds=10.0,
            max_result_bytes=512_000,
        )
        assert source.tool_name == "read_file"
        assert source.args == {"path": "/README.md"}
        assert source.pre_exec is False
        assert source.required is False
        assert source.timeout_seconds == 10.0
        assert source.max_result_bytes == 512_000

    def test_frozen(self) -> None:
        source = MCPToolSource(tool_name="test")
        with pytest.raises(ValidationError):
            source.tool_name = "modified"  # type: ignore[misc]

    def test_missing_tool_name_raises(self) -> None:
        with pytest.raises(ValidationError):
            MCPToolSource()  # type: ignore[call-arg]

    def test_round_trip_serialization(self) -> None:
        source = MCPToolSource(tool_name="search", args={"q": "hello"})
        data = source.model_dump()
        restored = MCPToolSource.model_validate(data)
        assert restored == source


# ===========================================================================
# WorkspaceSnapshotSource
# ===========================================================================


class TestWorkspaceSnapshotSource:
    """Tests for the workspace snapshot source model."""

    def test_defaults(self) -> None:
        source = WorkspaceSnapshotSource()
        assert source.type == "workspace_snapshot"
        assert source.snapshot_id == "latest"
        assert source.required is True
        assert source.timeout_seconds == 30.0
        assert source.max_result_bytes == 1_048_576

    def test_specific_snapshot(self) -> None:
        source = WorkspaceSnapshotSource(snapshot_id="abc123")
        assert source.snapshot_id == "abc123"

    def test_frozen(self) -> None:
        source = WorkspaceSnapshotSource()
        with pytest.raises(ValidationError):
            source.snapshot_id = "modified"  # type: ignore[misc]

    def test_round_trip_serialization(self) -> None:
        source = WorkspaceSnapshotSource(snapshot_id="snap-42")
        data = source.model_dump()
        restored = WorkspaceSnapshotSource.model_validate(data)
        assert restored == source


# ===========================================================================
# FileGlobSource
# ===========================================================================


class TestFileGlobSource:
    """Tests for the file glob source model."""

    def test_minimal(self) -> None:
        source = FileGlobSource(pattern="src/**/*.py")
        assert source.type == "file_glob"
        assert source.pattern == "src/**/*.py"
        assert source.max_files == 50
        assert source.required is True

    def test_custom_max_files(self) -> None:
        source = FileGlobSource(pattern="*.md", max_files=10)
        assert source.max_files == 10

    def test_empty_pattern_raises(self) -> None:
        with pytest.raises(ValidationError):
            FileGlobSource(pattern="")

    def test_max_files_zero_raises(self) -> None:
        with pytest.raises(ValidationError):
            FileGlobSource(pattern="*.py", max_files=0)

    def test_max_files_negative_raises(self) -> None:
        with pytest.raises(ValidationError):
            FileGlobSource(pattern="*.py", max_files=-1)

    def test_frozen(self) -> None:
        source = FileGlobSource(pattern="*.py")
        with pytest.raises(ValidationError):
            source.pattern = "*.js"  # type: ignore[misc]

    def test_round_trip_serialization(self) -> None:
        source = FileGlobSource(pattern="docs/**/*.md", max_files=20)
        data = source.model_dump()
        restored = FileGlobSource.model_validate(data)
        assert restored == source


# ===========================================================================
# MemoryQuerySource
# ===========================================================================


class TestMemoryQuerySource:
    """Tests for the memory query source model."""

    def test_minimal(self) -> None:
        source = MemoryQuerySource(query="relevant to {{task.description}}")
        assert source.type == "memory_query"
        assert source.query == "relevant to {{task.description}}"
        assert source.top_k == 10

    def test_custom_top_k(self) -> None:
        source = MemoryQuerySource(query="auth patterns", top_k=5)
        assert source.top_k == 5

    def test_empty_query_raises(self) -> None:
        with pytest.raises(ValidationError):
            MemoryQuerySource(query="")

    def test_top_k_zero_raises(self) -> None:
        with pytest.raises(ValidationError):
            MemoryQuerySource(query="test", top_k=0)

    def test_top_k_negative_raises(self) -> None:
        with pytest.raises(ValidationError):
            MemoryQuerySource(query="test", top_k=-1)

    def test_frozen(self) -> None:
        source = MemoryQuerySource(query="test")
        with pytest.raises(ValidationError):
            source.query = "modified"  # type: ignore[misc]

    def test_round_trip_serialization(self) -> None:
        source = MemoryQuerySource(query="find auth", top_k=3)
        data = source.model_dump()
        restored = MemoryQuerySource.model_validate(data)
        assert restored == source


# ===========================================================================
# Discriminated Union (ContextSource)
# ===========================================================================


class TestContextSourceDiscriminator:
    """Tests for the ContextSource discriminated union."""

    @pytest.mark.parametrize(
        "data,expected_type",
        [
            ({"type": "mcp_tool", "tool_name": "search"}, MCPToolSource),
            ({"type": "workspace_snapshot"}, WorkspaceSnapshotSource),
            ({"type": "file_glob", "pattern": "*.py"}, FileGlobSource),
            ({"type": "memory_query", "query": "test"}, MemoryQuerySource),
        ],
    )
    def test_discriminator_dispatches(
        self, data: dict[str, Any], expected_type: type
    ) -> None:
        from pydantic import TypeAdapter

        adapter = TypeAdapter(ContextSource)
        source = adapter.validate_python(data)
        assert isinstance(source, expected_type)

    def test_unknown_type_raises(self) -> None:
        from pydantic import TypeAdapter

        adapter = TypeAdapter(ContextSource)
        with pytest.raises(ValidationError):
            adapter.validate_python({"type": "unknown_source"})

    def test_missing_type_raises(self) -> None:
        from pydantic import TypeAdapter

        adapter = TypeAdapter(ContextSource)
        with pytest.raises(ValidationError):
            adapter.validate_python({"tool_name": "test"})

    def test_round_trip_all_types(self) -> None:
        from pydantic import TypeAdapter

        adapter = TypeAdapter(ContextSource)
        sources: list[Any] = [
            MCPToolSource(tool_name="search"),
            WorkspaceSnapshotSource(snapshot_id="latest"),
            FileGlobSource(pattern="**/*.py"),
            MemoryQuerySource(query="test query"),
        ]
        for source in sources:
            data = source.model_dump()
            restored = adapter.validate_python(data)
            assert restored == source


# ===========================================================================
# SourceResult
# ===========================================================================


class TestSourceResult:
    """Tests for the SourceResult frozen dataclass."""

    def test_ok_result(self) -> None:
        result = SourceResult(
            source_type="file_glob",
            source_name="src/**/*.py",
            status="ok",
            data={"files": ["a.py", "b.py"]},
            elapsed_ms=42.5,
        )
        assert result.status == "ok"
        assert result.error_message is None
        assert result.elapsed_ms == 42.5

    def test_error_result(self) -> None:
        result = SourceResult(
            source_type="mcp_tool",
            source_name="search",
            status="error",
            data=None,
            error_message="Tool not found",
            elapsed_ms=1.2,
        )
        assert result.status == "error"
        assert result.error_message == "Tool not found"

    def test_timeout_result(self) -> None:
        result = SourceResult(
            source_type="memory_query",
            source_name="test",
            status="timeout",
            data=None,
            elapsed_ms=30000.0,
        )
        assert result.status == "timeout"

    def test_truncated_result(self) -> None:
        result = SourceResult(
            source_type="file_glob",
            source_name="**/*",
            status="truncated",
            data=b"partial...",
            elapsed_ms=500.0,
        )
        assert result.status == "truncated"

    def test_skipped_result(self) -> None:
        result = SourceResult(
            source_type="mcp_tool",
            source_name="missing_tool",
            status="skipped",
            data=None,
        )
        assert result.status == "skipped"

    def test_frozen(self) -> None:
        result = SourceResult(
            source_type="file_glob",
            source_name="*.py",
            status="ok",
            data="test",
        )
        with pytest.raises(FrozenInstanceError):
            result.status = "error"  # type: ignore[misc]

    def test_defaults(self) -> None:
        result = SourceResult(
            source_type="mcp_tool",
            source_name="test",
            status="ok",
            data=None,
        )
        assert result.error_message is None
        assert result.elapsed_ms == 0.0


# ===========================================================================
# ManifestResult
# ===========================================================================


class TestManifestResult:
    """Tests for the ManifestResult frozen dataclass."""

    def test_construction(self) -> None:
        r1 = SourceResult(
            source_type="file_glob",
            source_name="*.py",
            status="ok",
            data=["a.py"],
            elapsed_ms=10.0,
        )
        r2 = SourceResult(
            source_type="memory_query",
            source_name="test",
            status="ok",
            data=["chunk1"],
            elapsed_ms=20.0,
        )
        result = ManifestResult(
            sources=(r1, r2),
            resolved_at="2026-02-13T10:00:00Z",
            total_ms=25.0,
        )
        assert len(result.sources) == 2
        assert result.total_ms == 25.0

    def test_empty_sources(self) -> None:
        result = ManifestResult(
            sources=(),
            resolved_at="2026-02-13T10:00:00Z",
            total_ms=0.5,
        )
        assert len(result.sources) == 0

    def test_frozen(self) -> None:
        result = ManifestResult(
            sources=(),
            resolved_at="2026-02-13T10:00:00Z",
            total_ms=0.0,
        )
        with pytest.raises(FrozenInstanceError):
            result.total_ms = 99.0  # type: ignore[misc]


# ===========================================================================
# ManifestResolutionError
# ===========================================================================


class TestManifestResolutionError:
    """Tests for the ManifestResolutionError exception."""

    def test_single_failure(self) -> None:
        failed = SourceResult(
            source_type="mcp_tool",
            source_name="search",
            status="error",
            data=None,
            error_message="not found",
        )
        err = ManifestResolutionError(failed_sources=(failed,))
        assert "search" in str(err)
        assert err.failed_sources == (failed,)

    def test_multiple_failures(self) -> None:
        f1 = SourceResult(
            source_type="mcp_tool", source_name="tool_a", status="error", data=None
        )
        f2 = SourceResult(
            source_type="file_glob", source_name="*.rs", status="timeout", data=None
        )
        err = ManifestResolutionError(failed_sources=(f1, f2))
        assert "tool_a" in str(err)
        assert "*.rs" in str(err)
        assert len(err.failed_sources) == 2

    def test_is_exception(self) -> None:
        failed = SourceResult(
            source_type="mcp_tool", source_name="x", status="error", data=None
        )
        err = ManifestResolutionError(failed_sources=(failed,))
        assert isinstance(err, Exception)
        with pytest.raises(ManifestResolutionError):
            raise err
