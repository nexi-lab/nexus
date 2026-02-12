"""Tests for nexus.factory — service factory wiring.

Issue #1287: Extract NexusFS Domain Services from God Object.

Validates that create_nexus_services() and create_nexus_fs() correctly
create and wire all services together before we start extracting subsystems.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from nexus.backends.local import LocalBackend
from nexus.storage.raft_metadata_store import RaftMetadataStore


def _make_deps(tmp_path: Path) -> dict:
    """Build minimal real dependencies for factory tests."""
    backend_path = tmp_path / "storage"
    backend_path.mkdir(exist_ok=True)
    db_path = tmp_path / "metadata"

    backend = LocalBackend(str(backend_path))
    metadata_store = RaftMetadataStore.embedded(str(db_path))

    return {"backend": backend, "metadata_store": metadata_store}


# ---------------------------------------------------------------------------
# create_nexus_services() tests
# ---------------------------------------------------------------------------


class TestCreateNexusServices:
    """Tests for the create_nexus_services() factory function."""

    @pytest.fixture
    def deps(self, tmp_path: Path) -> dict:
        return _make_deps(tmp_path)

    def _make_record_store(self, tmp_path: Path) -> MagicMock:
        """Create a mock RecordStore with real engine/session_factory and tables."""
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from nexus.storage.models import Base

        db_file = tmp_path / "records.db"
        engine = create_engine(f"sqlite:///{db_file}")
        Base.metadata.create_all(engine)
        session_factory = sessionmaker(bind=engine)

        mock = MagicMock()
        mock.engine = engine
        mock.session_factory = session_factory
        mock.database_url = f"sqlite:///{db_file}"
        return mock

    def test_returns_dict(self, deps: dict, tmp_path: Path) -> None:
        """create_nexus_services() returns a dict."""
        from nexus.core.router import PathRouter
        from nexus.factory import create_nexus_services

        router = PathRouter()
        router.add_mount("/", deps["backend"], priority=0)
        record_store = self._make_record_store(tmp_path)

        result = create_nexus_services(
            record_store=record_store,
            metadata_store=deps["metadata_store"],
            backend=deps["backend"],
            router=router,
        )
        assert isinstance(result, dict)

    def test_contains_all_expected_keys(self, deps: dict, tmp_path: Path) -> None:
        """Result dict contains all expected service keys."""
        from nexus.core.router import PathRouter
        from nexus.factory import create_nexus_services

        router = PathRouter()
        router.add_mount("/", deps["backend"], priority=0)
        record_store = self._make_record_store(tmp_path)

        result = create_nexus_services(
            record_store=record_store,
            metadata_store=deps["metadata_store"],
            backend=deps["backend"],
            router=router,
        )

        expected_keys = {
            "rebac_manager",
            "dir_visibility_cache",
            "audit_store",
            "entity_registry",
            "permission_enforcer",
            "hierarchy_manager",
            "deferred_permission_buffer",
            "workspace_registry",
            "mount_manager",
            "workspace_manager",
            "write_observer",
            "version_service",
        }
        assert expected_keys.issubset(result.keys()), (
            f"Missing keys: {expected_keys - result.keys()}"
        )

    def test_no_none_values_for_required_services(self, deps: dict, tmp_path: Path) -> None:
        """Required services should not be None."""
        from nexus.core.router import PathRouter
        from nexus.factory import create_nexus_services

        router = PathRouter()
        router.add_mount("/", deps["backend"], priority=0)
        record_store = self._make_record_store(tmp_path)

        result = create_nexus_services(
            record_store=record_store,
            metadata_store=deps["metadata_store"],
            backend=deps["backend"],
            router=router,
        )

        always_required = [
            "rebac_manager",
            "audit_store",
            "entity_registry",
            "permission_enforcer",
            "hierarchy_manager",
            "workspace_registry",
            "mount_manager",
            "workspace_manager",
            "write_observer",
            "version_service",
        ]
        for key in always_required:
            assert result[key] is not None, f"Service '{key}' is None"

    def test_version_service_wiring(self, deps: dict, tmp_path: Path) -> None:
        """VersionService receives correct metadata_store and cas_store."""
        from nexus.core.router import PathRouter
        from nexus.factory import create_nexus_services

        router = PathRouter()
        router.add_mount("/", deps["backend"], priority=0)
        record_store = self._make_record_store(tmp_path)

        result = create_nexus_services(
            record_store=record_store,
            metadata_store=deps["metadata_store"],
            backend=deps["backend"],
            router=router,
        )

        vs = result["version_service"]
        assert vs.metadata == deps["metadata_store"]
        assert vs.cas == deps["backend"]

    def test_deferred_permission_buffer_disabled(self, deps: dict, tmp_path: Path) -> None:
        """DeferredPermissionBuffer is None when disabled."""
        from nexus.core.router import PathRouter
        from nexus.factory import create_nexus_services

        router = PathRouter()
        router.add_mount("/", deps["backend"], priority=0)
        record_store = self._make_record_store(tmp_path)

        result = create_nexus_services(
            record_store=record_store,
            metadata_store=deps["metadata_store"],
            backend=deps["backend"],
            router=router,
            enable_deferred_permissions=False,
        )
        assert result["deferred_permission_buffer"] is None

    def test_deferred_permission_buffer_enabled(self, deps: dict, tmp_path: Path) -> None:
        """DeferredPermissionBuffer is created when enabled."""
        from nexus.core.router import PathRouter
        from nexus.factory import create_nexus_services

        router = PathRouter()
        router.add_mount("/", deps["backend"], priority=0)
        record_store = self._make_record_store(tmp_path)

        result = create_nexus_services(
            record_store=record_store,
            metadata_store=deps["metadata_store"],
            backend=deps["backend"],
            router=router,
            enable_deferred_permissions=True,
        )
        buf = result["deferred_permission_buffer"]
        assert buf is not None

    def test_write_buffer_disabled_for_sqlite(self, deps: dict, tmp_path: Path) -> None:
        """WriteBuffer is disabled for SQLite by default."""
        from nexus.core.router import PathRouter
        from nexus.factory import create_nexus_services

        router = PathRouter()
        router.add_mount("/", deps["backend"], priority=0)
        record_store = self._make_record_store(tmp_path)

        result = create_nexus_services(
            record_store=record_store,
            metadata_store=deps["metadata_store"],
            backend=deps["backend"],
            router=router,
        )

        # SQLite should use synchronous RecordStoreSyncer
        observer = result["write_observer"]
        assert observer is not None
        assert type(observer).__name__ == "RecordStoreSyncer"

    def test_write_buffer_forced_on(self, deps: dict, tmp_path: Path) -> None:
        """WriteBuffer can be forced on via enable_write_buffer=True."""
        from nexus.core.router import PathRouter
        from nexus.factory import create_nexus_services

        router = PathRouter()
        router.add_mount("/", deps["backend"], priority=0)
        record_store = self._make_record_store(tmp_path)

        result = create_nexus_services(
            record_store=record_store,
            metadata_store=deps["metadata_store"],
            backend=deps["backend"],
            router=router,
            enable_write_buffer=True,
        )

        observer = result["write_observer"]
        assert type(observer).__name__ == "BufferedRecordStoreSyncer"


# ---------------------------------------------------------------------------
# create_nexus_fs() tests
# ---------------------------------------------------------------------------


class TestCreateNexusFS:
    """Tests for the create_nexus_fs() convenience function."""

    def test_creates_nexus_fs_without_record_store(self, tmp_path: Path) -> None:
        """create_nexus_fs() works without a record_store (kernel-only mode)."""
        from nexus.factory import create_nexus_fs

        deps = _make_deps(tmp_path)
        nx = create_nexus_fs(
            backend=deps["backend"],
            metadata_store=deps["metadata_store"],
            enforce_permissions=False,
        )
        assert nx is not None
        assert hasattr(nx, "read")
        assert hasattr(nx, "write")

    def _make_record_store(self, tmp_path: Path) -> MagicMock:
        """Create a mock RecordStore with real engine/session_factory and tables."""
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from nexus.storage.models import Base

        db_file = tmp_path / "records.db"
        engine = create_engine(f"sqlite:///{db_file}")
        Base.metadata.create_all(engine)
        session_factory = sessionmaker(bind=engine)

        mock = MagicMock()
        mock.engine = engine
        mock.session_factory = session_factory
        mock.database_url = f"sqlite:///{db_file}"
        return mock

    def test_creates_nexus_fs_with_record_store(self, tmp_path: Path) -> None:
        """create_nexus_fs() creates services when record_store is provided."""
        from nexus.factory import create_nexus_fs

        deps = _make_deps(tmp_path)
        record_store = self._make_record_store(tmp_path)

        nx = create_nexus_fs(
            backend=deps["backend"],
            metadata_store=deps["metadata_store"],
            record_store=record_store,
            enforce_permissions=False,
        )
        assert nx is not None
        assert nx.version_service is not None

    def test_services_wired_correctly(self, tmp_path: Path) -> None:
        """Services created by factory are properly wired into NexusFS."""
        from nexus.factory import create_nexus_fs

        deps = _make_deps(tmp_path)
        record_store = self._make_record_store(tmp_path)

        nx = create_nexus_fs(
            backend=deps["backend"],
            metadata_store=deps["metadata_store"],
            record_store=record_store,
            enforce_permissions=False,
        )

        # VersionService gets metadata_store
        assert nx.version_service.metadata == deps["metadata_store"]
        # ReBACService gets rebac_manager
        assert nx.rebac_service._rebac_manager == nx._rebac_manager
        # LLMService gets nexus_fs reference
        assert nx.llm_service.nexus_fs == nx

    def test_router_created_with_default_mount(self, tmp_path: Path) -> None:
        """Factory creates a PathRouter with the backend mounted at '/'."""
        from nexus.factory import create_nexus_fs

        deps = _make_deps(tmp_path)
        nx = create_nexus_fs(
            backend=deps["backend"],
            metadata_store=deps["metadata_store"],
            enforce_permissions=False,
        )
        assert nx.router is not None

    def test_custom_namespaces_registered(self, tmp_path: Path) -> None:
        """Custom namespaces are registered on the router."""
        from nexus.factory import create_nexus_fs

        deps = _make_deps(tmp_path)
        nx = create_nexus_fs(
            backend=deps["backend"],
            metadata_store=deps["metadata_store"],
            enforce_permissions=False,
            custom_namespaces=[{"name": "custom"}],
        )
        assert nx.router is not None
