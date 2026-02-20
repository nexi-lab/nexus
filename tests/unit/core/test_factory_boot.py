"""Unit tests for the factory boot sequence in nexus.factory.

Covers:
- _boot_kernel_services returns the expected 13 keys
- BootError raised with tier="kernel" on kernel boot failure
- _boot_system_services returns None for failed services (degraded mode)
- KernelServices is frozen (attribute assignment raises FrozenInstanceError)
- create_nexus_services returns a KernelServices instance
- _BootContext is frozen (attribute assignment raises error)
"""

from __future__ import annotations

import dataclasses
from unittest.mock import MagicMock, patch

import pytest

from nexus.contracts.deployment_profile import DeploymentProfile
from nexus.contracts.exceptions import BootError
from nexus.core.config import (
    BrickServices,
    DistributedConfig,
    KernelServices,
    PermissionConfig,
    SystemServices,
)
from nexus.core.performance_tuning import resolve_profile_tuning
from nexus.factory import (
    _boot_brick_services,
    _boot_kernel_services,
    _boot_system_services,
    _BootContext,
    create_nexus_services,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

EXPECTED_KERNEL_KEYS = frozenset(
    {
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
        "router": MagicMock(),
        "engine": record_store.engine,
        "read_engine": record_store.read_engine,
        "perm": PermissionConfig(enforce=False, enable_deferred=False, enable_tiger_cache=False),
        "cache_ttl_seconds": 300,
        "dist": DistributedConfig(
            enable_events=False,
            enable_locks=False,
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
    """Tests for _boot_kernel_services()."""

    def test_boot_kernel_services_returns_all_keys(self) -> None:
        """_boot_kernel_services(ctx) returns dict with exactly 11 expected keys.

        Issue #2034: version_service and rebac_circuit_breaker moved to brick tier.
        """
        ctx = _make_boot_context()
        result = _boot_kernel_services(ctx)

        assert isinstance(result, dict)
        assert set(result.keys()) == EXPECTED_KERNEL_KEYS
        assert len(result) == 11

    def test_boot_error_raised_on_kernel_failure(self) -> None:
        """When kernel boot fails (e.g. bad engine), BootError is raised with tier='kernel'."""
        ctx = _make_boot_context()
        # Sabotage the engine so that EnhancedReBACManager.__init__ blows up
        ctx.engine.dispose = MagicMock(side_effect=RuntimeError("boom"))

        with patch(
            "nexus.rebac.manager.EnhancedReBACManager.__init__",
            side_effect=RuntimeError("bad engine"),
        ):
            with pytest.raises(BootError) as exc_info:
                _boot_kernel_services(ctx)

            assert exc_info.value.tier == "kernel"
            assert "bad engine" in str(exc_info.value)

    def test_kernel_services_values_are_not_none(self) -> None:
        """All 11 kernel service values except deferred_permission_buffer are non-None.

        deferred_permission_buffer is None because enable_deferred=False in the
        test context.
        """
        ctx = _make_boot_context()
        result = _boot_kernel_services(ctx)

        for key, value in result.items():
            if key == "deferred_permission_buffer":
                # enable_deferred=False in our test context
                assert value is None, f"{key} should be None when enable_deferred=False"
            else:
                assert value is not None, f"{key} should not be None"


class TestBootSystemServices:
    """Tests for _boot_system_services()."""

    def test_system_tier_returns_none_on_failure(self) -> None:
        """_boot_system_services() returns None for failed services (degraded mode).

        It should NOT raise an exception — each service failure is swallowed
        and replaced with None.
        """
        ctx = _make_boot_context()
        kernel = _boot_kernel_services(ctx)

        # Patch all system-tier constructors to fail
        patches = [
            patch(
                "nexus.services.agents.agent_registry.AgentRegistry.__init__",
                side_effect=RuntimeError("agent fail"),
            ),
            patch(
                "nexus.rebac.namespace_factory.create_namespace_manager",
                side_effect=RuntimeError("ns fail"),
            ),
            patch(
                "nexus.services.routing.async_router.AsyncVFSRouter.__init__",
                side_effect=RuntimeError("router fail"),
            ),
        ]

        for p in patches:
            p.start()
        try:
            result = _boot_system_services(ctx, kernel)
        finally:
            for p in patches:
                p.stop()

        # Should return a dict (not raise)
        assert isinstance(result, dict)

        # The patched services should be None
        assert result["agent_registry"] is None
        assert result["namespace_manager"] is None
        assert result["async_vfs_router"] is None

    def test_system_tier_returns_dict(self) -> None:
        """_boot_system_services() returns a dict regardless of individual failures."""
        ctx = _make_boot_context()
        kernel = _boot_kernel_services(ctx)

        result = _boot_system_services(ctx, kernel)
        assert isinstance(result, dict)
        # System tier should contain these keys at minimum
        expected_system_keys = {
            "agent_registry",
            "async_agent_registry",
            "namespace_manager",
            "async_namespace_manager",
            "async_vfs_router",
            "delivery_worker",
            "observability_subsystem",
            "resiliency_manager",
            "context_branch_service",
            "brick_lifecycle_manager",
            "scoped_hook_engine",
        }
        assert expected_system_keys.issubset(set(result.keys()))


class TestBootBrickServices:
    """Tests for _boot_brick_services()."""

    def test_brick_tier_returns_dict(self) -> None:
        """_boot_brick_services() returns a dict."""
        ctx = _make_boot_context()
        kernel = _boot_kernel_services(ctx)
        result = _boot_brick_services(ctx, kernel)

        assert isinstance(result, dict)

    def test_brick_tier_contains_expected_keys(self) -> None:
        """_boot_brick_services() returns expected brick service keys."""
        ctx = _make_boot_context()
        kernel = _boot_kernel_services(ctx)
        result = _boot_brick_services(ctx, kernel)

        # These keys should always be present (even if values are None)
        expected_keys = {
            "wallet_provisioner",
            "manifest_resolver",
            "tool_namespace_middleware",
            "chunked_upload_service",
            "event_bus",
            "lock_manager",
            "workflow_engine",
            "api_key_creator",
            "snapshot_service",
            "task_queue_service",
            "skill_service",
            "skill_package_service",
        }
        for key in expected_keys:
            assert key in result, f"Missing brick key: {key}"


class TestKernelServicesFrozen:
    """Tests for KernelServices frozen dataclass."""

    def test_kernel_services_frozen(self) -> None:
        """KernelServices is a frozen dataclass — setting an attribute raises error."""
        ks = KernelServices()
        with pytest.raises(dataclasses.FrozenInstanceError):
            ks.router = MagicMock()  # type: ignore[misc]

    def test_kernel_services_replace_returns_new_instance(self) -> None:
        """dataclasses.replace() on KernelServices returns a new copy."""
        ks = KernelServices()
        sentinel = MagicMock()
        ks2 = dataclasses.replace(ks, router=sentinel)

        assert ks2 is not ks
        assert ks2.router is sentinel
        assert ks.router is None


class TestBootContextFrozen:
    """Tests for _BootContext frozen dataclass."""

    def test_boot_context_is_frozen(self) -> None:
        """_BootContext is frozen — attribute assignment raises error."""
        ctx = _make_boot_context()
        with pytest.raises(dataclasses.FrozenInstanceError):
            ctx.db_url = "postgresql://new"  # type: ignore[misc]


class TestCreateNexusServices:
    """Tests for the top-level create_nexus_services() function."""

    def test_create_nexus_services_returns_three_tier_tuple(self) -> None:
        """Full create_nexus_services() returns (KernelServices, SystemServices, BrickServices)."""
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

        router = MagicMock()

        result = create_nexus_services(
            record_store=record_store,
            metadata_store=metadata_store,
            backend=backend,
            router=router,
            permissions=PermissionConfig(
                enforce=False,
                enable_deferred=False,
                enable_tiger_cache=False,
            ),
            distributed=DistributedConfig(
                enable_events=False,
                enable_locks=False,
                enable_workflows=False,
            ),
            enable_write_buffer=False,
        )

        assert isinstance(result, tuple)
        assert len(result) == 3
        kernel, system, brick = result
        assert isinstance(kernel, KernelServices)
        assert isinstance(system, SystemServices)
        assert isinstance(brick, BrickServices)

    def test_create_nexus_services_populates_kernel_fields(self) -> None:
        """create_nexus_services() populates core kernel fields on the returned object."""
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

        router = MagicMock()

        kernel, system, brick = create_nexus_services(
            record_store=record_store,
            metadata_store=metadata_store,
            backend=backend,
            router=router,
            permissions=PermissionConfig(
                enforce=False,
                enable_deferred=False,
                enable_tiger_cache=False,
            ),
            distributed=DistributedConfig(
                enable_events=False,
                enable_locks=False,
                enable_workflows=False,
            ),
            enable_write_buffer=False,
        )

        # Kernel-tier fields must be populated
        assert kernel.rebac_manager is not None
        assert kernel.permission_enforcer is not None
        assert kernel.workspace_registry is not None
        assert kernel.write_observer is not None
        assert kernel.router is router

        # Issue #2034: version_service moved to brick tier
        assert brick.version_service is not None
