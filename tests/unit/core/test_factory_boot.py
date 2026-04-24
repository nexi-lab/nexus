"""Unit tests for the factory boot sequence in nexus.factory.

Covers:
- _boot_kernel_services validates Storage Pillars (returns empty dict)
- _boot_system_services returns critical services + degrades on non-critical failure
- BootError raised with tier="system-critical" on critical service failure
- create_nexus_services returns a flat dict
- _BootContext is frozen (attribute assignment raises error)

Issue #2193: Former kernel services moved to system tier.
"""

import dataclasses
from unittest.mock import MagicMock, patch

import pytest

from nexus.contracts.deployment_profile import DeploymentProfile
from nexus.contracts.exceptions import BootError
from nexus.contracts.types import AuditConfig
from nexus.core.config import (
    DistributedConfig,
    PermissionConfig,
)
from nexus.factory import (
    _boot_brick_services,
    _boot_kernel_services,
    _boot_system_services,
    _BootContext,
    create_nexus_fs,
    create_nexus_services,
)
from nexus.lib.performance_tuning import resolve_profile_tuning

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Issue #2193: Kernel returns empty dict (validation only)
EXPECTED_KERNEL_KEYS: frozenset[str] = frozenset()

# Issue #2193: System tier now has former-kernel + original system keys
EXPECTED_SYSTEM_KEYS = frozenset(
    {
        # Former-kernel critical
        "rebac_manager",
        "audit_store",
        "entity_registry",
        "permission_enforcer",
        "write_observer",
        # Former-kernel degradable
        # hierarchy_manager, dir_visibility_cache, namespace_manager → rebac-internal
        "deferred_permission_buffer",
        "workspace_registry",
        "mount_manager",
        "workspace_manager",
        # Original services
        "async_namespace_manager",
        "delivery_worker",
        "observability_subsystem",
        "resiliency_manager",
        "context_branch_service",
        "zone_lifecycle",
        "scheduler_service",
        "event_signal",
    }
)


def _make_boot_context(**overrides: object) -> _BootContext:
    """Build a _BootContext with fully mocked dependencies.

    All stores, backends, and engines are MagicMock instances so that
    downstream service constructors receive objects with the right shape
    without requiring real infrastructure.
    """
    record_store = MagicMock()
    record_store.engine = MagicMock()
    record_store.read_engine = MagicMock()
    record_store.session_factory = MagicMock()
    record_store.has_read_replica = False
    record_store.database_url = "sqlite://"
    record_store.async_session_factory = MagicMock()

    backend = MagicMock()
    backend.root_path = "/tmp/test"
    backend.has_root_path = True
    backend.on_write_callback = None
    backend.on_sync_callback = None

    profile_tuning = resolve_profile_tuning(DeploymentProfile.FULL)

    defaults: dict[str, object] = {
        "record_store": record_store,
        "metadata_store": MagicMock(),
        "backend": backend,
        "dlc": MagicMock(),
        "engine": record_store.engine,
        "read_engine": record_store.read_engine,
        "perm": PermissionConfig(enforce=False, enable_deferred=False, enable_tiger_cache=False),
        "audit": AuditConfig(strict_mode=False),
        "cache_ttl_seconds": 300,
        "dist": DistributedConfig(
            enable_events=False,
            enable_workflows=False,
        ),
        "zone_id": None,
        "agent_id": None,
        "enable_write_buffer": False,
        "resiliency_raw": None,
        "db_url": "sqlite://",
        "profile_tuning": profile_tuning,
    }
    defaults.update(overrides)
    return _BootContext(**defaults)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestBootKernelServices:
    """Tests for _boot_kernel_services() — Issue #2193: validation-only."""

    def test_boot_kernel_services_returns_empty_dict(self) -> None:
        """_boot_kernel_services(ctx) returns empty dict (validation only)."""
        ctx = _make_boot_context()
        result = _boot_kernel_services(ctx)

        assert isinstance(result, dict)
        assert set(result.keys()) == EXPECTED_KERNEL_KEYS
        assert len(result) == 0

    def test_boot_error_raised_on_none_dlc(self) -> None:
        """When dlc is None, BootError is raised with tier='kernel'."""
        ctx = _make_boot_context(dlc=None)

        with pytest.raises(BootError) as exc_info:
            _boot_kernel_services(ctx)

        assert exc_info.value.tier == "kernel"

    def test_boot_error_raised_on_none_metadata_store(self) -> None:
        """When metadata_store is None, BootError is raised with tier='kernel'."""
        ctx = _make_boot_context(metadata_store=None)

        with pytest.raises(BootError) as exc_info:
            _boot_kernel_services(ctx)

        assert exc_info.value.tier == "kernel"


