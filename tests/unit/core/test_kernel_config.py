"""Tests for kernel config dataclasses (Phase 6, Issue #1391).

Validates:
- Default values for each frozen dataclass
- frozen=True prevents mutation (raises FrozenInstanceError)
- dataclasses.replace() creates modified copies
- KernelServices allows mutation (not frozen)
- Backward-compat aliases (LRUCacheConfig, SecurityConfig, FeatureFlags)
"""

from __future__ import annotations

import dataclasses

import pytest

from nexus.core.config import (
    CacheConfig,
    DistributedConfig,
    FeatureFlags,
    KernelServices,
    MemoryConfig,
    ParseConfig,
    PermissionConfig,
)

# ---------------------------------------------------------------------------
# CacheConfig
# ---------------------------------------------------------------------------


class TestCacheConfig:
    """Tests for CacheConfig frozen dataclass."""

    def test_defaults(self) -> None:
        cfg = CacheConfig()
        assert cfg.enable_metadata_cache is True
        assert cfg.path_size == 512
        assert cfg.list_size == 1024
        assert cfg.kv_size == 256
        assert cfg.exists_size == 1024
        assert cfg.ttl_seconds == 300
        assert cfg.enable_content_cache is True
        assert cfg.content_cache_size_mb == 256

    def test_frozen(self) -> None:
        cfg = CacheConfig()
        with pytest.raises(dataclasses.FrozenInstanceError):
            cfg.path_size = 999  # type: ignore[misc]

    def test_replace(self) -> None:
        cfg = CacheConfig(path_size=128)
        new = dataclasses.replace(cfg, path_size=256)
        assert new.path_size == 256
        assert cfg.path_size == 128  # original unchanged

    def test_custom_values(self) -> None:
        cfg = CacheConfig(
            enable_metadata_cache=False,
            ttl_seconds=None,
            content_cache_size_mb=512,
        )
        assert cfg.enable_metadata_cache is False
        assert cfg.ttl_seconds is None
        assert cfg.content_cache_size_mb == 512


# ---------------------------------------------------------------------------
# PermissionConfig
# ---------------------------------------------------------------------------


class TestPermissionConfig:
    """Tests for PermissionConfig frozen dataclass."""

    def test_defaults(self) -> None:
        cfg = PermissionConfig()
        assert cfg.enforce is True
        assert cfg.inherit is True
        assert cfg.allow_admin_bypass is False
        assert cfg.enforce_zone_isolation is True
        assert cfg.audit_strict_mode is True
        assert cfg.enable_tiger_cache is True
        assert cfg.enable_deferred is True
        assert cfg.deferred_flush_interval == 0.05

    def test_frozen(self) -> None:
        cfg = PermissionConfig()
        with pytest.raises(dataclasses.FrozenInstanceError):
            cfg.enforce = False  # type: ignore[misc]

    def test_replace(self) -> None:
        cfg = PermissionConfig(enforce=False)
        new = dataclasses.replace(cfg, enforce=True, allow_admin_bypass=True)
        assert new.enforce is True
        assert new.allow_admin_bypass is True
        assert cfg.enforce is False  # original unchanged

    def test_common_test_config(self) -> None:
        """PermissionConfig(enforce=False) is the standard test setup."""
        cfg = PermissionConfig(enforce=False)
        assert cfg.enforce is False
        assert cfg.audit_strict_mode is True  # other defaults unchanged


# ---------------------------------------------------------------------------
# DistributedConfig
# ---------------------------------------------------------------------------


class TestDistributedConfig:
    """Tests for DistributedConfig frozen dataclass."""

    def test_defaults(self) -> None:
        cfg = DistributedConfig()
        assert cfg.coordination_url is None
        assert cfg.enable_events is True
        assert cfg.enable_locks is True
        assert cfg.enable_workflows is True
        assert cfg.event_bus_backend == "redis"
        assert cfg.nats_url == "nats://localhost:4222"

    def test_frozen(self) -> None:
        cfg = DistributedConfig()
        with pytest.raises(dataclasses.FrozenInstanceError):
            cfg.enable_events = False  # type: ignore[misc]

    def test_all_disabled(self) -> None:
        """Test pattern used by make_test_nexus: all distributed features off."""
        cfg = DistributedConfig(
            enable_events=False,
            enable_locks=False,
            enable_workflows=False,
        )
        assert cfg.enable_events is False
        assert cfg.enable_locks is False
        assert cfg.enable_workflows is False


