"""Tests for kernel config dataclasses (Phase 6, Issue #1391 + Issue #2034).

Validates:
- Default values for each frozen dataclass
- frozen=True prevents mutation (raises FrozenInstanceError)
- dataclasses.replace() creates modified copies
- KernelServices / SystemServices / BrickServices 3-tier split (Issue #2034)
- Cross-contamination: tier fields do not leak across containers
"""

from __future__ import annotations

import dataclasses

import pytest

from nexus.core.config import (
    BrickServices,
    CacheConfig,
    DistributedConfig,
    KernelServices,
    MemoryConfig,
    ParseConfig,
    PermissionConfig,
    SystemServices,
)

# ---------------------------------------------------------------------------
# CacheConfig
# ---------------------------------------------------------------------------


class TestCacheConfig:
    """Tests for CacheConfig frozen dataclass."""

    def test_defaults(self) -> None:
        cfg = CacheConfig()
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
            ttl_seconds=None,
            content_cache_size_mb=512,
        )
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
    """Tests for KernelServices frozen dataclass (Tier 0 — kernel only)."""

    def test_defaults_all_none(self) -> None:
        """Issue #2193: KernelServices has only router field."""
        ks = KernelServices()
        assert ks.router is None

    def test_frozen(self) -> None:
        """KernelServices is frozen — attributes cannot be set after init."""
        ks = KernelServices()
        with pytest.raises(dataclasses.FrozenInstanceError):
            ks.router = "some_router"  # type: ignore[misc]

    def test_construct_with_values(self) -> None:
        sentinel = object()
        ks = KernelServices(router=sentinel)
        assert ks.router is sentinel

    def test_replace(self) -> None:
        """Use dataclasses.replace() to create modified copies."""
        ks = KernelServices()
        new = dataclasses.replace(ks, router="new_router")
        assert new.router == "new_router"
        assert ks.router is None  # original unchanged

    def test_is_dataclass(self) -> None:
        assert dataclasses.is_dataclass(KernelServices)
        ks = KernelServices()
        assert dataclasses.is_dataclass(ks)

    def test_all_kernel_fields_present(self) -> None:
        """Verify KernelServices has exactly the Tier 0 kernel fields.

        Issue #2193: Only router remains. All other fields moved to SystemServices.
        """
        field_names = {f.name for f in dataclasses.fields(KernelServices)}
        expected_fields = {"router"}
        assert field_names == expected_fields, (
            f"Extra: {field_names - expected_fields}, Missing: {expected_fields - field_names}"
        )

    def test_router_annotation(self) -> None:
        """Issue #2193: KernelServices.router is typed Any."""
        annotations = KernelServices.__annotations__
        assert "router" in annotations


# ---------------------------------------------------------------------------
# SystemServices (Issue #2034, #2193 — Tier 1)
# ---------------------------------------------------------------------------


class TestSystemServices:
    """Tests for SystemServices frozen dataclass (Tier 1 — degraded-mode)."""

    def test_defaults_all_none(self) -> None:
        ss = SystemServices()
        # Former-kernel critical
        assert ss.rebac_manager is None
        assert ss.audit_store is None
        assert ss.entity_registry is None
        assert ss.permission_enforcer is None
        assert ss.write_observer is None
        # Former-kernel degradable
        assert ss.dir_visibility_cache is None
        assert ss.hierarchy_manager is None
        assert ss.deferred_permission_buffer is None
        assert ss.workspace_registry is None
        assert ss.mount_manager is None
        assert ss.workspace_manager is None
        # Original system services
        assert ss.agent_registry is None
        assert ss.async_agent_registry is None
        assert ss.namespace_manager is None
        assert ss.async_namespace_manager is None
        assert ss.context_branch_service is None
        assert ss.brick_lifecycle_manager is None
        assert ss.delivery_worker is None
        assert ss.observability_subsystem is None
        assert ss.resiliency_manager is None
        # (PipeManager is kernel-internal §4.2, not in SystemServices)

    def test_frozen(self) -> None:
        ss = SystemServices()
        with pytest.raises(dataclasses.FrozenInstanceError):
            ss.agent_registry = "x"  # type: ignore[misc]

    def test_construct_with_values(self) -> None:
        sentinel = object()
        ss = SystemServices(agent_registry=sentinel, resiliency_manager=sentinel)
        assert ss.agent_registry is sentinel
        assert ss.resiliency_manager is sentinel
        assert ss.namespace_manager is None

    def test_replace(self) -> None:
        ss = SystemServices()
        new = dataclasses.replace(ss, observability_subsystem="obs")
        assert new.observability_subsystem == "obs"
        assert ss.observability_subsystem is None

    def test_all_system_fields_present(self) -> None:
        """Verify SystemServices has exactly the Tier 1 system fields.

        Issue #2193: Absorbed 11 former-kernel fields.
        """
        field_names = {f.name for f in dataclasses.fields(SystemServices)}
        expected_fields = {
            # Former-kernel critical
            "rebac_manager",
            "audit_store",
            "entity_registry",
            "permission_enforcer",
            "write_observer",
            # Former-kernel degradable
            "dir_visibility_cache",
            "hierarchy_manager",
            "deferred_permission_buffer",
            "workspace_registry",
            "mount_manager",
            "workspace_manager",
            # Original system services
            "agent_registry",
            "async_agent_registry",
            "eviction_manager",
            "namespace_manager",
            "async_namespace_manager",
            "context_branch_service",
            "brick_lifecycle_manager",
            "brick_reconciler",
            "delivery_worker",
            "observability_subsystem",
            "resiliency_manager",
            "zone_lifecycle",
            "process_table",
            "scheduler_service",
        }
        assert field_names == expected_fields, (
            f"Extra: {field_names - expected_fields}, Missing: {expected_fields - field_names}"
        )

    def test_protocol_type_annotations(self) -> None:
        annotations = SystemServices.__annotations__
        ns_ann = str(annotations.get("namespace_manager", ""))
        assert "NamespaceManagerProtocol" in ns_ann
        assert "None" in ns_ann
        # Issue #2193: write_observer moved from KernelServices
        wo_ann = str(annotations.get("write_observer", ""))
        assert "WriteObserverProtocol" in wo_ann
        assert "None" in wo_ann