class TestBootSystemServices:
    """Tests for _boot_system_services() — Issue #2193: critical + degradable."""

    def test_system_tier_returns_all_keys(self) -> None:
        """System tier returns dict with all expected keys."""
        ctx = _make_boot_context()
        result = _boot_system_services(ctx)

        assert isinstance(result, dict)
        assert set(result.keys()) == EXPECTED_SYSTEM_KEYS

    def test_critical_and_rebac_services_are_not_none(self) -> None:
        """Critical (write_observer) + degradable ReBAC services must be non-None.

        Issue #2440: ReBAC services are degradable but always get NoOp fallback.
        deferred_permission_buffer is degradable and may be None.
        """
        ctx = _make_boot_context()
        result = _boot_system_services(ctx)

        for key in (
            "rebac_manager",
            "audit_store",
            "entity_registry",
            "permission_enforcer",
            "write_observer",
        ):
            assert result[key] is not None, f"Service {key} should not be None"

    def test_rebac_failure_raises_boot_error(self) -> None:
        """ReBAC is critical — failure raises BootError with tier='system-critical'.

        ReBACManager, AuditStore, EntityRegistry, PermissionEnforcer, and
        WriteObserver are all in the critical section.  Any failure aborts boot.
        """
        ctx = _make_boot_context()

        with (
            patch(
                "nexus.bricks.rebac.manager.ReBACManager",
                side_effect=RuntimeError("bad engine"),
            ),
            pytest.raises(BootError) as exc_info,
        ):
            _boot_system_services(ctx)

        assert exc_info.value.tier == "system-critical"

    def test_system_services_values_are_not_none(self) -> None:
        """All system service values except nullable keys are non-None.

        deferred_permission_buffer is None because enable_deferred=False in the
        test context.
        """
        ctx = _make_boot_context()
        result = _boot_system_services(ctx)

        _NULLABLE_KEYS = {
            "deferred_permission_buffer",
            "delivery_worker",
            "observability_subsystem",
            "workspace_registry",  # degradable — None with mock session_factory
            "scheduler_service",  # degradable — None if SchedulerService unavailable
        }
        for key, value in result.items():
            if key in _NULLABLE_KEYS:
                continue  # may be None depending on config
            else:
                assert value is not None, f"{key} should not be None"

    def test_degradable_failure_returns_none(self) -> None:
        """Degradable service failure returns None (not an exception).

        Patches namespace_manager creation via rebac_manager.create_namespace_manager().
        Agent registry was removed (Issue #1692).
        """
        ctx = _make_boot_context()

        with patch(
            "nexus.bricks.rebac.namespace_factory.create_namespace_manager",
            side_effect=RuntimeError("ns fail"),
        ):
            result = _boot_system_services(ctx)

        assert isinstance(result, dict)
        # namespace_manager is now rebac-internal — verify via rebac_manager property
        rebac = result["rebac_manager"]
        assert rebac is not None
        assert getattr(rebac, "namespace_manager", None) is None

    def test_deferred_buffer_none_when_disabled(self) -> None:
        """deferred_permission_buffer is None when enable_deferred=False."""
        ctx = _make_boot_context()  # enable_deferred=False by default
        result = _boot_system_services(ctx)
        assert result["deferred_permission_buffer"] is None


class TestBootBrickServices:
    """Tests for _boot_brick_services()."""

    def test_brick_tier_returns_dict(self) -> None:
        """_boot_brick_services() returns a dict."""
        ctx = _make_boot_context()
        system = _boot_system_services(ctx)
        result = _boot_brick_services(ctx, system)

        assert isinstance(result, dict)

    def test_brick_tier_contains_expected_keys(self) -> None:
        """_boot_brick_services() returns expected brick service keys."""
        ctx = _make_boot_context()
        system = _boot_system_services(ctx)
        result = _boot_brick_services(ctx, system)

        # These keys should always be present (even if values are None)
        # event_bus/lock_manager moved to _boot_services()
        expected_keys = {
            "wallet_provisioner",
            "manifest_resolver",
            "tool_namespace_middleware",
            "chunked_upload_service",
            "workflow_engine",
            "api_key_creator",
            "snapshot_service",
        }
        for key in expected_keys:
            assert key in result, f"Missing brick key: {key}"


class TestBootContextFrozen:
    """Tests for _BootContext frozen dataclass."""

    def test_boot_context_is_frozen(self) -> None:
        """_BootContext is frozen — attribute assignment raises error."""
        ctx = _make_boot_context()
        with pytest.raises(dataclasses.FrozenInstanceError):
            ctx.db_url = "postgresql://new"  # type: ignore[misc]


