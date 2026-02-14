"""Integration tests for context manifest full pipeline (Issue #1341, #1427).

Tests the complete flow: create sources → resolve with stub executors →
verify files on disk → validate _index.json schema.

Issue #1427 adds FileGlobExecutor e2e and full pipeline with file_glob tests.
Issue #1428 adds WorkspaceSnapshot, MemoryQuery, and Metrics integration tests.

Uses tmp_path for filesystem and in-memory stubs for executors.
"""

from __future__ import annotations

import json
import types
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from nexus.core.agent_record import AgentRecord, AgentState
from nexus.services.context_manifest.models import (
    FileGlobSource,
    ManifestResult,
    MCPToolSource,
    MemoryQuerySource,
    SourceResult,
    WorkspaceSnapshotSource,
)
from nexus.services.context_manifest.resolver import ManifestResolver

# ---------------------------------------------------------------------------
# Stub executors
# ---------------------------------------------------------------------------


class IntegrationOkExecutor:
    """Returns realistic-looking data for integration testing."""

    def __init__(self, data_factory: Any = None) -> None:
        self._factory = data_factory

    async def execute(self, source: Any, variables: dict[str, str]) -> SourceResult:  # noqa: ARG002
        data = self._factory(source, variables) if self._factory else {"files": ["a.py", "b.py"]}
        return SourceResult(
            source_type=source.type,
            source_name=source.source_name,
            status="ok",
            data=data,
            elapsed_ms=5.0,
        )


def _make_all_ok_executors() -> dict[str, Any]:
    return {
        "mcp_tool": IntegrationOkExecutor(),
        "workspace_snapshot": IntegrationOkExecutor(
            data_factory=lambda s, v: {"snapshot_id": s.snapshot_id, "files": 42}
        ),
        "file_glob": IntegrationOkExecutor(
            data_factory=lambda s, v: {"pattern": s.pattern, "matches": ["a.py", "b.py"]}
        ),
        "memory_query": IntegrationOkExecutor(
            data_factory=lambda s, v: {"query": s.query, "chunks": ["chunk1", "chunk2"]}
        ),
    }


# ===========================================================================
# Test 1: Full pipeline — multi-source resolution
# ===========================================================================


class TestFullPipeline:
    @pytest.mark.asyncio
    async def test_multi_source_resolve_writes_files(self, tmp_path: Path) -> None:
        """Full pipeline: 4 sources → resolve → verify files on disk."""
        resolver = ManifestResolver(executors=_make_all_ok_executors(), max_resolve_seconds=10.0)
        sources = [
            FileGlobSource(pattern="src/**/*.py"),
            MemoryQuerySource(query="relevant to auth"),
            WorkspaceSnapshotSource(snapshot_id="latest"),
            MCPToolSource(tool_name="search_codebase", required=False),
        ]
        variables: dict[str, str] = {}

        result = await resolver.resolve(sources, variables, tmp_path)

        # Result structure
        assert isinstance(result, ManifestResult)
        assert len(result.sources) == 4
        assert all(r.status == "ok" for r in result.sources)
        assert result.total_ms > 0
        assert result.resolved_at  # non-empty

        # Files on disk
        assert (tmp_path / "_index.json").exists()
        result_files = [f for f in tmp_path.iterdir() if f.name != "_index.json"]
        assert len(result_files) == 4

        # Each result file is valid JSON
        for f in result_files:
            data = json.loads(f.read_text())
            assert "source_type" in data
            assert "status" in data
            assert data["status"] == "ok"


# ===========================================================================
# Test 2: _index.json schema validation
# ===========================================================================


