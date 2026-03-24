"""Tests for LifespanServices.from_app() extraction (Issue #2135).

Validates that the typed container correctly extracts all factory-produced
services from app.state and NexusFS internals, and handles missing/None
values gracefully.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

from nexus.server.lifespan.services_container import LifespanServices

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_app(**state_attrs) -> MagicMock:
    """Create a minimal FastAPI-like app stub with given state attributes."""
    app = MagicMock()
    app.state = SimpleNamespace(**state_attrs)
    return app


def _make_nexus_fs(**attrs) -> SimpleNamespace:
    """Create a NexusFS stub with given attributes."""
    defaults = {
        "_system_services": None,
        "_brick_services": None,
        "SessionLocal": None,
        "_sql_engine": None,
        "_entity_registry": None,
        "_permission_enforcer": None,
        "_rebac_manager": None,
        "_event_bus": None,
        "_coordination_client": None,
        "workflow_engine": None,
        "_snapshot_service": None,
        "_namespace_manager": None,
        "config": None,
        "service_coordinator": None,
    }
    defaults.update(attrs)
    return SimpleNamespace(**defaults)


# ---------------------------------------------------------------------------
# from_app() — core extraction
# ---------------------------------------------------------------------------


class TestFromAppExtraction:
    """Test that from_app() extracts all factory services."""

    def test_bare_app_produces_all_none(self) -> None:
        """App with no nexus_fs yields container with all None."""
        app = _make_app()
        svc = LifespanServices.from_app(app)

        assert svc.nexus_fs is None
        assert svc.database_url is None
        assert svc.record_store is None
        assert svc.brick_lifecycle_manager is None
        assert svc.brick_reconciler is None
        assert svc.event_bus is None
        assert svc.rebac_manager is None

    def test_extracts_top_level_state(self) -> None:
        """Top-level app.state attributes are extracted."""
        app = _make_app(
            nexus_fs=None,
            database_url="postgresql://localhost/test",
            record_store="fake_rs",
            zone_id="zone-42",
            deployment_profile="lite",
            deployment_mode="federated",
            profile_tuning="fake_tuning",
            thread_pool_size=80,
        )
        svc = LifespanServices.from_app(app)

        assert svc.database_url == "postgresql://localhost/test"
        assert svc.record_store == "fake_rs"
        assert svc.zone_id == "zone-42"
        assert svc.deployment_profile == "lite"
        assert svc.deployment_mode == "federated"
        assert svc.profile_tuning == "fake_tuning"
        assert svc.thread_pool_size == 80

    def test_extracts_nexus_fs_internals(self) -> None:
        """NexusFS private attributes are extracted."""
        nx = _make_nexus_fs(
            SessionLocal="session_factory",
            _sql_engine="sql_engine",
            _entity_registry="entity_reg",
            _permission_enforcer="perm_enf",
            _rebac_manager="rebac_mgr",
            _event_bus="event_bus",
            _coordination_client="coord_client",
            workflow_engine="wf_engine",
            _snapshot_service="snap_svc",
            _namespace_manager="ns_mgr",
            config="nexus_cfg",
        )
        app = _make_app(nexus_fs=nx)
        svc = LifespanServices.from_app(app)

        assert svc.nexus_fs is nx
        assert svc.session_factory == "session_factory"
        assert svc.sql_engine == "sql_engine"
        assert svc.entity_registry == "entity_reg"
        assert svc.permission_enforcer == "perm_enf"
        assert svc.rebac_manager == "rebac_mgr"
        assert svc.event_bus == "event_bus"
        assert svc.coordination_client == "coord_client"
        assert svc.workflow_engine == "wf_engine"
        assert svc.snapshot_service == "snap_svc"
        assert svc.namespace_manager == "ns_mgr"
        assert svc.nexus_config == "nexus_cfg"


class TestFromAppSystemServices:
    """Test extraction from nexus_fs._system_services."""

    def test_extracts_system_services(self) -> None:
        """System services (brick_lifecycle_manager, etc.) are extracted."""
        sys_svc = SimpleNamespace(
            brick_lifecycle_manager="blm",
            brick_reconciler="br",
            eviction_manager="em",
            write_observer="write_obs",
            zone_lifecycle="zl",
        )
        nx = _make_nexus_fs(_system_services=sys_svc, _pipe_manager="pipe_mgr")
        app = _make_app(nexus_fs=nx)
        svc = LifespanServices.from_app(app)

        assert svc.brick_lifecycle_manager == "blm"
        assert svc.brick_reconciler == "br"
        assert svc.eviction_manager == "em"
        assert svc.write_observer == "write_obs"
        assert svc.zone_lifecycle == "zl"
        assert svc.pipe_manager == "pipe_mgr"

    def test_missing_system_services_yields_none(self) -> None:
        """When _system_services is None, all system service fields are None."""
        nx = _make_nexus_fs(_system_services=None)
        app = _make_app(nexus_fs=nx)
        svc = LifespanServices.from_app(app)

        assert svc.brick_lifecycle_manager is None
        assert svc.brick_reconciler is None
        assert svc.eviction_manager is None


class TestFromAppBrickServices:
    """Test extraction from nexus_fs._brick_services."""

    def test_extracts_brick_services_container(self) -> None:
        """The entire BrickServices object is captured."""
        brk = SimpleNamespace(cache_brick="cb", ipc_storage_driver="ipc")
        nx = _make_nexus_fs(_brick_services=brk)
        app = _make_app(nexus_fs=nx)
        svc = LifespanServices.from_app(app)

        assert svc.brick_services is brk

    def test_missing_brick_services_is_none(self) -> None:
        """When _brick_services is None, field is None."""
        nx = _make_nexus_fs(_brick_services=None)
        app = _make_app(nexus_fs=nx)
        svc = LifespanServices.from_app(app)

        assert svc.brick_services is None


class TestFromAppObservability:
    """Test extraction of observability_subsystem from _system_services."""

    def test_extracts_observability_subsystem(self) -> None:
        """observability_subsystem extracted from _system_services."""
        sys_svc = SimpleNamespace(observability_subsystem="obs_sub")
        nx = _make_nexus_fs(_system_services=sys_svc)
        app = _make_app(nexus_fs=nx)
        svc = LifespanServices.from_app(app)

        assert svc.observability_subsystem == "obs_sub"

    def test_missing_system_services_yields_none(self) -> None:
        """When _system_services is None, observability_subsystem is None."""
        nx = _make_nexus_fs(_system_services=None)
        app = _make_app(nexus_fs=nx)
        svc = LifespanServices.from_app(app)

        assert svc.observability_subsystem is None


class TestFromAppDefaults:
    """Test default values when attributes are absent from app.state."""

    def test_deployment_profile_defaults_to_full(self) -> None:
        app = _make_app()
        svc = LifespanServices.from_app(app)
        assert svc.deployment_profile == "full"

    def test_deployment_mode_defaults_to_standalone(self) -> None:
        app = _make_app()
        svc = LifespanServices.from_app(app)
        assert svc.deployment_mode == "standalone"

    def test_thread_pool_size_defaults_to_40(self) -> None:
        app = _make_app()
        svc = LifespanServices.from_app(app)
        assert svc.thread_pool_size == 40

    def test_enabled_bricks_defaults_to_empty_frozenset(self) -> None:
        app = _make_app()
        svc = LifespanServices.from_app(app)
        assert svc.enabled_bricks == frozenset()


class TestFromAppEdgeCases:
    """Edge cases for from_app() robustness."""

    def test_nexus_fs_without_optional_attributes(self) -> None:
        """NexusFS missing some private attrs doesn't crash."""
        # Use a SimpleNamespace with only _system_services
        nx = SimpleNamespace(_system_services=None, _brick_services=None)
        app = _make_app(nexus_fs=nx)

        # Should not raise even though nx has no SessionLocal, etc.
        svc = LifespanServices.from_app(app)
        assert svc.nexus_fs is nx
        assert svc.write_observer is None  # extracted from _system_services (None here)
        assert svc.rebac_manager is None

    def test_observability_registry_extracted(self) -> None:
        """observability_registry from app.state is extracted."""
        app = _make_app(observability_registry="obs_reg")
        svc = LifespanServices.from_app(app)
        assert svc.observability_registry == "obs_reg"