# ---------------------------------------------------------------------------
# BrickServices (Issue #2034 — Tier 2)
# ---------------------------------------------------------------------------


class TestBrickServices:
    """Tests for BrickServices frozen dataclass (Tier 2 — optional)."""

    def test_defaults_all_none(self) -> None:
        bs = BrickServices()
        assert bs.event_bus is None
        assert bs.lock_manager is None
        assert bs.workflow_engine is None
        assert bs.rebac_circuit_breaker is None
        assert bs.wallet_provisioner is None
        assert bs.chunked_upload_service is None
        assert bs.manifest_resolver is None
        assert bs.tool_namespace_middleware is None
        assert bs.api_key_creator is None
        assert bs.snapshot_service is None
        # DT_PIPE consumer (Issue #810)
        assert bs.zoekt_pipe_consumer is None

    def test_frozen(self) -> None:
        bs = BrickServices()
        with pytest.raises(dataclasses.FrozenInstanceError):
            bs.event_bus = "x"  # type: ignore[misc]

    def test_construct_with_values(self) -> None:
        sentinel = object()
        bs = BrickServices(event_bus=sentinel, lock_manager=sentinel)
        assert bs.event_bus is sentinel
        assert bs.lock_manager is sentinel
        assert bs.workflow_engine is None

    def test_replace(self) -> None:
        bs = BrickServices()
        new = dataclasses.replace(bs, workflow_engine="wf")
        assert new.workflow_engine == "wf"
        assert bs.workflow_engine is None

    def test_all_brick_fields_present(self) -> None:
        """Verify BrickServices has exactly the Tier 2 brick fields.

        Issue #2034: version_service moved here from KernelServices.
        """
        field_names = {f.name for f in dataclasses.fields(BrickServices)}
        expected_fields = {
            "event_bus",
            "lock_manager",
            "workflow_engine",
            "rebac_circuit_breaker",
            "wallet_provisioner",
            "chunked_upload_service",
            "manifest_resolver",
            "tool_namespace_middleware",
            "api_key_creator",
            "snapshot_service",
            "cache_brick",
            "ipc_storage_driver",
            "ipc_provisioner",
            "agent_event_log",
            "delegation_service",
            "reputation_service",
            "version_service",
            # Factory-created bricks (Issue #2134)
            "parse_fn",
            "content_cache",
            "parser_registry",
            "provider_registry",
            # NOTE: vfs_lock_manager removed — now kernel-internal (NexusFS.__init__).
            # See write-path-extraction-design.md.
            # Governance Brick (Issue #2129)
            "governance_anomaly_service",
            "governance_collusion_service",
            "governance_graph_service",
            "governance_response_service",
            # DT_PIPE consumer (Issue #810)
            "zoekt_pipe_consumer",
        }
        assert field_names == expected_fields, (
            f"Extra: {field_names - expected_fields}, Missing: {expected_fields - field_names}"
        )

    def test_protocol_type_annotations(self) -> None:
        annotations = BrickServices.__annotations__
        wf_ann = str(annotations.get("workflow_engine", ""))
        assert "WorkflowProtocol" in wf_ann
        assert "None" in wf_ann


# ---------------------------------------------------------------------------
# Cross-contamination tests (Issue #2034)
# ---------------------------------------------------------------------------


class TestCrossContamination:
    """Verify tier fields do NOT leak across containers."""

    def test_kernel_has_no_system_fields(self) -> None:
        kernel_fields = {f.name for f in dataclasses.fields(KernelServices)}
        system_fields = {f.name for f in dataclasses.fields(SystemServices)}
        overlap = kernel_fields & system_fields
        assert overlap == set(), f"Kernel/System overlap: {overlap}"

    def test_kernel_has_no_brick_fields(self) -> None:
        kernel_fields = {f.name for f in dataclasses.fields(KernelServices)}
        brick_fields = {f.name for f in dataclasses.fields(BrickServices)}
        overlap = kernel_fields & brick_fields
        assert overlap == set(), f"Kernel/Brick overlap: {overlap}"

    def test_system_has_no_brick_fields(self) -> None:
        system_fields = {f.name for f in dataclasses.fields(SystemServices)}
        brick_fields = {f.name for f in dataclasses.fields(BrickServices)}
        overlap = system_fields & brick_fields
        assert overlap == set(), f"System/Brick overlap: {overlap}"

    def test_all_three_tiers_frozen(self) -> None:
        for cls in (KernelServices, SystemServices, BrickServices):
            assert dataclasses.fields(cls)  # is a dataclass
            instance = cls()
            first_field = dataclasses.fields(cls)[0].name
            with pytest.raises(dataclasses.FrozenInstanceError):
                setattr(instance, first_field, "mutated")
