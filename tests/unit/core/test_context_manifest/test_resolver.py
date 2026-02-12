"""Tests for ManifestResolver and SourceExecutor (Issue #1341).

TDD Phase 3 — RED: Write tests before implementation.
Covers happy paths + 12 edge cases from the plan.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from nexus.core.context_manifest.models import (
    ManifestResolutionError,
    SourceResult,
)
from nexus.core.context_manifest.resolver import ManifestResolver, SourceExecutor

from .conftest import make_source

# ===========================================================================
# Stub executors for testing
# ===========================================================================


class OkExecutor:
    """Always returns ok with configurable data."""

    def __init__(self, data: Any = None, delay_ms: float = 0.0) -> None:
        self._data = data or {"result": "ok"}
        self._delay = delay_ms / 1000.0

    async def execute(self, source: Any, variables: dict[str, str]) -> SourceResult:
        if self._delay > 0:
            await asyncio.sleep(self._delay)
        name = _get_source_name(source)
        return SourceResult(
            source_type=source.type,
            source_name=name,
            status="ok",
            data=self._data,
            elapsed_ms=self._delay * 1000,
        )


class ErrorExecutor:
    """Always returns an error."""

    def __init__(self, message: str = "execution failed") -> None:
        self._message = message

    async def execute(self, source: Any, variables: dict[str, str]) -> SourceResult:
        name = _get_source_name(source)
        return SourceResult(
            source_type=source.type,
            source_name=name,
            status="error",
            data=None,
            error_message=self._message,
            elapsed_ms=1.0,
        )


class SlowExecutor:
    """Simulates a slow executor that takes longer than timeout."""

    def __init__(self, delay_seconds: float = 60.0) -> None:
        self._delay = delay_seconds

    async def execute(self, source: Any, variables: dict[str, str]) -> SourceResult:
        await asyncio.sleep(self._delay)
        name = _get_source_name(source)
        return SourceResult(
            source_type=source.type,
            source_name=name,
            status="ok",
            data={"result": "slow"},
            elapsed_ms=self._delay * 1000,
        )


class LargeDataExecutor:
    """Returns data larger than max_result_bytes."""

    def __init__(self, size_bytes: int = 2_000_000) -> None:
        self._size = size_bytes

    async def execute(self, source: Any, variables: dict[str, str]) -> SourceResult:
        name = _get_source_name(source)
        return SourceResult(
            source_type=source.type,
            source_name=name,
            status="ok",
            data="x" * self._size,
            elapsed_ms=5.0,
        )


class NoneDataExecutor:
    """Returns ok with None data."""

    async def execute(self, source: Any, variables: dict[str, str]) -> SourceResult:
        name = _get_source_name(source)
        return SourceResult(
            source_type=source.type,
            source_name=name,
            status="ok",
            data=None,
            elapsed_ms=0.5,
        )


def _get_source_name(source: Any) -> str:
    """Extract the human-readable name from a source model."""
    if hasattr(source, "tool_name"):
        return source.tool_name
    if hasattr(source, "snapshot_id"):
        return source.snapshot_id
    if hasattr(source, "pattern"):
        return source.pattern
    if hasattr(source, "query"):
        return source.query
    return "unknown"


def _make_executors(
    mcp: SourceExecutor | None = None,
    workspace: SourceExecutor | None = None,
    file_glob: SourceExecutor | None = None,
    memory: SourceExecutor | None = None,
) -> dict[str, SourceExecutor]:
    """Build executors dict with defaults."""
    ok = OkExecutor()
    return {
        "mcp_tool": mcp or ok,
        "workspace_snapshot": workspace or ok,
        "file_glob": file_glob or ok,
        "memory_query": memory or ok,
    }


# ===========================================================================
# Happy path tests
# ===========================================================================


class TestManifestResolverHappyPath:
    """Tests for successful manifest resolution."""

    @pytest.mark.asyncio
    async def test_single_source(self, tmp_path: Path) -> None:
        """Single file_glob source resolves and writes to output_dir."""
        resolver = ManifestResolver(executors=_make_executors())
        sources = [make_source("file_glob", pattern="*.py")]

        result = await resolver.resolve(sources, {}, tmp_path)

        assert len(result.sources) == 1
        assert result.sources[0].status == "ok"
        assert result.total_ms >= 0
        assert result.resolved_at  # non-empty ISO timestamp

    @pytest.mark.asyncio
    async def test_multiple_sources_parallel(self, tmp_path: Path) -> None:
        """Multiple sources resolve in parallel."""
        resolver = ManifestResolver(executors=_make_executors())
        sources = [
            make_source("file_glob", pattern="*.py"),
            make_source("memory_query", query="test"),
            make_source("workspace_snapshot"),
        ]

        result = await resolver.resolve(sources, {}, tmp_path)

        assert len(result.sources) == 3
        assert all(r.status == "ok" for r in result.sources)

    @pytest.mark.asyncio
    async def test_output_files_written(self, tmp_path: Path) -> None:
        """Result files are written to output_dir with correct structure."""
        resolver = ManifestResolver(executors=_make_executors())
        sources = [make_source("file_glob", pattern="src/**/*.py")]

        await resolver.resolve(sources, {}, tmp_path)

        # _index.json should exist
        index_path = tmp_path / "_index.json"
        assert index_path.exists()

        # At least one source result file should exist
        result_files = [f for f in tmp_path.iterdir() if f.name != "_index.json"]
        assert len(result_files) == 1

    @pytest.mark.asyncio
    async def test_index_json_structure(self, tmp_path: Path) -> None:
        """_index.json has the expected schema."""
        resolver = ManifestResolver(executors=_make_executors())
        sources = [make_source("file_glob", pattern="*.py")]

        await resolver.resolve(sources, {}, tmp_path)

        index = json.loads((tmp_path / "_index.json").read_text())
        assert "resolved_at" in index
        assert "total_ms" in index
        assert "source_count" in index
        assert "sources" in index
        assert len(index["sources"]) == 1
        assert index["sources"][0]["source_type"] == "file_glob"
        assert index["sources"][0]["status"] == "ok"
        assert "file" in index["sources"][0]

    @pytest.mark.asyncio
    async def test_template_variables_resolved(
        self, tmp_path: Path, template_variables: dict[str, str]
    ) -> None:
        """Template variables in sources are resolved before execution."""
        resolver = ManifestResolver(executors=_make_executors())
        sources = [make_source("memory_query", query="relevant to {{task.description}}")]

        # Should not raise — template is valid
        result = await resolver.resolve(sources, template_variables, tmp_path)
        assert result.sources[0].status == "ok"


# ===========================================================================
# Edge case 1: Empty manifest
# ===========================================================================


class TestEdgeCaseEmptyManifest:
    @pytest.mark.asyncio
    async def test_empty_manifest_produces_empty_index(self, tmp_path: Path) -> None:
        resolver = ManifestResolver(executors=_make_executors())

        result = await resolver.resolve([], {}, tmp_path)

        assert len(result.sources) == 0
        index = json.loads((tmp_path / "_index.json").read_text())
        assert index["source_count"] == 0


# ===========================================================================
# Edge case 2: All required sources fail
# ===========================================================================


class TestEdgeCaseAllRequiredFail:
    @pytest.mark.asyncio
    async def test_all_required_fail_raises(self, tmp_path: Path) -> None:
        executors = _make_executors(file_glob=ErrorExecutor())
        resolver = ManifestResolver(executors=executors)
        sources = [make_source("file_glob", pattern="*.py", required=True)]

        with pytest.raises(ManifestResolutionError) as exc_info:
            await resolver.resolve(sources, {}, tmp_path)

        assert len(exc_info.value.failed_sources) == 1


# ===========================================================================
# Edge case 3: Required fail + optional succeed
# ===========================================================================


class TestEdgeCaseRequiredFailOptionalSucceed:
    @pytest.mark.asyncio
    async def test_required_fail_aborts_despite_optional_success(self, tmp_path: Path) -> None:
        executors = _make_executors(
            file_glob=ErrorExecutor(),
            memory=OkExecutor(),
        )
        resolver = ManifestResolver(executors=executors)
        sources = [
            make_source("file_glob", pattern="*.py", required=True),
            make_source("memory_query", query="test", required=False),
        ]

        with pytest.raises(ManifestResolutionError):
            await resolver.resolve(sources, {}, tmp_path)


# ===========================================================================
# Edge case 4: Required succeed + optional fail
# ===========================================================================


class TestEdgeCaseOptionalFail:
    @pytest.mark.asyncio
    async def test_optional_fail_does_not_abort(self, tmp_path: Path) -> None:
        executors = _make_executors(
            file_glob=OkExecutor(),
            mcp=ErrorExecutor(),
        )
        resolver = ManifestResolver(executors=executors)
        sources = [
            make_source("file_glob", pattern="*.py", required=True),
            make_source("mcp_tool", tool_name="broken", required=False),
        ]

        result = await resolver.resolve(sources, {}, tmp_path)

        # Should succeed — required source is ok
        assert len(result.sources) == 2
        ok_sources = [s for s in result.sources if s.status == "ok"]
        error_sources = [s for s in result.sources if s.status == "error"]
        assert len(ok_sources) == 1
        assert len(error_sources) == 1


# ===========================================================================
# Edge case 5: Source timeout
# ===========================================================================


class TestEdgeCaseSourceTimeout:
    @pytest.mark.asyncio
    async def test_source_timeout_produces_timeout_status(self, tmp_path: Path) -> None:
        executors = _make_executors(file_glob=SlowExecutor(delay_seconds=10.0))
        resolver = ManifestResolver(executors=executors)
        sources = [make_source("file_glob", pattern="*.py", timeout_seconds=0.1, required=False)]

        result = await resolver.resolve(sources, {}, tmp_path)

        assert result.sources[0].status == "timeout"

    @pytest.mark.asyncio
    async def test_required_timeout_raises(self, tmp_path: Path) -> None:
        executors = _make_executors(file_glob=SlowExecutor(delay_seconds=10.0))
        resolver = ManifestResolver(executors=executors)
        sources = [make_source("file_glob", pattern="*.py", timeout_seconds=0.1, required=True)]

        with pytest.raises(ManifestResolutionError):
            await resolver.resolve(sources, {}, tmp_path)


# ===========================================================================
# Edge case 6: Result exceeds max_result_bytes
# ===========================================================================


class TestEdgeCaseResultTruncation:
    @pytest.mark.asyncio
    async def test_large_result_truncated(self, tmp_path: Path) -> None:
        executors = _make_executors(file_glob=LargeDataExecutor(size_bytes=2_000_000))
        resolver = ManifestResolver(executors=executors)
        sources = [
            make_source(
                "file_glob",
                pattern="*.py",
                max_result_bytes=1000,
                required=False,
            )
        ]

        result = await resolver.resolve(sources, {}, tmp_path)

        assert result.sources[0].status == "truncated"


# ===========================================================================
# Edge case 7: Undefined template variable
# ===========================================================================


class TestEdgeCaseUndefinedVariable:
    @pytest.mark.asyncio
    async def test_undefined_variable_raises_before_execution(self, tmp_path: Path) -> None:
        resolver = ManifestResolver(executors=_make_executors())
        sources = [make_source("memory_query", query="{{task.description}}")]

        with pytest.raises(ValueError, match="task.description"):
            await resolver.resolve(sources, {}, tmp_path)  # no variables provided


# ===========================================================================
# Edge case 8: Template injection attempt
# ===========================================================================


class TestEdgeCaseTemplateInjection:
    @pytest.mark.asyncio
    async def test_injection_via_dunder_raises(self, tmp_path: Path) -> None:
        resolver = ManifestResolver(executors=_make_executors())
        sources = [make_source("memory_query", query="{{task.__class__}}")]

        with pytest.raises(ValueError, match="task.__class__"):
            await resolver.resolve(sources, {}, tmp_path)


# ===========================================================================
# Edge case 9: Concurrent resolution for 2 agents
# ===========================================================================


class TestEdgeCaseConcurrentResolution:
    @pytest.mark.asyncio
    async def test_no_shared_state_between_concurrent_resolves(self, tmp_path: Path) -> None:
        """Two concurrent resolve() calls produce independent results."""
        resolver = ManifestResolver(
            executors=_make_executors(file_glob=OkExecutor(data={"files": ["a.py"]}, delay_ms=50))
        )

        dir1 = tmp_path / "agent1"
        dir2 = tmp_path / "agent2"
        dir1.mkdir()
        dir2.mkdir()

        sources1 = [make_source("file_glob", pattern="*.py")]
        sources2 = [make_source("file_glob", pattern="*.rs")]

        r1, r2 = await asyncio.gather(
            resolver.resolve(sources1, {}, dir1),
            resolver.resolve(sources2, {}, dir2),
        )

        assert len(r1.sources) == 1
        assert len(r2.sources) == 1
        # Both directories should have their own _index.json
        assert (dir1 / "_index.json").exists()
        assert (dir2 / "_index.json").exists()


# ===========================================================================
# Edge case 10: Source returns None/empty data
# ===========================================================================


class TestEdgeCaseEmptyData:
    @pytest.mark.asyncio
    async def test_none_data_produces_ok_status(self, tmp_path: Path) -> None:
        executors = _make_executors(file_glob=NoneDataExecutor())
        resolver = ManifestResolver(executors=executors)
        sources = [make_source("file_glob", pattern="*.py")]

        result = await resolver.resolve(sources, {}, tmp_path)

        assert result.sources[0].status == "ok"
        assert result.sources[0].data is None


# ===========================================================================
# Edge case 11: Duplicate sources
# ===========================================================================


class TestEdgeCaseDuplicateSources:
    @pytest.mark.asyncio
    async def test_duplicate_sources_execute_independently(self, tmp_path: Path) -> None:
        resolver = ManifestResolver(executors=_make_executors())
        sources = [
            make_source("file_glob", pattern="*.py"),
            make_source("file_glob", pattern="*.py"),
        ]

        result = await resolver.resolve(sources, {}, tmp_path)

        assert len(result.sources) == 2
        assert all(r.status == "ok" for r in result.sources)


# ===========================================================================
# Edge case 12: Unicode in template values
# ===========================================================================


class TestEdgeCaseUnicodeTemplates:
    @pytest.mark.asyncio
    async def test_unicode_template_values(self, tmp_path: Path) -> None:
        resolver = ManifestResolver(executors=_make_executors())
        sources = [make_source("memory_query", query="{{task.description}}")]
        variables = {"task.description": "日本語テスト"}

        result = await resolver.resolve(sources, variables, tmp_path)

        assert result.sources[0].status == "ok"


# ===========================================================================
# Global timeout
# ===========================================================================


class TestGlobalTimeout:
    @pytest.mark.asyncio
    async def test_global_timeout_cancels_remaining(self, tmp_path: Path) -> None:
        """max_resolve_seconds causes overall cancellation."""
        executors = _make_executors(
            file_glob=SlowExecutor(delay_seconds=10.0),
            memory=SlowExecutor(delay_seconds=10.0),
        )
        resolver = ManifestResolver(executors=executors, max_resolve_seconds=0.2)
        sources = [
            make_source("file_glob", pattern="*.py", required=False, timeout_seconds=60),
            make_source("memory_query", query="test", required=False, timeout_seconds=60),
        ]

        result = await resolver.resolve(sources, {}, tmp_path)

        # Both should be timeout due to global timeout
        assert all(r.status == "timeout" for r in result.sources)


# ===========================================================================
# Executor missing for source type
# ===========================================================================


class TestMissingExecutor:
    @pytest.mark.asyncio
    async def test_missing_executor_skips_source(self, tmp_path: Path) -> None:
        """If no executor registered for a source type, the source is skipped."""
        resolver = ManifestResolver(executors={})  # no executors
        sources = [make_source("file_glob", pattern="*.py", required=False)]

        result = await resolver.resolve(sources, {}, tmp_path)

        assert result.sources[0].status == "skipped"

    @pytest.mark.asyncio
    async def test_missing_executor_required_raises(self, tmp_path: Path) -> None:
        """Required source with no executor raises."""
        resolver = ManifestResolver(executors={})
        sources = [make_source("file_glob", pattern="*.py", required=True)]

        with pytest.raises(ManifestResolutionError):
            await resolver.resolve(sources, {}, tmp_path)


# ===========================================================================
# Constructor validation
# ===========================================================================


class TestConstructorValidation:
    def test_negative_max_resolve_seconds_raises(self) -> None:
        with pytest.raises(ValueError, match="positive"):
            ManifestResolver(executors={}, max_resolve_seconds=-1.0)

    def test_zero_max_resolve_seconds_raises(self) -> None:
        with pytest.raises(ValueError, match="positive"):
            ManifestResolver(executors={}, max_resolve_seconds=0.0)

    def test_positive_max_resolve_seconds_ok(self) -> None:
        resolver = ManifestResolver(executors={}, max_resolve_seconds=0.1)
        assert resolver._max_resolve_seconds == 0.1