class TestIndexJsonSchema:
    @pytest.mark.asyncio
    async def test_index_json_has_required_fields(self, tmp_path: Path) -> None:
        """Verify _index.json contains all required fields."""
        resolver = ManifestResolver(executors=_make_all_ok_executors())
        sources = [
            FileGlobSource(pattern="*.py"),
            MemoryQuerySource(query="test"),
        ]

        await resolver.resolve(sources, {}, tmp_path)

        index = json.loads((tmp_path / "_index.json").read_text())

        # Top-level fields
        assert "resolved_at" in index
        assert "total_ms" in index
        assert isinstance(index["total_ms"], float)
        assert "source_count" in index
        assert index["source_count"] == 2
        assert "sources" in index
        assert len(index["sources"]) == 2

        # Per-source fields
        for source_entry in index["sources"]:
            assert "source_type" in source_entry
            assert "source_name" in source_entry
            assert "status" in source_entry
            assert "file" in source_entry
            assert "elapsed_ms" in source_entry


# ===========================================================================
# Test 3: Truncation writes truncated content to disk
# ===========================================================================


class LargeDataExecutor:
    async def execute(self, source: Any, variables: dict[str, str]) -> SourceResult:  # noqa: ARG002
        return SourceResult(
            source_type=source.type,
            source_name=source.source_name,
            status="ok",
            data="A" * 500_000,  # 500KB
            elapsed_ms=3.0,
        )


class TestTruncationIntegration:
    @pytest.mark.asyncio
    async def test_truncated_result_written_to_disk(self, tmp_path: Path) -> None:
        """Truncation produces a file with truncated content and correct status."""
        executors = _make_all_ok_executors()
        executors["file_glob"] = LargeDataExecutor()
        resolver = ManifestResolver(executors=executors)
        sources = [FileGlobSource(pattern="*.py", max_result_bytes=1000, required=False)]

        result = await resolver.resolve(sources, {}, tmp_path)

        assert result.sources[0].status == "truncated"

        # Check the file on disk
        result_files = [f for f in tmp_path.iterdir() if f.name != "_index.json"]
        assert len(result_files) == 1
        data = json.loads(result_files[0].read_text())
        assert data["status"] == "truncated"


# ===========================================================================
# Test 4: ManifestResult timing is reasonable
# ===========================================================================


class TestTimingMetrics:
    @pytest.mark.asyncio
    async def test_total_ms_is_reasonable(self, tmp_path: Path) -> None:
        """total_ms should be positive and reasonable for stub executors."""
        resolver = ManifestResolver(executors=_make_all_ok_executors())
        sources = [FileGlobSource(pattern="*.py")]

        result = await resolver.resolve(sources, {}, tmp_path)

        # Stubs are fast — should be well under 1 second
        assert 0 < result.total_ms < 1000


# ===========================================================================
# Test 5: AgentRecord manifest round-trip
# ===========================================================================


class TestAgentRecordManifestRoundTrip:
    def test_serialize_and_deserialize_manifest(self) -> None:
        """AgentRecord stores manifest as tuple of dicts, round-trips correctly."""
        from pydantic import TypeAdapter

        from nexus.services.context_manifest.models import ContextSource

        # Create sources via Pydantic models
        sources = [
            FileGlobSource(pattern="src/**/*.py", max_files=20),
            MemoryQuerySource(query="relevant to {{task.description}}", top_k=5),
        ]

        # Serialize to dicts (what AgentRecord stores)
        manifest_data = tuple(s.model_dump() for s in sources)

        # Store on AgentRecord
        record = AgentRecord(
            agent_id="a1",
            owner_id="u1",
            zone_id="z1",
            name="test-agent",
            state=AgentState.UNKNOWN,
            generation=0,
            last_heartbeat=None,
            metadata=types.MappingProxyType({}),
            created_at=datetime.now(),
            updated_at=datetime.now(),
            context_manifest=manifest_data,
        )

        # Verify stored data
        assert len(record.context_manifest) == 2
        assert record.context_manifest[0]["type"] == "file_glob"
        assert record.context_manifest[1]["type"] == "memory_query"

        # Deserialize back to Pydantic models
        adapter = TypeAdapter(ContextSource)
        restored = [adapter.validate_python(d) for d in record.context_manifest]

        assert isinstance(restored[0], FileGlobSource)
        assert restored[0].pattern == "src/**/*.py"
        assert restored[0].max_files == 20
        assert isinstance(restored[1], MemoryQuerySource)
        assert restored[1].query == "relevant to {{task.description}}"
        assert restored[1].top_k == 5

    def test_default_empty_manifest(self) -> None:
        """AgentRecord with no manifest defaults to empty tuple."""
        record = AgentRecord(
            agent_id="a1",
            owner_id="u1",
            zone_id="z1",
            name="test-agent",
            state=AgentState.UNKNOWN,
            generation=0,
            last_heartbeat=None,
            metadata=types.MappingProxyType({}),
            created_at=datetime.now(),
            updated_at=datetime.now(),
        )
        assert record.context_manifest == ()


