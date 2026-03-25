"""Tests for NexusAppState dataclass and init_app_state() helper.

Issue #2135: Typed app.state initialization.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

from nexus.server.app_state import NexusAppState, init_app_state


def _make_app() -> MagicMock:
    """Create a minimal FastAPI-like app stub with state."""
    app = MagicMock()
    app.state = SimpleNamespace()
    return app


class TestNexusAppState:
    """Tests for NexusAppState dataclass."""

    def test_all_fields_default_to_none_or_falsy(self) -> None:
        """All fields should have safe defaults (None, False, empty)."""
        state = NexusAppState()
        assert state.nexus_fs is None
        assert state.database_url is None
        assert state.api_key is None
        assert state.auth_provider is None
        assert state.system_services is None
        assert state.brick_services is None
        assert state.search_daemon_enabled is False

    def test_deployment_defaults(self) -> None:
        """Deployment config should have sensible defaults."""
        state = NexusAppState()
        assert state.deployment_profile == "full"
        assert state.deployment_mode == "standalone"
        assert state.thread_pool_size == 40
        assert state.operation_timeout == 30.0

    def test_enabled_bricks_defaults_to_empty_frozenset(self) -> None:
        state = NexusAppState()
        assert state.enabled_bricks == frozenset()

    def test_exposed_methods_defaults_to_empty_dict(self) -> None:
        state = NexusAppState()
        assert state.exposed_methods == {}


class TestInitAppState:
    """Tests for init_app_state() helper."""

    def test_sets_all_fields_on_app_state(self) -> None:
        """init_app_state should set all NexusAppState fields on app.state."""
        app = _make_app()
        init_app_state(app)

        # All NexusAppState fields should be initialized
        assert app.state.nexus_fs is None
        assert app.state.database_url is None
        assert app.state.deployment_profile == "full"
        assert app.state.thread_pool_size == 40
        assert app.state.system_services is None
        assert app.state.brick_services is None

    def test_sets_nexus_fs(self) -> None:
        """init_app_state should set nexus_fs on app.state."""
        app = _make_app()
        mock_fs = MagicMock()
        mock_fs._system_services = None
        mock_fs._brick_services = None
        mock_fs._write_observer = None
        mock_fs._permission_enforcer = None
        init_app_state(app, nexus_fs=mock_fs)
        assert app.state.nexus_fs is mock_fs

    def test_overrides_applied(self) -> None:
        """Keyword overrides should be set on app.state."""
        app = _make_app()
        init_app_state(app, api_key="test-key", database_url="sqlite://")
        assert app.state.api_key == "test-key"
        assert app.state.database_url == "sqlite://"

    def test_flattens_nexus_fs_internals(self) -> None:
        """init_app_state should flatten NexusFS private attrs onto app.state."""
        app = _make_app()
        mock_sys = MagicMock()
        mock_sys.observability_subsystem = "obs"
        mock_sys.brick_lifecycle_manager = "blm"
        mock_sys.brick_reconciler = "br"
        mock_sys.eviction_manager = "em"
        mock_fs = MagicMock()
        mock_fs.service.return_value = "eb"
        mock_fs._system_services = mock_sys
        mock_fs._brick_services = "brk"
        mock_fs._write_observer = "wo"
        mock_fs._permission_enforcer = "pe"
        mock_fs.service = lambda name: {"event_bus": "eb"}.get(name)

        init_app_state(app, nexus_fs=mock_fs)

        assert app.state.system_services is mock_sys
        assert app.state.brick_services == "brk"
        assert app.state.event_bus == "eb"
        assert app.state.write_observer == "wo"
        assert app.state.permission_enforcer == "pe"
        assert app.state.observability_subsystem == "obs"
        assert app.state.brick_lifecycle_manager == "blm"
        assert app.state.brick_reconciler == "br"
        assert app.state.eviction_manager == "em"

    def test_none_nexus_fs_does_not_crash(self) -> None:
        """init_app_state should work fine with nexus_fs=None."""
        app = _make_app()
        init_app_state(app, nexus_fs=None)
        assert app.state.nexus_fs is None
        assert app.state.system_services is None
        assert app.state.brick_services is None

    def test_does_not_overwrite_existing_attrs(self) -> None:
        """init_app_state should not overwrite pre-existing app.state fields."""
        app = _make_app()
        app.state.deployment_profile = "edge"
        init_app_state(app)
        # Should preserve existing value
        assert app.state.deployment_profile == "edge"

    def test_missing_system_services_safe(self) -> None:
        """When NexusFS has no _system_services, flattened fields stay None."""
        app = _make_app()
        mock_fs = MagicMock(spec=[])  # No attributes at all
        init_app_state(app, nexus_fs=mock_fs)
        assert app.state.system_services is None
        assert app.state.observability_subsystem is None
