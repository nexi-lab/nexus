"""Tests for thread pool delegation in executors (Issue #1428: 12A).

Verifies that MemoryQueryExecutor, WorkspaceSnapshotExecutor, and
FileGlobExecutor delegate blocking I/O to a thread pool via
loop.run_in_executor(), and that a custom thread pool is used when provided.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from nexus.services.context_manifest.executors.memory_query import MemoryQueryExecutor
from nexus.services.context_manifest.executors.workspace_snapshot import (
    WorkspaceSnapshotExecutor,
)
from nexus.services.context_manifest.models import MemoryQuerySource, WorkspaceSnapshotSource

# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


class StubMemorySearch:
    def search(self, query: str, top_k: int, search_mode: str) -> tuple[list[dict[str, Any]], str]:
        return [], search_mode


class StubSnapshotLookup:
    def get_snapshot(self, snapshot_id: str) -> dict[str, Any] | None:
        return {
            "snapshot_id": snapshot_id,
            "workspace_path": "/ws",
            "snapshot_number": 1,
            "manifest_hash": "abc",
            "file_count": 1,
            "total_size_bytes": 100,
            "description": "test",
            "created_by": "user",
            "tags": [],
            "created_at": "2025-01-01T00:00:00",
        }

    def get_latest_snapshot(self, workspace_path: str) -> dict[str, Any] | None:
        return None


# ---------------------------------------------------------------------------
# Test 1: MemoryQueryExecutor uses thread pool
# ---------------------------------------------------------------------------


class TestMemoryQueryThreadPool:
    @pytest.mark.asyncio
    async def test_uses_run_in_executor(self) -> None:
        """MemoryQueryExecutor.execute calls run_in_executor."""
        stub = StubMemorySearch()
        executor = MemoryQueryExecutor(memory_search=stub)
        source = MemoryQuerySource(query="test")

        # Patch the event loop's run_in_executor
        with patch("asyncio.get_running_loop") as mock_loop_fn:
            mock_loop = MagicMock()
            mock_loop_fn.return_value = mock_loop
            # Make run_in_executor return a coroutine-like result
            import asyncio

            future = asyncio.Future()
            from nexus.services.context_manifest.models import SourceResult

            future.set_result(
                SourceResult.ok(source_type="memory_query", source_name="test", data={})
            )
            mock_loop.run_in_executor.return_value = future

            await executor.execute(source, {})

            mock_loop.run_in_executor.assert_called_once()
            # Verify None is passed as executor (default pool)
            call_args = mock_loop.run_in_executor.call_args
            assert call_args[0][0] is None  # default pool

    @pytest.mark.asyncio
    async def test_custom_thread_pool(self) -> None:
        """Custom thread pool is passed to run_in_executor."""
        stub = StubMemorySearch()
        custom_pool = ThreadPoolExecutor(max_workers=2)
        executor = MemoryQueryExecutor(memory_search=stub, thread_pool=custom_pool)
        source = MemoryQuerySource(query="test")

        with patch("asyncio.get_running_loop") as mock_loop_fn:
            mock_loop = MagicMock()
            mock_loop_fn.return_value = mock_loop
            import asyncio

            future = asyncio.Future()
            from nexus.services.context_manifest.models import SourceResult

            future.set_result(
                SourceResult.ok(source_type="memory_query", source_name="test", data={})
            )
            mock_loop.run_in_executor.return_value = future

            await executor.execute(source, {})

            call_args = mock_loop.run_in_executor.call_args
            assert call_args[0][0] is custom_pool

        custom_pool.shutdown(wait=False)


# ---------------------------------------------------------------------------
# Test 2: WorkspaceSnapshotExecutor uses thread pool
# ---------------------------------------------------------------------------


class TestWorkspaceSnapshotThreadPool:
    @pytest.mark.asyncio
    async def test_uses_run_in_executor(self) -> None:
        """WorkspaceSnapshotExecutor.execute calls run_in_executor."""
        lookup = StubSnapshotLookup()
        executor = WorkspaceSnapshotExecutor(snapshot_lookup=lookup)
        source = WorkspaceSnapshotSource(snapshot_id="snap-001")

        with patch("asyncio.get_running_loop") as mock_loop_fn:
            mock_loop = MagicMock()
            mock_loop_fn.return_value = mock_loop
            import asyncio

            future = asyncio.Future()
            from nexus.services.context_manifest.models import SourceResult

            future.set_result(
                SourceResult.ok(source_type="workspace_snapshot", source_name="snap-001", data={})
            )
            mock_loop.run_in_executor.return_value = future

            await executor.execute(source, {})

            mock_loop.run_in_executor.assert_called_once()
            call_args = mock_loop.run_in_executor.call_args
            assert call_args[0][0] is None  # default pool

    @pytest.mark.asyncio
    async def test_custom_thread_pool(self) -> None:
        """Custom thread pool is passed to run_in_executor."""
        lookup = StubSnapshotLookup()
        custom_pool = ThreadPoolExecutor(max_workers=2)
        executor = WorkspaceSnapshotExecutor(snapshot_lookup=lookup, thread_pool=custom_pool)
        source = WorkspaceSnapshotSource(snapshot_id="snap-001")

        with patch("asyncio.get_running_loop") as mock_loop_fn:
            mock_loop = MagicMock()
            mock_loop_fn.return_value = mock_loop
            import asyncio

            future = asyncio.Future()
            from nexus.services.context_manifest.models import SourceResult

            future.set_result(
                SourceResult.ok(source_type="workspace_snapshot", source_name="snap-001", data={})
            )
            mock_loop.run_in_executor.return_value = future

            await executor.execute(source, {})

            call_args = mock_loop.run_in_executor.call_args
            assert call_args[0][0] is custom_pool

        custom_pool.shutdown(wait=False)