# ===========================================================================
# Test 6: FileGlobExecutor e2e (Issue #1427)
# ===========================================================================


class TestFileGlobExecutorE2E:
    @pytest.mark.asyncio
    async def test_file_glob_executor_e2e(self, tmp_path: Path) -> None:
        """Create temp files → FileGlobExecutor → verify file contents in result."""
        from nexus.services.context_manifest.executors.file_glob import FileGlobExecutor

        # Create workspace with files
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "main.py").write_text("print('main')")
        (workspace / "utils.py").write_text("def helper(): pass")
        (workspace / "readme.md").write_text("# Readme")

        executor = FileGlobExecutor(workspace_root=workspace)
        source = FileGlobSource(pattern="*.py", max_files=50)
        result = await executor.execute(source, {})

        assert result.status == "ok"
        assert result.data["total_matched"] == 2
        assert result.data["returned"] == 2
        assert "main.py" in result.data["files"]
        assert "utils.py" in result.data["files"]
        assert "readme.md" not in result.data["files"]  # .md excluded by *.py pattern
        assert result.data["files"]["main.py"] == "print('main')"


# ===========================================================================
# Test 7: Full pipeline with real FileGlobExecutor (Issue #1427)
# ===========================================================================


class TestFullPipelineWithFileGlob:
    @pytest.mark.asyncio
    async def test_full_pipeline_with_file_glob(self, tmp_path: Path) -> None:
        """Set manifest on agent → resolve with real FileGlobExecutor → verify."""
        from nexus.services.context_manifest.executors.file_glob import FileGlobExecutor

        # Create workspace with files
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "app.py").write_text("import os")
        (workspace / "test.py").write_text("def test(): pass")

        output_dir = tmp_path / "output"

        # Build resolver with real FileGlobExecutor + stub for other types
        executors: dict[str, Any] = {
            "file_glob": FileGlobExecutor(workspace_root=workspace),
            "memory_query": IntegrationOkExecutor(
                data_factory=lambda s, v: {"query": s.query, "chunks": ["c1"]}
            ),
        }
        resolver = ManifestResolver(executors=executors, max_resolve_seconds=10.0)

        sources = [
            FileGlobSource(pattern="*.py"),
            MemoryQuerySource(query="test context", required=False),
        ]

        result = await resolver.resolve(sources, {}, output_dir)

        assert isinstance(result, ManifestResult)
        assert len(result.sources) == 2
        assert all(r.status == "ok" for r in result.sources)

        # Verify file_glob result contains actual file contents
        glob_result = result.sources[0]
        assert glob_result.source_type == "file_glob"
        assert glob_result.data["returned"] == 2
        assert "app.py" in glob_result.data["files"]

        # Verify _index.json written
        assert (output_dir / "_index.json").exists()
        index = json.loads((output_dir / "_index.json").read_text())
        assert index["source_count"] == 2


# ===========================================================================
# Test 8: WorkspaceSnapshotExecutor with StubSnapshotLookup (Issue #1428)
# ===========================================================================


class StubSnapshotLookup:
    """Stub SnapshotLookup for integration testing."""

    def __init__(self, snapshots: dict[str, dict[str, Any]] | None = None) -> None:
        self._snapshots = snapshots or {}

    def get_snapshot(self, snapshot_id: str) -> dict[str, Any] | None:
        return self._snapshots.get(snapshot_id)

    def get_latest_snapshot(self, workspace_path: str) -> dict[str, Any] | None:
        return None