class TestCreateNexusServices:
    """Tests for the top-level create_nexus_services() function."""

    def test_create_nexus_services_returns_single_dict(self) -> None:
        """Full create_nexus_services() returns a single flat dict."""
        record_store = MagicMock()
        record_store.engine = MagicMock()
        record_store.read_engine = MagicMock()
        record_store.session_factory = MagicMock()
        record_store.has_read_replica = False
        record_store.database_url = "sqlite://"
        record_store.async_session_factory = MagicMock()

        metadata_store = MagicMock()

        backend = MagicMock()
        backend.root_path = "/tmp/test"
        backend.has_root_path = True
        backend.on_write_callback = None
        backend.on_sync_callback = None

        mock_dlc = MagicMock()

        result = create_nexus_services(
            record_store=record_store,
            metadata_store=metadata_store,
            backend=backend,
            dlc=mock_dlc,
            permissions=PermissionConfig(
                enforce=False,
                enable_deferred=False,
                enable_tiger_cache=False,
            ),
            distributed=DistributedConfig(
                enable_events=False,
                enable_workflows=False,
            ),
            enable_write_buffer=False,
        )

        assert isinstance(result, dict)
        assert "rebac_manager" in result
        assert "permission_enforcer" in result

    def test_create_nexus_services_populates_fields(self) -> None:
        """Issue #2193: create_nexus_services() populates service fields."""
        record_store = MagicMock()
        record_store.engine = MagicMock()
        record_store.read_engine = MagicMock()
        record_store.session_factory = MagicMock()
        record_store.has_read_replica = False
        record_store.database_url = "sqlite://"
        record_store.async_session_factory = MagicMock()

        metadata_store = MagicMock()

        backend = MagicMock()
        backend.root_path = "/tmp/test"
        backend.has_root_path = True
        backend.on_write_callback = None
        backend.on_sync_callback = None

        mock_dlc = MagicMock()

        services = create_nexus_services(
            record_store=record_store,
            metadata_store=metadata_store,
            backend=backend,
            dlc=mock_dlc,
            permissions=PermissionConfig(
                enforce=False,
                enable_deferred=False,
                enable_tiger_cache=False,
            ),
            distributed=DistributedConfig(
                enable_events=False,
                enable_workflows=False,
            ),
            enable_write_buffer=False,
        )

        # Issue #2193: Former-kernel fields now in flat services dict
        assert services["rebac_manager"] is not None
        assert services["permission_enforcer"] is not None
        # workspace_registry may be None with mock session_factory (degradable)
        assert services["write_observer"] is not None

        # Issue #2034: version_service in the unified dict
        assert services["version_service"] is not None


class TestBrickServicesFieldCompleteness:
    """Issue #2134: Factory-created bricks are packed into BrickServices container."""

    @pytest.mark.asyncio
    def test_create_nexus_fs_packs_factory_bricks_into_brick_services(self) -> None:
        """create_nexus_fs() packs parse_fn, content_cache, registries, lock manager
        into BrickServices rather than passing as flat NexusFS params (Issue #2134).
        """
        record_store = MagicMock()
        record_store.engine = MagicMock()
        record_store.read_engine = MagicMock()
        record_store.session_factory = MagicMock()
        record_store.has_read_replica = False
        record_store.database_url = "sqlite://"
        record_store.async_session_factory = MagicMock()

        from nexus_kernel import Kernel

        metadata_store = MagicMock()
        metadata_store._kernel = Kernel()

        backend = MagicMock()
        backend.root_path = "/tmp/test"
        backend.has_root_path = True
        backend.on_write_callback = None
        backend.on_sync_callback = None

        nx = create_nexus_fs(
            backend=backend,
            metadata_store=metadata_store,
            record_store=record_store,
            permissions=PermissionConfig(
                enforce=False,
                enable_deferred=False,
                enable_tiger_cache=False,
            ),
            distributed=DistributedConfig(
                enable_events=False,
                enable_workflows=False,
            ),
            enable_write_buffer=False,
        )

        # Issue #2134: These fields now live in service registry, not as flat params
        assert nx.service("parse_fn") is not None, "parse_fn should be packed into service registry"
        assert nx.service("parser_registry") is not None, (
            "parser_registry should be in service registry"
        )
        assert nx.service("provider_registry") is not None, (
            "provider_registry should be in service registry"
        )
        # NOTE: vfs_lock_manager removed from BrickServices — now kernel-internal
        # (created in NexusFS.__init__). See write-path-extraction-design.md.
        # NOTE: content_cache may be None when router.route("/") fails during
        # _do_link() (MagicMock backend doesn't set up proper route table).
        # The factory gracefully degrades — ContentCache is optional.

    @pytest.mark.asyncio
    def test_create_nexus_fs_workflow_engine_override_in_brick_services(self) -> None:
        """workflow_engine param is packed into BrickServices (Issue #2134)."""
        record_store = MagicMock()
        record_store.engine = MagicMock()
        record_store.read_engine = MagicMock()
        record_store.session_factory = MagicMock()
        record_store.has_read_replica = False
        record_store.database_url = "sqlite://"
        record_store.async_session_factory = MagicMock()

        from nexus_kernel import Kernel

        metadata_store = MagicMock()
        metadata_store._kernel = Kernel()

        backend = MagicMock()
        backend.root_path = "/tmp/test"
        backend.has_root_path = True
        backend.on_write_callback = None
        backend.on_sync_callback = None

        sentinel_engine = MagicMock()

        nx = create_nexus_fs(
            backend=backend,
            metadata_store=metadata_store,
            record_store=record_store,
            permissions=PermissionConfig(
                enforce=False,
                enable_deferred=False,
                enable_tiger_cache=False,
            ),
            distributed=DistributedConfig(
                enable_events=False,
                enable_workflows=False,
            ),
            enable_write_buffer=False,
            workflow_engine=sentinel_engine,
        )

        _ref = nx.service("workflow_engine")
        assert _ref is not None
        assert _ref is sentinel_engine
