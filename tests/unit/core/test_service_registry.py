"""Unit tests for ServiceRegistry (Issue #1452)."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from nexus.core.service_registry import ServiceInfo, ServiceRegistry

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def registry() -> ServiceRegistry:
    return ServiceRegistry()


@pytest.fixture()
def mock_svc() -> MagicMock:
    return MagicMock(spec=["glob", "search"])


# ---------------------------------------------------------------------------
# ServiceInfo dataclass
# ---------------------------------------------------------------------------


class TestServiceInfo:
    def test_frozen(self) -> None:
        info = ServiceInfo(name="x", instance=object())
        with pytest.raises(AttributeError):
            info.name = "y"

    def test_defaults(self) -> None:
        svc = object()
        info = ServiceInfo(name="search", instance=svc)
        assert info.dependencies == ()
        assert info.profile_gate is None
        assert info.is_remote is False
        assert dict(info.metadata) == {}

    def test_metadata_immutable(self) -> None:
        info = ServiceInfo(name="x", instance=object())
        with pytest.raises(TypeError):
            dict.__setitem__(info.metadata, "k", "v")


# ---------------------------------------------------------------------------
# register_service + service lookup
# ---------------------------------------------------------------------------


class TestRegisterAndLookup:
    def test_register_and_service(self, registry: ServiceRegistry, mock_svc: Any) -> None:
        registry.register_service("search", mock_svc)
        assert registry.service("search") is mock_svc

    def test_service_missing_returns_none(self, registry: ServiceRegistry) -> None:
        assert registry.service("nonexistent") is None

    def test_service_or_raise_missing(self, registry: ServiceRegistry) -> None:
        with pytest.raises(KeyError, match="nonexistent"):
            registry.service_or_raise("nonexistent")

    def test_service_or_raise_found(self, registry: ServiceRegistry, mock_svc: Any) -> None:
        registry.register_service("search", mock_svc)
        assert registry.service_or_raise("search") is mock_svc

    def test_service_info_returns_envelope(self, registry: ServiceRegistry, mock_svc: Any) -> None:
        registry.register_service("search", mock_svc, profile_gate="discovery")
        info = registry.service_info("search")
        assert info is not None
        assert isinstance(info, ServiceInfo)
        assert info.name == "search"
        assert info.instance is mock_svc
        assert info.profile_gate == "discovery"

    def test_service_info_missing(self, registry: ServiceRegistry) -> None:
        assert registry.service_info("nope") is None


# ---------------------------------------------------------------------------
# Duplicate / overwrite
# ---------------------------------------------------------------------------


class TestDuplicateRegistration:
    def test_duplicate_raises(self, registry: ServiceRegistry) -> None:
        registry.register_service("search", MagicMock())
        with pytest.raises(ValueError, match="already registered"):
            registry.register_service("search", MagicMock())

    def test_allow_overwrite(self, registry: ServiceRegistry) -> None:
        svc1 = MagicMock()
        svc2 = MagicMock()
        registry.register_service("search", svc1)
        registry.register_service("search", svc2, allow_overwrite=True)
        assert registry.service("search") is svc2


# ---------------------------------------------------------------------------
# Dependency validation
# ---------------------------------------------------------------------------


class TestDependencyValidation:
    def test_missing_dep_raises(self, registry: ServiceRegistry) -> None:
        with pytest.raises(ValueError, match="missing dependencies.*gateway"):
            registry.register_service("mount", MagicMock(), dependencies=("gateway",))

    def test_satisfied_deps_ok(self, registry: ServiceRegistry) -> None:
        registry.register_service("gateway", MagicMock())
        registry.register_service("mount", MagicMock(), dependencies=("gateway",))
        assert registry.service("mount") is not None

    def test_multiple_missing_deps(self, registry: ServiceRegistry) -> None:
        with pytest.raises(ValueError, match="missing dependencies"):
            registry.register_service("mount", MagicMock(), dependencies=("gateway", "rebac"))


# ---------------------------------------------------------------------------
# register_many
# ---------------------------------------------------------------------------


class TestRegisterMany:
    def test_skips_none(self, registry: ServiceRegistry) -> None:
        svc = MagicMock()
        count = registry.register_many({"search": svc, "mcp": None, "llm": None})
        assert count == 1
        assert registry.service("search") is svc
        assert registry.service("mcp") is None

    def test_is_remote_flag(self, registry: ServiceRegistry) -> None:
        svc = MagicMock()
        registry.register_many({"search": svc}, is_remote=True)
        info = registry.service_info("search")
        assert info is not None
        assert info.is_remote is True

    def test_empty_dict(self, registry: ServiceRegistry) -> None:
        assert registry.register_many({}) == 0


# ---------------------------------------------------------------------------
# snapshot
# ---------------------------------------------------------------------------


class TestSnapshot:
    def test_snapshot_format(self, registry: ServiceRegistry) -> None:
        registry.register_service("search", MagicMock(), profile_gate="discovery")
        registry.register_service("gateway", MagicMock())
        snap = registry.snapshot()
        assert len(snap) == 2
        names = {s["name"] for s in snap}
        assert names == {"gateway", "search"}
        search_entry = next(s for s in snap if s["name"] == "search")
        assert search_entry["profile_gate"] == "discovery"
        assert search_entry["is_remote"] is False
        assert isinstance(search_entry["type"], str)


# ---------------------------------------------------------------------------
# Dual-write invariant: populate_service_registry
# ---------------------------------------------------------------------------


class TestPopulateServiceRegistry:
    """Verify populate_service_registry registers all services correctly."""

    def test_all_canonical_names_registered(self) -> None:
        from nexus.core.service_registry import ServiceRegistry
        from nexus.factory.service_routing import (
            _CANONICAL_NAMES,
            populate_service_registry,
        )

        # Build a dict with a unique mock per service
        wired_dict: dict[str, Any] = {}
        for src_key in _CANONICAL_NAMES:
            wired_dict[src_key] = MagicMock(name=f"mock_{src_key}")

        reg = ServiceRegistry()
        count = populate_service_registry(reg, wired_dict)
        assert count == len(_CANONICAL_NAMES)

        # Every canonical name should map to the correct mock instance
        for src_key, canonical in _CANONICAL_NAMES.items():
            reg_val = reg.service(canonical)
            assert reg_val is wired_dict[src_key], (
                f"Mismatch for {src_key}: registry({canonical}) is not wired_dict[{src_key}]"
            )