# ---------------------------------------------------------------------------
# MemoryConfig
# ---------------------------------------------------------------------------


class TestMemoryConfig:
    """Tests for MemoryConfig frozen dataclass."""

    def test_defaults(self) -> None:
        cfg = MemoryConfig()
        assert cfg.enable_paging is True
        assert cfg.main_capacity == 100
        assert cfg.recall_max_age_hours == 24.0

    def test_frozen(self) -> None:
        cfg = MemoryConfig()
        with pytest.raises(dataclasses.FrozenInstanceError):
            cfg.main_capacity = 50  # type: ignore[misc]

    def test_custom_values(self) -> None:
        cfg = MemoryConfig(main_capacity=200, recall_max_age_hours=48.0)
        assert cfg.main_capacity == 200
        assert cfg.recall_max_age_hours == 48.0


# ---------------------------------------------------------------------------
# ParseConfig
# ---------------------------------------------------------------------------


class TestParseConfig:
    """Tests for ParseConfig frozen dataclass."""

    def test_defaults(self) -> None:
        cfg = ParseConfig()
        assert cfg.auto_parse is True
        assert cfg.providers is None

    def test_frozen(self) -> None:
        cfg = ParseConfig()
        with pytest.raises(dataclasses.FrozenInstanceError):
            cfg.auto_parse = False  # type: ignore[misc]

    def test_auto_parse_off(self) -> None:
        """ParseConfig(auto_parse=False) is the standard test setup."""
        cfg = ParseConfig(auto_parse=False)
        assert cfg.auto_parse is False

    def test_with_providers(self) -> None:
        providers = ({"name": "pdf"}, {"name": "docx"})
        cfg = ParseConfig(providers=providers)
        assert cfg.providers == ({"name": "pdf"}, {"name": "docx"})


# ---------------------------------------------------------------------------
# KernelServices
# ---------------------------------------------------------------------------


class TestKernelServices:
    """Tests for KernelServices mutable dataclass."""

    def test_defaults_all_none(self) -> None:
        ks = KernelServices()
        assert ks.router is None
        assert ks.rebac_manager is None
        assert ks.event_bus is None
        assert ks.lock_manager is None
        assert ks.workflow_engine is None
        assert ks.version_service is None
        assert ks.write_observer is None
        # Server-layer extras are in server_extras dict, not direct fields
        assert ks.server_extras == {}

    def test_mutable(self) -> None:
        """KernelServices is NOT frozen — attributes can be set."""
        ks = KernelServices()
        ks.router = "some_router"
        assert ks.router == "some_router"

    def test_construct_with_values(self) -> None:
        sentinel = object()
        ks = KernelServices(version_service=sentinel, event_bus=sentinel)
        assert ks.version_service is sentinel
        assert ks.event_bus is sentinel
        assert ks.rebac_manager is None  # others still None

    def test_is_dataclass(self) -> None:
        assert dataclasses.is_dataclass(KernelServices)
        ks = KernelServices()
        assert dataclasses.is_dataclass(ks)

    def test_all_service_fields_present(self) -> None:
        """Verify KernelServices has all expected fields.

        Server-layer extras (observability_subsystem, chunked_upload_service,
        manifest_resolver, manifest_metrics, rebac_circuit_breaker,
        tool_namespace_middleware, resiliency_manager, delivery_worker) are
        stored in the opaque server_extras dict, not as direct dataclass fields.
        """
        field_names = {f.name for f in dataclasses.fields(KernelServices)}
        expected_fields = {
            "router",
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
            "overlay_resolver",
            "wallet_provisioner",
            "event_bus",
            "lock_manager",
            "workflow_engine",
            "server_extras",
        }
        assert expected_fields.issubset(field_names), f"Missing: {expected_fields - field_names}"


# ---------------------------------------------------------------------------
# Backward-compat aliases
# ---------------------------------------------------------------------------


class TestBackwardCompatAliases:
    """Verify backward-compat aliases still work."""

    def test_lru_cache_config_is_cache_config(self) -> None:
        from nexus.core.config import LRUCacheConfig

        assert LRUCacheConfig is CacheConfig

    def test_security_config_is_permission_config(self) -> None:
        from nexus.core.config import SecurityConfig

        assert SecurityConfig is PermissionConfig

    def test_feature_flags_accepts_kwargs(self) -> None:
        """FeatureFlags stub accepts any kwargs without error."""
        ff = FeatureFlags(enable_tiger_cache=True, enable_deferred=False)
        assert ff is not None
