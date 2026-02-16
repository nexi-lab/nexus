"""Tests for factory wiring of context manifest components (Issue #1428: 9A).

Verifies that the factory creates a correctly wired ManifestResolver
with all expected executors, and that graceful degradation works.

These tests exercise the same construction logic as factory.py lines 530-593,
using real imports and mock dependencies.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from nexus.services.context_manifest.executors.file_glob import FileGlobExecutor
from nexus.services.context_manifest.executors.memory_query import MemoryQueryExecutor
from nexus.services.context_manifest.executors.workspace_snapshot import (
    WorkspaceSnapshotExecutor,
)
from nexus.services.context_manifest.metrics import (
    ManifestMetricsConfig,
    ManifestMetricsObserver,
)
from nexus.services.context_manifest.resolver import ManifestResolver

# ---------------------------------------------------------------------------
# Test 1: FileGlobExecutor creation with valid root_path
# ---------------------------------------------------------------------------


class TestFileGlobExecutorWiring:
    def test_creates_with_valid_path(self, tmp_path: Path) -> None:
        """FileGlobExecutor is created when root_path is a valid directory."""
        executor = FileGlobExecutor(workspace_root=tmp_path)
        assert executor is not None
        assert executor._workspace_root == tmp_path.resolve()

    def test_creates_with_string_path(self, tmp_path: Path) -> None:
        """FileGlobExecutor accepts Path objects."""
        executor = FileGlobExecutor(workspace_root=Path(str(tmp_path)))
        assert executor._workspace_root == tmp_path.resolve()


# ---------------------------------------------------------------------------
# Test 2: WorkspaceSnapshotExecutor creation
# ---------------------------------------------------------------------------


class TestWorkspaceSnapshotExecutorWiring:
    def test_creates_with_session_factory_and_backend(self) -> None:
        """WorkspaceSnapshotExecutor is created with session_factory + CAS backend."""
        from nexus.services.context_manifest.executors.snapshot_lookup_db import (
            CASManifestReader,
            DatabaseSnapshotLookup,
        )

        mock_session_factory = MagicMock()
        mock_backend = MagicMock()

        lookup = DatabaseSnapshotLookup(session_factory=mock_session_factory)
        reader = CASManifestReader(backend=mock_backend)
        executor = WorkspaceSnapshotExecutor(snapshot_lookup=lookup, manifest_reader=reader)

        assert executor is not None
        assert executor._snapshot_lookup is lookup
        assert executor._manifest_reader is reader


# ---------------------------------------------------------------------------
# Test 3: MemoryQueryExecutor creation
# ---------------------------------------------------------------------------


class TestMemoryQueryExecutorWiring:
    def test_creates_with_memory_search(self) -> None:
        """MemoryQueryExecutor is created with a MemorySearch implementation."""
        from nexus.services.context_manifest.executors.memory_search_adapter import (
            MemorySearchAdapter,
        )

        mock_memory = MagicMock()
        adapter = MemorySearchAdapter(memory=mock_memory)
        executor = MemoryQueryExecutor(memory_search=adapter)

        assert executor is not None
        assert executor._memory_search is adapter


# ---------------------------------------------------------------------------
# Test 4: ManifestResolver creation with executors
# ---------------------------------------------------------------------------


class TestResolverWiring:
    def test_creates_with_executors_and_metrics(self, tmp_path: Path) -> None:
        """ManifestResolver is created with executors dict and metrics observer."""
        executors: dict[str, Any] = {
            "file_glob": FileGlobExecutor(workspace_root=tmp_path),
        }
        metrics = ManifestMetricsObserver(ManifestMetricsConfig())

        resolver = ManifestResolver(
            executors=executors,
            max_resolve_seconds=5.0,
            metrics_observer=metrics,
        )

        assert resolver is not None
        assert resolver._max_resolve_seconds == 5.0
        assert resolver._metrics is metrics
        assert "file_glob" in resolver._executors

    def test_defensive_copy_of_executors(self, tmp_path: Path) -> None:
        """Resolver makes a defensive copy of executors dict."""
        executors: dict[str, Any] = {
            "file_glob": FileGlobExecutor(workspace_root=tmp_path),
        }
        resolver = ManifestResolver(executors=executors)

        # Mutating original dict does not affect resolver
        executors["new_type"] = MagicMock()
        assert "new_type" not in resolver._executors

    def test_rejects_non_positive_timeout(self) -> None:
        """Resolver rejects non-positive max_resolve_seconds."""
        with pytest.raises(ValueError, match="must be positive"):
            ManifestResolver(executors={}, max_resolve_seconds=0)

        with pytest.raises(ValueError, match="must be positive"):
            ManifestResolver(executors={}, max_resolve_seconds=-1.0)


# ---------------------------------------------------------------------------
# Test 5: Full wiring simulation (mimics factory.py)
# ---------------------------------------------------------------------------


class TestFullWiringSimulation:
    def test_factory_like_wiring(self, tmp_path: Path) -> None:
        """Simulate the full factory wiring path: create all executors and resolver."""
        from nexus.services.context_manifest.executors.snapshot_lookup_db import (
            CASManifestReader,
            DatabaseSnapshotLookup,
        )

        # Simulate factory inputs
        root_path = str(tmp_path)
        session_factory = MagicMock()
        backend = MagicMock()

        # Build executors dict (same logic as factory.py)
        executors: dict[str, Any] = {}

        # FileGlobExecutor
        executors["file_glob"] = FileGlobExecutor(workspace_root=Path(root_path))

        # WorkspaceSnapshotExecutor
        snapshot_lookup = DatabaseSnapshotLookup(session_factory=session_factory)
        cas_reader = CASManifestReader(backend=backend)
        executors["workspace_snapshot"] = WorkspaceSnapshotExecutor(
            snapshot_lookup=snapshot_lookup,
            manifest_reader=cas_reader,
        )

        # Metrics
        metrics = ManifestMetricsObserver(ManifestMetricsConfig())

        # Resolver
        resolver = ManifestResolver(
            executors=executors,
            max_resolve_seconds=5.0,
            metrics_observer=metrics,
        )

        assert len(resolver._executors) == 2
        assert "file_glob" in resolver._executors
        assert "workspace_snapshot" in resolver._executors
        assert resolver._metrics is metrics

    def test_graceful_degradation_without_workspace_snapshot(self, tmp_path: Path) -> None:
        """Resolver works fine with only FileGlobExecutor (no snapshot module)."""
        executors: dict[str, Any] = {
            "file_glob": FileGlobExecutor(workspace_root=tmp_path),
        }
        resolver = ManifestResolver(executors=executors)

        assert len(resolver._executors) == 1
        assert "file_glob" in resolver._executors

    def test_graceful_degradation_no_executors(self) -> None:
        """Resolver works with empty executors dict (all sources will be skipped)."""
        resolver = ManifestResolver(executors={})
        assert len(resolver._executors) == 0
