"""Unit tests for ServiceRegistry (Issue #1452)."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock

import pytest

from nexus.core.service_registry import ServiceInfo, ServiceRef, ServiceRegistry

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
        assert info.exports == ()
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
        ref = registry.service("search")
        assert isinstance(ref, ServiceRef)
        assert ref._service_instance is mock_svc

    def test_service_missing_returns_none(self, registry: ServiceRegistry) -> None:
        assert registry.service("nonexistent") is None

    def test_service_or_raise_missing(self, registry: ServiceRegistry) -> None:
        with pytest.raises(KeyError, match="nonexistent"):
            registry.service_or_raise("nonexistent")

    def test_service_or_raise_found(self, registry: ServiceRegistry, mock_svc: Any) -> None:
        registry.register_service("search", mock_svc)
        # service_or_raise returns raw instance (not ServiceRef)
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
        ref = registry.service("search")
        assert ref is not None
        assert ref._service_instance is svc2


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
        ref = registry.service("mount")
        assert ref is not None

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
        ref = registry.service("search")
        assert ref is not None
        assert ref._service_instance is svc
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
# enlist_wired_services (Issue #1708)
# ---------------------------------------------------------------------------


class TestEnlistWiredServices:
    """Verify enlist_wired_services registers all services via coordinator (#1708)."""

    def test_all_canonical_names_registered(self) -> None:
        import asyncio

        from nexus.core.kernel_dispatch import KernelDispatch
        from nexus.core.service_registry import ServiceRegistry
        from nexus.factory.service_routing import (
            _CANONICAL_NAMES,
            enlist_wired_services,
        )
        from nexus.system_services.lifecycle.service_lifecycle_coordinator import (
            ServiceLifecycleCoordinator,
        )

        # Build a dict with a unique mock per service
        wired_dict: dict[str, Any] = {}
        for src_key in _CANONICAL_NAMES:
            wired_dict[src_key] = MagicMock(name=f"mock_{src_key}")

        reg = ServiceRegistry()
        dispatch = KernelDispatch()
        coordinator = ServiceLifecycleCoordinator(reg, None, dispatch)
        count = asyncio.run(enlist_wired_services(coordinator, wired_dict))
        assert count == len(_CANONICAL_NAMES)

        # Every canonical name should map to the correct mock instance
        # service() returns ServiceRef, use _service_instance to unwrap
        for src_key, canonical in _CANONICAL_NAMES.items():
            ref = reg.service(canonical)
            assert ref is not None, f"Missing service: {canonical}"
            assert ref._service_instance is wired_dict[src_key], (
                f"Mismatch for {src_key}: registry({canonical}) is not wired_dict[{src_key}]"
            )


# ---------------------------------------------------------------------------
# EXPORT_SYMBOL validation
# ---------------------------------------------------------------------------


class TestExportsValidation:
    def test_valid_exports(self, registry: ServiceRegistry) -> None:
        svc = MagicMock(spec=["glob", "grep"])
        registry.register_service("search", svc, exports=("glob", "grep"))
        info = registry.service_info("search")
        assert info is not None
        assert info.exports == ("glob", "grep")

    def test_invalid_export_raises(self, registry: ServiceRegistry) -> None:
        svc = MagicMock(spec=["glob"])
        with pytest.raises(ValueError, match="exports not found.*nonexistent"):
            registry.register_service("search", svc, exports=("glob", "nonexistent"))

    def test_empty_exports_default(self, registry: ServiceRegistry, mock_svc: Any) -> None:
        registry.register_service("search", mock_svc)
        info = registry.service_info("search")
        assert info is not None
        assert info.exports == ()

    def test_snapshot_includes_exports(self, registry: ServiceRegistry) -> None:
        svc = MagicMock(spec=["glob", "grep"])
        registry.register_service("search", svc, exports=("glob", "grep"))
        snap = registry.snapshot()
        assert len(snap) == 1
        assert snap[0]["exports"] == ["glob", "grep"]


# ---------------------------------------------------------------------------
# replace_service (atomic swap)
# ---------------------------------------------------------------------------


class TestReplaceService:
    def test_atomic_replace(self, registry: ServiceRegistry) -> None:
        svc1 = MagicMock(spec=["glob"])
        svc2 = MagicMock(spec=["glob", "grep"])
        registry.register_service("search", svc1, exports=("glob",))

        old_info = registry.replace_service("search", svc2, exports=("glob", "grep"))
        assert old_info.instance is svc1
        assert old_info.exports == ("glob",)

        # New instance is now served
        ref = registry.service("search")
        assert ref is not None
        assert ref._service_instance is svc2
        # New exports
        info = registry.service_info("search")
        assert info is not None
        assert info.exports == ("glob", "grep")

    def test_replace_preserves_dependencies(self, registry: ServiceRegistry) -> None:
        registry.register_service("gateway", MagicMock())
        svc1 = MagicMock()
        registry.register_service("mount", svc1, dependencies=("gateway",))

        svc2 = MagicMock()
        old = registry.replace_service("mount", svc2)
        assert old.instance is svc1

        info = registry.service_info("mount")
        assert info is not None
        assert info.dependencies == ("gateway",)
        assert info.instance is svc2

    def test_replace_missing_raises(self, registry: ServiceRegistry) -> None:
        with pytest.raises(KeyError, match="not registered"):
            registry.replace_service("nonexistent", MagicMock())

    def test_replace_invalid_exports_raises(self, registry: ServiceRegistry) -> None:
        svc1 = MagicMock(spec=["glob"])
        registry.register_service("search", svc1)
        svc2 = MagicMock(spec=[])  # no 'glob' attribute
        with pytest.raises(ValueError, match="invalid exports"):
            registry.replace_service("search", svc2, exports=("glob",))

    def test_replace_inherits_old_exports_when_empty(self, registry: ServiceRegistry) -> None:
        svc1 = MagicMock(spec=["glob"])
        registry.register_service("search", svc1, exports=("glob",))
        svc2 = MagicMock(spec=["glob", "grep"])
        registry.replace_service("search", svc2)
        info = registry.service_info("search")
        assert info is not None
        assert info.exports == ("glob",)  # inherited from old


# ---------------------------------------------------------------------------
# unregister_service (rmmod with dependency guard)
# ---------------------------------------------------------------------------


class TestUnregisterService:
    def test_unregister(self, registry: ServiceRegistry) -> None:
        svc = MagicMock()
        registry.register_service("search", svc)
        removed = registry.unregister_service("search")
        assert removed is not None
        assert removed.instance is svc
        assert registry.service("search") is None

    def test_unregister_missing(self, registry: ServiceRegistry) -> None:
        assert registry.unregister_service("nonexistent") is None

    def test_unregister_blocked_by_dependent(self, registry: ServiceRegistry) -> None:
        registry.register_service("gateway", MagicMock())
        registry.register_service("mount", MagicMock(), dependencies=("gateway",))
        with pytest.raises(ValueError, match="depended on by.*mount"):
            registry.unregister_service("gateway")


# ---------------------------------------------------------------------------
# ServiceRef — transparent proxy with ref-counting
# ---------------------------------------------------------------------------


class TestServiceRef:
    def test_sync_method_delegation(self) -> None:
        svc = MagicMock()
        svc.glob.return_value = ["a.py", "b.py"]
        refcounts: dict[str, int] = {}
        drain_events: dict[str, asyncio.Event] = {}
        ref = ServiceRef(svc, "search", refcounts, drain_events)

        result = ref.glob("*.py")
        assert result == ["a.py", "b.py"]
        svc.glob.assert_called_once_with("*.py")
        # Refcount should be 0 after sync call completes
        assert refcounts.get("search", 0) == 0

    @pytest.mark.asyncio()
    async def test_async_method_delegation(self) -> None:
        svc = MagicMock()

        async def _async_search(query: str) -> list:
            return [query]

        object.__setattr__(svc, "search", _async_search)
        refcounts: dict[str, int] = {}
        drain_events: dict[str, asyncio.Event] = {}
        ref = ServiceRef(svc, "search", refcounts, drain_events)

        result = await ref.search("hello")
        assert result == ["hello"]
        assert refcounts.get("search", 0) == 0

    def test_attribute_delegation(self) -> None:
        svc = MagicMock()
        svc.name = "test_service"
        refcounts: dict[str, int] = {}
        drain_events: dict[str, asyncio.Event] = {}
        ref = ServiceRef(svc, "search", refcounts, drain_events)

        assert ref.name == "test_service"

    def test_setattr_delegation(self) -> None:
        svc = MagicMock()
        refcounts: dict[str, int] = {}
        drain_events: dict[str, asyncio.Event] = {}
        ref = ServiceRef(svc, "search", refcounts, drain_events)

        ref.custom_flag = True
        assert svc.custom_flag is True

    @pytest.mark.asyncio()
    async def test_drain_event_fires(self) -> None:
        svc = MagicMock()

        async def _slow_op() -> str:
            await asyncio.sleep(0.01)
            return "done"

        object.__setattr__(svc, "slow", _slow_op)
        refcounts: dict[str, int] = {}
        drain_events: dict[str, asyncio.Event] = {}
        ref = ServiceRef(svc, "search", refcounts, drain_events)

        # Set up drain event before call
        evt = asyncio.Event()
        drain_events["search"] = evt

        result = await ref.slow()
        assert result == "done"
        # Drain event should have been set (refcount went to 0)
        assert evt.is_set()

    def test_repr(self) -> None:
        svc = MagicMock()
        ref = ServiceRef(svc, "search", {}, {})
        r = repr(ref)
        assert "search" in r
        assert "MagicMock" in r

    def test_service_returns_ref(self, registry: ServiceRegistry, mock_svc: Any) -> None:
        registry.register_service("search", mock_svc)
        ref = registry.service("search")
        assert isinstance(ref, ServiceRef)
        assert ref._service_instance is mock_svc