class TestWorkspaceSnapshotExecutorInPipeline:
    @pytest.mark.asyncio
    async def test_workspace_snapshot_in_full_pipeline(self, tmp_path: Path) -> None:
        """WorkspaceSnapshotExecutor wired into full resolver pipeline."""
        from nexus.services.context_manifest.executors.workspace_snapshot import (
            WorkspaceSnapshotExecutor,
        )

        snapshot_data = {
            "snapshot_id": "snap-int-001",
            "workspace_path": "/ws",
            "snapshot_number": 1,
            "manifest_hash": "abc",
            "file_count": 5,
            "total_size_bytes": 1000,
            "description": "test",
            "created_by": "user",
            "tags": [],
            "created_at": "2025-01-15T10:00:00",
        }
        lookup = StubSnapshotLookup(snapshots={"snap-int-001": snapshot_data})
        executor = WorkspaceSnapshotExecutor(snapshot_lookup=lookup)

        resolver = ManifestResolver(
            executors={"workspace_snapshot": executor},
            max_resolve_seconds=10.0,
        )
        sources = [WorkspaceSnapshotSource(snapshot_id="snap-int-001")]

        result = await resolver.resolve(sources, {}, tmp_path)

        assert isinstance(result, ManifestResult)
        assert len(result.sources) == 1
        assert result.sources[0].status == "ok"
        assert result.sources[0].data["snapshot_id"] == "snap-int-001"
        assert result.sources[0].data["file_count"] == 5


# ===========================================================================
# Test 9: MemoryQueryExecutor with StubMemorySearch (Issue #1428)
# ===========================================================================


class StubMemorySearch:
    """Stub MemorySearch for integration testing."""

    def search(
        self, query: str, top_k: int, search_mode: str
    ) -> tuple[list[dict[str, Any]], str]:
        return [
            {"content": "test result", "score": 0.9, "memory_type": "fact"},
        ], search_mode


class TestMemoryQueryExecutorInPipeline:
    @pytest.mark.asyncio
    async def test_memory_query_in_full_pipeline(self, tmp_path: Path) -> None:
        """MemoryQueryExecutor wired into full resolver pipeline."""
        from nexus.services.context_manifest.executors.memory_query import (
            MemoryQueryExecutor,
        )

        executor = MemoryQueryExecutor(memory_search=StubMemorySearch())
        resolver = ManifestResolver(
            executors={"memory_query": executor},
            max_resolve_seconds=10.0,
        )
        sources = [MemoryQuerySource(query="find auth patterns")]

        result = await resolver.resolve(sources, {}, tmp_path)

        assert isinstance(result, ManifestResult)
        assert len(result.sources) == 1
        assert result.sources[0].status == "ok"
        assert result.sources[0].data["total"] == 1
        assert result.sources[0].data["search_mode"] == "hybrid"


# ===========================================================================
# Test 10: MetricsObserver wired to resolver (Issue #1428)
# ===========================================================================


class TestMetricsObserverInPipeline:
    @pytest.mark.asyncio
    async def test_metrics_observer_wired(self, tmp_path: Path) -> None:
        """MetricsObserver receives hooks from resolver during resolution."""
        from nexus.services.context_manifest.metrics import ManifestMetricsObserver

        observer = ManifestMetricsObserver()
        resolver = ManifestResolver(
            executors=_make_all_ok_executors(),
            max_resolve_seconds=10.0,
            metrics_observer=observer,
        )
        sources = [
            FileGlobSource(pattern="*.py"),
            MemoryQuerySource(query="test"),
        ]

        await resolver.resolve(sources, {}, tmp_path)

        snap = observer.snapshot()
        assert snap["total_resolutions"] == 1
        assert snap["total_source_executions"] == 2
        assert snap["active_resolutions"] == 0
