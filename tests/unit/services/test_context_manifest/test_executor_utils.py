"""Tests for executor_utils shared utilities (Issue #1428: 5A, 6A).

Covers:
1. resolve_source_template: happy path, no templates, error handling
2. Per-executor source protocols: structural conformance
3. get_executor_pool: returns custom or None
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor

from nexus.services.context_manifest.executors.executor_utils import (
    FileGlobSourceProtocol,
    MemoryQuerySourceProtocol,
    WorkspaceSnapshotSourceProtocol,
    get_executor_pool,
    resolve_source_template,
)
from nexus.services.context_manifest.models import (
    FileGlobSource,
    MemoryQuerySource,
    SourceResult,
    WorkspaceSnapshotSource,
)

# ---------------------------------------------------------------------------
# Test 1: resolve_source_template
# ---------------------------------------------------------------------------


class TestResolveSourceTemplate:
    def test_happy_path(self) -> None:
        """Template variables are resolved correctly."""
        source = FileGlobSource(pattern="src/**/*.py")
        start = time.monotonic()

        resolved, err = resolve_source_template(
            "{{task.id}}_files",
            {"task.id": "t123"},
            source,
            start,
        )

        assert err is None
        assert resolved == "t123_files"

    def test_no_templates(self) -> None:
        """String without templates is returned unchanged, no error."""
        source = FileGlobSource(pattern="*.py")
        start = time.monotonic()

        resolved, err = resolve_source_template("plain string", {}, source, start)

        assert err is None
        assert resolved == "plain string"

    def test_missing_variable_returns_error(self) -> None:
        """Missing template variable returns SourceResult.error."""
        source = MemoryQuerySource(query="test")
        start = time.monotonic()

        resolved, err = resolve_source_template(
            "query about {{task.description}}",
            {},  # missing task.description
            source,
            start,
        )

        assert err is not None
        assert isinstance(err, SourceResult)
        assert err.status == "error"
        assert "template" in err.error_message.lower()
        assert err.source_type == "memory_query"
        assert err.source_name == "test"

    def test_unknown_variable_returns_error(self) -> None:
        """Unknown template variable returns SourceResult.error."""
        source = FileGlobSource(pattern="*.py")
        start = time.monotonic()

        resolved, err = resolve_source_template(
            "{{bad.variable}}",
            {},
            source,
            start,
        )

        assert err is not None
        assert err.status == "error"
        assert "not allowed" in err.error_message

    def test_elapsed_ms_populated_on_error(self) -> None:
        """Error result includes positive elapsed_ms."""
        source = FileGlobSource(pattern="*.py")
        start = time.monotonic()

        _, err = resolve_source_template(
            "{{task.description}}",
            {},
            source,
            start,
        )

        assert err is not None
        assert err.elapsed_ms >= 0


# ---------------------------------------------------------------------------
# Test 2: Per-executor source protocols
# ---------------------------------------------------------------------------


class TestSourceProtocols:
    def test_file_glob_source_matches_protocol(self) -> None:
        """FileGlobSource satisfies FileGlobSourceProtocol."""
        source = FileGlobSource(pattern="src/*.py", max_files=20)
        assert isinstance(source, FileGlobSourceProtocol)
        assert source.pattern == "src/*.py"
        assert source.max_files == 20

    def test_memory_query_source_matches_protocol(self) -> None:
        """MemoryQuerySource satisfies MemoryQuerySourceProtocol."""
        source = MemoryQuerySource(query="find bugs", top_k=5)
        assert isinstance(source, MemoryQuerySourceProtocol)
        assert source.query == "find bugs"
        assert source.top_k == 5

    def test_workspace_snapshot_source_matches_protocol(self) -> None:
        """WorkspaceSnapshotSource satisfies WorkspaceSnapshotSourceProtocol."""
        source = WorkspaceSnapshotSource(snapshot_id="snap-001")
        assert isinstance(source, WorkspaceSnapshotSourceProtocol)
        assert source.snapshot_id == "snap-001"

    def test_cross_protocol_mismatch(self) -> None:
        """FileGlobSource does NOT satisfy MemoryQuerySourceProtocol."""
        source = FileGlobSource(pattern="*.py")
        assert not isinstance(source, MemoryQuerySourceProtocol)


# ---------------------------------------------------------------------------
# Test 3: get_executor_pool
# ---------------------------------------------------------------------------


class TestGetExecutorPool:
    def test_returns_none_for_default(self) -> None:
        """None input returns None (use default pool)."""
        assert get_executor_pool(None) is None

    def test_returns_custom_pool(self) -> None:
        """Custom pool is returned as-is."""
        pool = ThreadPoolExecutor(max_workers=2)
        try:
            assert get_executor_pool(pool) is pool
        finally:
            pool.shutdown(wait=False)
