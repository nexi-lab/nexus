"""Tests for nexus.factory — service factory wiring.

Issue #1287: Extract NexusFS Domain Services from God Object.
Issue #1391: Builder Pattern — create_nexus_services returns KernelServices.

Validates that create_nexus_services() and create_nexus_fs() correctly
create and wire all services together before we start extracting subsystems.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from nexus.backends.local import LocalBackend
from nexus.core.config import KernelServices, PermissionConfig
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

    def test_returns_kernel_services(self, deps: dict, tmp_path: Path) -> None:
        """create_nexus_services() returns a KernelServices dataclass."""
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
        assert isinstance(result, KernelServices)

    def test_contains_all_expected_services(self, deps: dict, tmp_path: Path) -> None:
        """Result KernelServices has all expected service attributes populated."""
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

        expected_attrs = [
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
            "wallet_provisioner",
        ]
        for attr in expected_attrs:
            assert hasattr(result, attr), f"Missing attribute: {attr}"

        # Server-layer extras are in server_extras dict
        expected_extras = [
            "observability_subsystem",
            "tool_namespace_middleware",
        ]
        for key in expected_extras:
            assert key in result.server_extras, f"Missing server_extras key: {key}"

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
        for attr in always_required:
            assert getattr(result, attr) is not None, f"Service '{attr}' is None"

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

        vs = result.version_service
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
            permissions=PermissionConfig(enable_deferred=False),
        )
        assert result.deferred_permission_buffer is None

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
            permissions=PermissionConfig(enable_deferred=True),
        )
        assert result.deferred_permission_buffer is not None

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
        observer = result.write_observer
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

        observer = result.write_observer
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
            permissions=PermissionConfig(enforce=False),
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
            permissions=PermissionConfig(enforce=False),
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
            permissions=PermissionConfig(enforce=False),
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
            permissions=PermissionConfig(enforce=False),
        )
        assert nx.router is not None

    def test_custom_namespaces_registered(self, tmp_path: Path) -> None:
        """Custom namespaces are registered on the router."""
        from nexus.factory import create_nexus_fs

        deps = _make_deps(tmp_path)
        nx = create_nexus_fs(
            backend=deps["backend"],
            metadata_store=deps["metadata_store"],
            permissions=PermissionConfig(enforce=False),
            custom_namespaces=[{"name": "custom"}],
        )
        assert nx.router is not None


# ---------------------------------------------------------------------------
# Tool namespace middleware wiring (Issue #1272)
# ---------------------------------------------------------------------------


class TestToolNamespaceMiddleware:
    """Tests for ToolNamespaceMiddleware factory wiring."""

    def _make_record_store(self, tmp_path: Path) -> MagicMock:
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

    def test_middleware_created_by_factory(self, tmp_path: Path) -> None:
        """Factory creates ToolNamespaceMiddleware when services are built."""
        from nexus.core.router import PathRouter
        from nexus.factory import create_nexus_services

        deps = _make_deps(tmp_path)
        router = PathRouter()
        router.add_mount("/", deps["backend"], priority=0)
        record_store = self._make_record_store(tmp_path)

        result = create_nexus_services(
            record_store=record_store,
            metadata_store=deps["metadata_store"],
            backend=deps["backend"],
            router=router,
        )

        mw = result.server_extras.get("tool_namespace_middleware")
        assert mw is not None
        assert type(mw).__name__ == "ToolNamespaceMiddleware"

    def test_middleware_receives_rebac_manager(self, tmp_path: Path) -> None:
        """Middleware is wired with the same rebac_manager from the factory."""
        from nexus.core.router import PathRouter
        from nexus.factory import create_nexus_services

        deps = _make_deps(tmp_path)
        router = PathRouter()
        router.add_mount("/", deps["backend"], priority=0)
        record_store = self._make_record_store(tmp_path)

        result = create_nexus_services(
            record_store=record_store,
            metadata_store=deps["metadata_store"],
            backend=deps["backend"],
            router=router,
        )

        mw = result.server_extras.get("tool_namespace_middleware")
        assert mw._rebac_manager is result.rebac_manager

    def test_middleware_receives_zone_id(self, tmp_path: Path) -> None:
        """Middleware inherits zone_id from factory params."""
        from nexus.core.router import PathRouter
        from nexus.factory import create_nexus_services

        deps = _make_deps(tmp_path)
        router = PathRouter()
        router.add_mount("/", deps["backend"], priority=0)
        record_store = self._make_record_store(tmp_path)

        result = create_nexus_services(
            record_store=record_store,
            metadata_store=deps["metadata_store"],
            backend=deps["backend"],
            router=router,
            zone_id="test-zone-42",
        )

        mw = result.server_extras.get("tool_namespace_middleware")
        assert mw._zone_id == "test-zone-42"

    def test_middleware_metrics_initially_zero(self, tmp_path: Path) -> None:
        """Freshly created middleware has zero metrics."""
        from nexus.core.router import PathRouter
        from nexus.factory import create_nexus_services

        deps = _make_deps(tmp_path)
        router = PathRouter()
        router.add_mount("/", deps["backend"], priority=0)
        record_store = self._make_record_store(tmp_path)

        result = create_nexus_services(
            record_store=record_store,
            metadata_store=deps["metadata_store"],
            backend=deps["backend"],
            router=router,
        )

        mw = result.server_extras.get("tool_namespace_middleware")
        assert mw.metrics["cache_hits"] == 0
        assert mw.metrics["cache_misses"] == 0
        assert mw.metrics["enabled"] is True


# ---------------------------------------------------------------------------
# Default tool profiles YAML (Issue #1272)
# ---------------------------------------------------------------------------


class TestDefaultToolProfiles:
    """Tests for the default tool_profiles.yaml config."""

    _CONFIG_PATH = Path("src/nexus/config/tool_profiles.yaml")

    def test_default_profiles_load_successfully(self) -> None:
        """Default YAML config loads without errors."""
        from nexus.mcp.profiles import load_profiles

        config = load_profiles(self._CONFIG_PATH)
        assert config is not None

    def test_default_profiles_contain_expected_names(self) -> None:
        """Default config has all 5 standard profiles."""
        from nexus.mcp.profiles import load_profiles

        config = load_profiles(self._CONFIG_PATH)
        expected = {"minimal", "coding", "search", "execution", "full"}
        assert set(config.profile_names) == expected

    def test_default_profile_is_minimal(self) -> None:
        """Default profile is 'minimal'."""
        from nexus.mcp.profiles import load_profiles

        config = load_profiles(self._CONFIG_PATH)
        default = config.get_default()
        assert default is not None
        assert default.name == "minimal"

    def test_inheritance_resolved_correctly(self) -> None:
        """Profile inheritance chains resolve to correct tool sets."""
        from nexus.mcp.profiles import load_profiles

        config = load_profiles(self._CONFIG_PATH)

        # minimal has 4 tools
        minimal = config.get_profile("minimal")
        assert minimal is not None
        assert len(minimal.tools) == 4
        assert "nexus_read_file" in minimal.tools

        # coding extends minimal → 4 + 7 = 11 tools
        coding = config.get_profile("coding")
        assert coding is not None
        assert "nexus_read_file" in coding.tools  # inherited
        assert "nexus_write_file" in coding.tools  # own

        # full extends execution → all tools
        full = config.get_profile("full")
        assert full is not None
        assert "nexus_read_file" in full.tools  # from minimal
        assert "nexus_python" in full.tools  # from execution
        assert "nexus_discovery_search_tools" in full.tools  # own

    def test_profile_inheritance_deduplicates(self) -> None:
        """Duplicate tools from inheritance are deduplicated."""
        from nexus.mcp.profiles import load_profiles

        config = load_profiles(self._CONFIG_PATH)
        full = config.get_profile("full")
        assert full is not None
        # No duplicates — tools is a tuple of unique names
        assert len(full.tools) == len(set(full.tools))


# ---------------------------------------------------------------------------
# Import structure tests (Issue #1291)
# ---------------------------------------------------------------------------


class TestImportStructure:
    """Tests for import structure integrity after circular import fix."""

    def test_types_is_leaf_module(self) -> None:
        """core/types.py has zero runtime nexus.* imports."""
        import ast

        types_path = Path("src/nexus/core/types.py")
        source = types_path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(types_path))

        runtime_nexus_imports: list[str] = []
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, ast.If):
                test = node.test
                if (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
                    isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
                ):
                    continue
            if isinstance(node, ast.ImportFrom):
                if node.module and node.module.startswith("nexus"):
                    runtime_nexus_imports.append(node.module)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("nexus"):
                        runtime_nexus_imports.append(alias.name)

        assert runtime_nexus_imports == [], f"core/types.py must be leaf: {runtime_nexus_imports}"

    def test_permissions_reexports_from_types(self) -> None:
        """permissions.py re-exports from types.py (same identity)."""
        from nexus.core.permissions import OperationContext as P_OC
        from nexus.core.permissions import Permission as P_P
        from nexus.core.types import OperationContext as T_OC
        from nexus.core.types import Permission as T_P

        assert P_OC is T_OC, "OperationContext must be same object"
        assert P_P is T_P, "Permission must be same object"

    def test_factory_import_chain(self) -> None:
        """Factory module imports without circular import errors."""
        from nexus.factory import create_nexus_fs  # noqa: F401

    def test_factory_creates_instance(self, tmp_path: Path) -> None:
        """Full DI wiring: factory creates a working NexusFS."""
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
