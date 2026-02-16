"""Deterministic concurrent resolver tests (Issue #1428: 11B).

Uses asyncio.Event for deterministic control of concurrent execution:
1. Mixed latencies: fast + slow executors run truly in parallel
2. Per-source timeout fires while other sources succeed
3. Global timeout fires mid-execution, preserving completed results
4. Multiple executors of different types interleave correctly
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from nexus.services.context_manifest.models import (
    FileGlobSource,
    MemoryQuerySource,
    SourceResult,
    WorkspaceSnapshotSource,
)
from nexus.services.context_manifest.resolver import ManifestResolver

# ---------------------------------------------------------------------------
# Event-driven stub executors (no real time, no flakiness)
# ---------------------------------------------------------------------------


class EventControlledExecutor:
    """Executor that waits on an asyncio.Event before returning.

    This allows tests to control exactly when each executor completes,
    making concurrent tests deterministic.
    """

    def __init__(
        self,
        event: asyncio.Event | None = None,
        result_data: Any = None,
        error: Exception | None = None,
    ) -> None:
        self._event = event or asyncio.Event()
        self._result_data = result_data or {"test": True}
        self._error = error
        self.execute_called = asyncio.Event()

    async def execute(self, source: Any, variables: dict[str, str]) -> SourceResult:
        self.execute_called.set()  # Signal that execute was called
        await self._event.wait()  # Wait for test to release
        if self._error is not None:
            raise self._error
        return SourceResult.ok(
            source_type=source.type,
            source_name=source.source_name,
            data=self._result_data,
            elapsed_ms=1.0,
        )


class ImmediateExecutor:
    """Executor that completes immediately."""

    def __init__(self, data: Any = None) -> None:
        self._data = data or {"immediate": True}

    async def execute(self, source: Any, variables: dict[str, str]) -> SourceResult:
        return SourceResult.ok(
            source_type=source.type,
            source_name=source.source_name,
            data=self._data,
            elapsed_ms=0.1,
        )


# ---------------------------------------------------------------------------
# Test 1: Mixed latencies — fast + slow executors run in parallel
# ---------------------------------------------------------------------------


class TestMixedLatencies:
    @pytest.mark.asyncio
    async def test_fast_source_not_blocked_by_slow(self, tmp_path: Path) -> None:
        """Fast executor completes while slow executor is still running."""
        slow_event = asyncio.Event()
        slow_exec = EventControlledExecutor(event=slow_event, result_data={"slow": True})
        fast_exec = ImmediateExecutor(data={"fast": True})

        resolver = ManifestResolver(
            executors={"file_glob": fast_exec, "memory_query": slow_exec},
            max_resolve_seconds=10.0,
        )
        sources = [
            FileGlobSource(pattern="*.py"),
            MemoryQuerySource(query="slow query"),
        ]

        async def release_slow():
            await slow_exec.execute_called.wait()
            slow_event.set()

        release_task = asyncio.create_task(release_slow())
        result = await resolver.resolve(sources, {}, tmp_path)
        await release_task

        assert len(result.sources) == 2
        assert all(r.status == "ok" for r in result.sources)


# ---------------------------------------------------------------------------
# Test 2: Per-source timeout fires while other sources succeed
# ---------------------------------------------------------------------------


class TestPerSourceTimeout:
    @pytest.mark.asyncio
    async def test_per_source_timeout_isolated(self, tmp_path: Path) -> None:
        """One source times out; other sources still succeed."""
        # Slow executor that never completes
        never_event = asyncio.Event()  # Never set
        slow_exec = EventControlledExecutor(event=never_event)
        fast_exec = ImmediateExecutor()

        resolver = ManifestResolver(
            executors={"file_glob": fast_exec, "memory_query": slow_exec},
            max_resolve_seconds=10.0,
        )
        sources = [
            FileGlobSource(pattern="*.py"),
            MemoryQuerySource(query="never finishes", timeout_seconds=0.1, required=False),
        ]

        result = await resolver.resolve(sources, {}, tmp_path)

        # Fast source succeeded
        assert result.sources[0].status == "ok"
        # Slow source timed out
        assert result.sources[1].status == "timeout"


# ---------------------------------------------------------------------------
# Test 3: Global timeout preserves completed results
# ---------------------------------------------------------------------------


class TestGlobalTimeout:
    @pytest.mark.asyncio
    async def test_global_timeout_preserves_completed(self) -> None:
        """Global timeout fires; already-completed sources keep their results."""
        never_event = asyncio.Event()
        slow_exec = EventControlledExecutor(event=never_event)
        fast_exec = ImmediateExecutor(data={"completed": True})

        resolver = ManifestResolver(
            executors={"file_glob": fast_exec, "memory_query": slow_exec},
            max_resolve_seconds=0.2,
        )
        sources = [
            FileGlobSource(pattern="*.py", required=False),
            MemoryQuerySource(query="hangs forever", timeout_seconds=60.0, required=False),
        ]

        result = await resolver.resolve(sources, {})

        # Fast source completed before timeout
        assert result.sources[0].status == "ok"
        assert result.sources[0].data == {"completed": True}
        # Slow source hit global timeout
        assert result.sources[1].status == "timeout"


# ---------------------------------------------------------------------------
# Test 4: Multiple executor types interleave correctly
# ---------------------------------------------------------------------------


class TestMultiExecutorInterleave:
    @pytest.mark.asyncio
    async def test_three_types_resolve_in_parallel(self, tmp_path: Path) -> None:
        """Three different source types all resolve in parallel."""
        events = [asyncio.Event() for _ in range(3)]
        executors = {
            "file_glob": EventControlledExecutor(event=events[0], result_data={"glob": True}),
            "memory_query": EventControlledExecutor(event=events[1], result_data={"memory": True}),
            "workspace_snapshot": EventControlledExecutor(
                event=events[2], result_data={"snap": True}
            ),
        }

        resolver = ManifestResolver(executors=executors, max_resolve_seconds=10.0)
        sources = [
            FileGlobSource(pattern="*.py"),
            MemoryQuerySource(query="test"),
            WorkspaceSnapshotSource(snapshot_id="snap-001"),
        ]

        async def release_all():
            # Wait for all executors to be called
            for exec_impl in executors.values():
                await exec_impl.execute_called.wait()
            # Release all at once
            for e in events:
                e.set()

        release_task = asyncio.create_task(release_all())
        result = await resolver.resolve(sources, {}, tmp_path)
        await release_task

        assert len(result.sources) == 3
        assert all(r.status == "ok" for r in result.sources)
        # Verify each type returned its data
        types_data = {r.source_type: r.data for r in result.sources}
        assert types_data["file_glob"] == {"glob": True}
        assert types_data["memory_query"] == {"memory": True}
        assert types_data["workspace_snapshot"] == {"snap": True}


# ---------------------------------------------------------------------------
# Test 5: with_executors creates independent resolver
# ---------------------------------------------------------------------------


class TestWithExecutors:
    @pytest.mark.asyncio
    async def test_with_executors_does_not_mutate_original(self) -> None:
        """with_executors returns a new resolver; original is unchanged."""
        exec_a = ImmediateExecutor(data={"a": True})
        exec_b = ImmediateExecutor(data={"b": True})

        original = ManifestResolver(
            executors={"file_glob": exec_a},
            max_resolve_seconds=5.0,
        )
        augmented = original.with_executors({"memory_query": exec_b})

        # Original only handles file_glob
        sources_glob = [FileGlobSource(pattern="*.py")]
        result_orig = await original.resolve(sources_glob, {})
        assert result_orig.sources[0].status == "ok"

        # memory_query on original -> skipped
        sources_mem = [MemoryQuerySource(query="test", required=False)]
        result_skip = await original.resolve(sources_mem, {})
        assert result_skip.sources[0].status == "skipped"

        # Augmented handles both
        result_aug = await augmented.resolve(sources_mem, {})
        assert result_aug.sources[0].status == "ok"

    @pytest.mark.asyncio
    async def test_with_executors_overrides_existing(self) -> None:
        """with_executors overrides an existing executor for a source type."""
        exec_v1 = ImmediateExecutor(data={"version": 1})
        exec_v2 = ImmediateExecutor(data={"version": 2})

        original = ManifestResolver(executors={"file_glob": exec_v1})
        augmented = original.with_executors({"file_glob": exec_v2})

        sources = [FileGlobSource(pattern="*.py")]

        result_orig = await original.resolve(sources, {})
        assert result_orig.sources[0].data == {"version": 1}

        result_aug = await augmented.resolve(sources, {})
        assert result_aug.sources[0].data == {"version": 2}


# ---------------------------------------------------------------------------
# Test 6: Resolve without output_dir (15A)
# ---------------------------------------------------------------------------


class TestResolveWithoutOutputDir:
    @pytest.mark.asyncio
    async def test_resolve_without_output_dir(self) -> None:
        """Resolve with output_dir=None returns results without writing files."""
        exec_ = ImmediateExecutor(data={"in_memory": True})
        resolver = ManifestResolver(executors={"file_glob": exec_})
        sources = [FileGlobSource(pattern="*.py")]

        result = await resolver.resolve(sources, {})

        assert len(result.sources) == 1
        assert result.sources[0].status == "ok"
        assert result.sources[0].data == {"in_memory": True}

    @pytest.mark.asyncio
    async def test_resolve_with_output_dir_writes_files(self, tmp_path: Path) -> None:
        """Resolve with output_dir writes files as before."""
        exec_ = ImmediateExecutor(data={"on_disk": True})
        resolver = ManifestResolver(executors={"file_glob": exec_})
        sources = [FileGlobSource(pattern="*.py")]

        await resolver.resolve(sources, {}, tmp_path)

        assert (tmp_path / "_index.json").exists()
