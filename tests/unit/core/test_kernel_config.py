"""Tests for kernel config dataclasses (Phase 6, Issue #1391).

Validates:
- Default values for each frozen dataclass
- frozen=True prevents mutation (raises FrozenInstanceError)
- dataclasses.replace() creates modified copies
"""

from __future__ import annotations

import dataclasses

import pytest

from nexus.core.config import (
    CacheConfig,
    DistributedConfig,
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
        assert cfg.path_size == 512
        assert cfg.list_size == 1024
        assert cfg.kv_size == 256
        assert cfg.exists_size == 1024
        assert cfg.ttl_seconds == 300

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
        )
        assert cfg.ttl_seconds is None


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
            enable_workflows=False,
        )
        assert cfg.enable_events is False
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
