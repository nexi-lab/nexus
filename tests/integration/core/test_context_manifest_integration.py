"""Integration tests for context manifest full pipeline (Issue #1341).

Tests the complete flow: create sources → resolve with stub executors →
verify files on disk → validate _index.json schema.

Uses tmp_path for filesystem and in-memory stubs for executors.
"""

from __future__ import annotations

import asyncio
import json
import types
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from nexus.core.agent_record import AgentRecord, AgentState
from nexus.core.context_manifest.models import (
    FileGlobSource,
    MCPToolSource,
    ManifestResult,
    MemoryQuerySource,
    SourceResult,
    WorkspaceSnapshotSource,
)
from nexus.core.context_manifest.resolver import ManifestResolver


# ---------------------------------------------------------------------------
# Stub executors
# ---------------------------------------------------------------------------


class IntegrationOkExecutor:
    """Returns realistic-looking data for integration testing."""

    def __init__(self, data_factory: Any = None) -> None:
        self._factory = data_factory

    async def execute(self, source: Any, variables: dict[str, str]) -> SourceResult:
        name = _name(source)
        data = self._factory(source, variables) if self._factory else {"files": ["a.py", "b.py"]}
        return SourceResult(
            source_type=source.type,
            source_name=name,
            status="ok",
            data=data,
            elapsed_ms=5.0,
        )


def _name(source: Any) -> str:
    for attr in ("tool_name", "snapshot_id", "pattern", "query"):
        v = getattr(source, attr, None)
        if v is not None:
            return str(v)
    return "unknown"


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
        resolver = ManifestResolver(
            executors=_make_all_ok_executors(), max_resolve_seconds=10.0
        )
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
    async def execute(self, source: Any, variables: dict[str, str]) -> SourceResult:
        name = _name(source)
        return SourceResult(
            source_type=source.type,
            source_name=name,
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

        from nexus.core.context_manifest.models import ContextSource

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
