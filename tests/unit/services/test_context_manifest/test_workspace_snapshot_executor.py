"""Tests for WorkspaceSnapshotExecutor (Issue #1428).

Covers:
1. Happy path: specific snapshot_id found
2. Latest resolution with workspace.root variable
3. Latest without workspace.root → error
4. Latest with no snapshots → error
5. Specific ID not found → error
6. File tree included when reader provided
7. File tree capped at MAX_TREE_FILES (200)
8. File tree omitted when no reader
9. File tree omitted on reader failure (graceful)
10. Template variable in snapshot_id
11. elapsed_ms positive
12. source_type/source_name correct
"""

from __future__ import annotations

from typing import Any

import pytest

from nexus.services.context_manifest.executors.workspace_snapshot import (
    MAX_TREE_FILES,
    WorkspaceSnapshotExecutor,
)
from nexus.services.context_manifest.models import WorkspaceSnapshotSource

# ---------------------------------------------------------------------------
# Stub implementations
# ---------------------------------------------------------------------------


class StubSnapshotLookup:
    """Stub SnapshotLookup that returns configurable snapshot data."""

    def __init__(
        self,
        snapshots: dict[str, dict[str, Any]] | None = None,
        latest_snapshots: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self._snapshots = snapshots or {}
        self._latest = latest_snapshots or {}

    def get_snapshot(self, snapshot_id: str) -> dict[str, Any] | None:
        return self._snapshots.get(snapshot_id)

    def get_latest_snapshot(self, workspace_path: str) -> dict[str, Any] | None:
        return self._latest.get(workspace_path)


class StubManifestReader:
    """Stub ManifestReader that returns configurable file paths."""

    def __init__(
        self,
        paths: dict[str, list[str]] | None = None,
        error: Exception | None = None,
    ) -> None:
        self._paths = paths or {}
        self._error = error

    def read_file_paths(self, manifest_hash: str) -> list[str] | None:
        if self._error is not None:
            raise self._error
        return self._paths.get(manifest_hash)


def _sample_snapshot(
    snapshot_id: str = "snap-001",
    workspace_path: str = "/my-workspace",
    snapshot_number: int = 5,
    manifest_hash: str = "abc123",
) -> dict[str, Any]:
    return {
        "snapshot_id": snapshot_id,
        "workspace_path": workspace_path,
        "snapshot_number": snapshot_number,
        "manifest_hash": manifest_hash,
        "file_count": 42,
        "total_size_bytes": 128000,
        "description": "Weekly snapshot",
        "created_by": "user1",
        "tags": ["release", "v2"],
        "created_at": "2025-01-15T10:00:00",
    }


def _make_source(snapshot_id: str = "snap-001", **kw: Any) -> WorkspaceSnapshotSource:
    return WorkspaceSnapshotSource(snapshot_id=snapshot_id, **kw)


# ---------------------------------------------------------------------------
# Test 1: Happy path — specific snapshot_id found
# ---------------------------------------------------------------------------


class TestHappyPath:
    @pytest.mark.asyncio
    async def test_specific_snapshot_found(self) -> None:
        """Specific snapshot_id returns snapshot metadata."""
        snap = _sample_snapshot()
        lookup = StubSnapshotLookup(snapshots={"snap-001": snap})
        executor = WorkspaceSnapshotExecutor(snapshot_lookup=lookup)

        result = await executor.execute(_make_source("snap-001"), {})

        assert result.status == "ok"
        assert result.data["snapshot_id"] == "snap-001"
        assert result.data["snapshot_number"] == 5
        assert result.data["workspace_path"] == "/my-workspace"
        assert result.data["file_count"] == 42
        assert result.data["total_size_bytes"] == 128000
        assert result.data["description"] == "Weekly snapshot"
        assert result.data["created_by"] == "user1"
        assert result.data["tags"] == ["release", "v2"]
        assert result.data["created_at"] == "2025-01-15T10:00:00"


# ---------------------------------------------------------------------------
# Test 2: Latest resolution with workspace.root
# ---------------------------------------------------------------------------


class TestLatestResolution:
    @pytest.mark.asyncio
    async def test_latest_with_workspace_root(self) -> None:
        """'latest' resolves to most recent snapshot for workspace.root."""
        snap = _sample_snapshot(snapshot_id="snap-latest")
        lookup = StubSnapshotLookup(latest_snapshots={"/my-workspace": snap})
        executor = WorkspaceSnapshotExecutor(snapshot_lookup=lookup)

        variables = {"workspace.root": "/my-workspace"}
        result = await executor.execute(_make_source("latest"), variables)

        assert result.status == "ok"
        assert result.data["snapshot_id"] == "snap-latest"


# ---------------------------------------------------------------------------
# Test 3: Latest without workspace.root → error
# ---------------------------------------------------------------------------


class TestLatestWithoutRoot:
    @pytest.mark.asyncio
    async def test_latest_without_workspace_root(self) -> None:
        """'latest' without workspace.root → error."""
        lookup = StubSnapshotLookup()
        executor = WorkspaceSnapshotExecutor(snapshot_lookup=lookup)

        result = await executor.execute(_make_source("latest"), {})

        assert result.status == "error"
        assert "workspace.root" in result.error_message


# ---------------------------------------------------------------------------
# Test 4: Latest with no snapshots → error
# ---------------------------------------------------------------------------


class TestLatestNoSnapshots:
    @pytest.mark.asyncio
    async def test_latest_no_snapshots_found(self) -> None:
        """'latest' with workspace.root but no snapshots → error."""
        lookup = StubSnapshotLookup(latest_snapshots={})
        executor = WorkspaceSnapshotExecutor(snapshot_lookup=lookup)

        variables = {"workspace.root": "/empty-workspace"}
        result = await executor.execute(_make_source("latest"), variables)

        assert result.status == "error"
        assert "No snapshots" in result.error_message


# ---------------------------------------------------------------------------
# Test 5: Specific ID not found → error
# ---------------------------------------------------------------------------


class TestSnapshotNotFound:
    @pytest.mark.asyncio
    async def test_specific_id_not_found(self) -> None:
        """Specific snapshot_id not in DB → error."""
        lookup = StubSnapshotLookup(snapshots={})
        executor = WorkspaceSnapshotExecutor(snapshot_lookup=lookup)

        result = await executor.execute(_make_source("nonexistent"), {})

        assert result.status == "error"
        assert "not found" in result.error_message


# ---------------------------------------------------------------------------
# Test 6: File tree included when reader provided
# ---------------------------------------------------------------------------


class TestFileTreeIncluded:
    @pytest.mark.asyncio
    async def test_file_tree_from_manifest_reader(self) -> None:
        """File tree is included when manifest_reader is provided."""
        snap = _sample_snapshot(manifest_hash="hash123")
        lookup = StubSnapshotLookup(snapshots={"snap-001": snap})
        reader = StubManifestReader(
            paths={"hash123": ["src/main.py", "src/utils.py", "README.md"]}
        )
        executor = WorkspaceSnapshotExecutor(
            snapshot_lookup=lookup, manifest_reader=reader
        )

        result = await executor.execute(_make_source("snap-001"), {})

        assert result.status == "ok"
        assert result.data["file_tree"] == ["src/main.py", "src/utils.py", "README.md"]
        assert result.data["file_tree_total"] == 3
        assert result.data["file_tree_capped"] is False


# ---------------------------------------------------------------------------
# Test 7: File tree capped at MAX_TREE_FILES
# ---------------------------------------------------------------------------


class TestFileTreeCapped:
    @pytest.mark.asyncio
    async def test_file_tree_capped_at_max(self) -> None:
        """File tree is capped at MAX_TREE_FILES (200)."""
        snap = _sample_snapshot(manifest_hash="hash-big")
        lookup = StubSnapshotLookup(snapshots={"snap-001": snap})
        many_paths = [f"file_{i:04d}.py" for i in range(500)]
        reader = StubManifestReader(paths={"hash-big": many_paths})
        executor = WorkspaceSnapshotExecutor(
            snapshot_lookup=lookup, manifest_reader=reader
        )

        result = await executor.execute(_make_source("snap-001"), {})

        assert result.status == "ok"
        assert len(result.data["file_tree"]) == MAX_TREE_FILES
        assert result.data["file_tree_total"] == 500
        assert result.data["file_tree_capped"] is True


# ---------------------------------------------------------------------------
# Test 8: File tree omitted when no reader
# ---------------------------------------------------------------------------


class TestFileTreeOmittedNoReader:
    @pytest.mark.asyncio
    async def test_no_file_tree_without_reader(self) -> None:
        """File tree is omitted when no manifest_reader provided."""
        snap = _sample_snapshot()
        lookup = StubSnapshotLookup(snapshots={"snap-001": snap})
        executor = WorkspaceSnapshotExecutor(snapshot_lookup=lookup)

        result = await executor.execute(_make_source("snap-001"), {})

        assert result.status == "ok"
        assert "file_tree" not in result.data


# ---------------------------------------------------------------------------
# Test 9: File tree omitted on reader failure (graceful)
# ---------------------------------------------------------------------------


class TestFileTreeReaderFailure:
    @pytest.mark.asyncio
    async def test_graceful_degradation_on_reader_error(self) -> None:
        """Reader failure → file_tree omitted, result still ok."""
        snap = _sample_snapshot(manifest_hash="hash-fail")
        lookup = StubSnapshotLookup(snapshots={"snap-001": snap})
        reader = StubManifestReader(error=RuntimeError("CAS unavailable"))
        executor = WorkspaceSnapshotExecutor(
            snapshot_lookup=lookup, manifest_reader=reader
        )

        result = await executor.execute(_make_source("snap-001"), {})

        assert result.status == "ok"
        assert "file_tree" not in result.data

    @pytest.mark.asyncio
    async def test_reader_returns_none_no_file_tree(self) -> None:
        """Reader returns None → file_tree omitted."""
        snap = _sample_snapshot(manifest_hash="hash-missing")
        lookup = StubSnapshotLookup(snapshots={"snap-001": snap})
        reader = StubManifestReader(paths={})  # hash not found → returns None
        executor = WorkspaceSnapshotExecutor(
            snapshot_lookup=lookup, manifest_reader=reader
        )

        result = await executor.execute(_make_source("snap-001"), {})

        assert result.status == "ok"
        assert "file_tree" not in result.data


# ---------------------------------------------------------------------------
# Test 10: Template variable in snapshot_id
# ---------------------------------------------------------------------------


class TestTemplateVariable:
    @pytest.mark.asyncio
    async def test_template_in_snapshot_id(self) -> None:
        """{{workspace.id}} in snapshot_id is resolved."""
        snap = _sample_snapshot(snapshot_id="ws-123-snap")
        lookup = StubSnapshotLookup(snapshots={"ws-123-snap": snap})
        executor = WorkspaceSnapshotExecutor(snapshot_lookup=lookup)

        variables = {"workspace.id": "ws-123-snap"}
        result = await executor.execute(
            _make_source("{{workspace.id}}"), variables
        )

        assert result.status == "ok"
        assert result.data["snapshot_id"] == "ws-123-snap"

    @pytest.mark.asyncio
    async def test_template_failure_returns_error(self) -> None:
        """Missing template variable → error."""
        lookup = StubSnapshotLookup()
        executor = WorkspaceSnapshotExecutor(snapshot_lookup=lookup)

        result = await executor.execute(
            _make_source("{{workspace.id}}"), {}
        )

        assert result.status == "error"
        assert "template" in result.error_message.lower()


# ---------------------------------------------------------------------------
# Test 11: elapsed_ms positive
# ---------------------------------------------------------------------------


class TestElapsedMs:
    @pytest.mark.asyncio
    async def test_elapsed_ms_positive(self) -> None:
        """elapsed_ms is always positive."""
        snap = _sample_snapshot()
        lookup = StubSnapshotLookup(snapshots={"snap-001": snap})
        executor = WorkspaceSnapshotExecutor(snapshot_lookup=lookup)

        result = await executor.execute(_make_source("snap-001"), {})

        assert result.elapsed_ms > 0


# ---------------------------------------------------------------------------
# Test 12: source_type and source_name correct
# ---------------------------------------------------------------------------


class TestSourceMetadata:
    @pytest.mark.asyncio
    async def test_source_type_correct(self) -> None:
        """source_type is 'workspace_snapshot'."""
        snap = _sample_snapshot()
        lookup = StubSnapshotLookup(snapshots={"snap-001": snap})
        executor = WorkspaceSnapshotExecutor(snapshot_lookup=lookup)

        result = await executor.execute(_make_source("snap-001"), {})

        assert result.source_type == "workspace_snapshot"

    @pytest.mark.asyncio
    async def test_source_name_is_snapshot_id(self) -> None:
        """source_name is the snapshot_id."""
        snap = _sample_snapshot()
        lookup = StubSnapshotLookup(snapshots={"snap-001": snap})
        executor = WorkspaceSnapshotExecutor(snapshot_lookup=lookup)

        result = await executor.execute(_make_source("snap-001"), {})

        assert result.source_name == "snap-001"
