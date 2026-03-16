"""Unit tests for _build_retroactive_hook_specs() (Issue #1452 Phase 4).

Verifies that all retroactive hook groups from _register_vfs_hooks() are
captured as HookSpecs so swap_service() can cleanly unregister them during
hot-swap.  Services that implement HotSwappable.hook_spec() (e.g. EventsService)
are NOT in this table — they self-register at bootstrap time.
"""

from __future__ import annotations

from unittest.mock import sentinel

import pytest

from nexus.contracts.protocols.service_hooks import HookSpec
from nexus.factory.orchestrator import _build_retroactive_hook_specs


class _FakeCoordinator:
    """Minimal coordinator stub — just stores set_hook_spec calls."""

    def __init__(self) -> None:
        self.specs: dict[str, HookSpec] = {}

    def set_hook_spec(self, name: str, spec: HookSpec) -> None:
        self.specs[name] = spec


def _full_hook_refs() -> dict[str, object]:
    """Return hook_refs dict with all retroactive hook objects present.

    EventsService is excluded — it implements HotSwappable.hook_spec()
    and self-registers at bootstrap time (Issue #1611).
    """
    return {
        "perm_hook": sentinel.perm_hook,
        "audit": sentinel.audit,
        "viewer_hook": sentinel.viewer_hook,
        "auto_parse_hook": sentinel.auto_parse_hook,
        "tiger_rename_hook": sentinel.tiger_rename,
        "tiger_write_hook": sentinel.tiger_write,
        "vview_resolver": sentinel.vview_resolver,
        "bus_observer": sentinel.bus_observer,
        "rev_observer": sentinel.rev_observer,
    }


# ---------------------------------------------------------------------------
# All hooks captured
# ---------------------------------------------------------------------------


class TestBuildRetroactiveHookSpecs:
    def test_all_hooks_captured(self) -> None:
        """Full boot — all retroactive hook groups result in specs."""
        coord = _FakeCoordinator()
        _build_retroactive_hook_specs(coord, _full_hook_refs())

        expected_names = {
            "permission",
            "audit",
            "viewer",
            "auto_parse",
            "tiger_cache",
            "virtual_view",
            "event_bus",
            "revision_tracking",
        }
        assert set(coord.specs.keys()) == expected_names

    def test_partial_hooks_captured(self) -> None:
        """Minimal boot — only bus_observer provided."""
        coord = _FakeCoordinator()
        hook_refs = dict.fromkeys(_full_hook_refs())
        hook_refs["bus_observer"] = sentinel.bus_observer

        _build_retroactive_hook_specs(coord, hook_refs)

        assert set(coord.specs.keys()) == {"event_bus"}

    def test_none_hooks_skipped(self) -> None:
        """All None — no specs set at all."""
        coord = _FakeCoordinator()
        hook_refs = dict.fromkeys(_full_hook_refs())
        _build_retroactive_hook_specs(coord, hook_refs)

        assert coord.specs == {}

    def test_empty_hook_refs(self) -> None:
        """Empty dict — no specs, no errors."""
        coord = _FakeCoordinator()
        _build_retroactive_hook_specs(coord, {})
        assert coord.specs == {}


# ---------------------------------------------------------------------------
# Permission hook — 6 channels
# ---------------------------------------------------------------------------


class TestPermissionHookSpec:
    def test_permission_has_6_channels(self) -> None:
        coord = _FakeCoordinator()
        hook_refs = dict.fromkeys(_full_hook_refs())
        hook_refs["perm_hook"] = sentinel.perm_hook

        _build_retroactive_hook_specs(coord, hook_refs)

        spec = coord.specs["permission"]
        assert spec.read_hooks == (sentinel.perm_hook,)
        assert spec.write_hooks == (sentinel.perm_hook,)
        assert spec.delete_hooks == (sentinel.perm_hook,)
        assert spec.rename_hooks == (sentinel.perm_hook,)
        assert spec.mkdir_hooks == (sentinel.perm_hook,)
        assert spec.rmdir_hooks == (sentinel.perm_hook,)
        assert spec.total_hooks == 6


# ---------------------------------------------------------------------------
# Audit hook — 6 channels
# ---------------------------------------------------------------------------


class TestAuditHookSpec:
    def test_audit_has_6_channels(self) -> None:
        coord = _FakeCoordinator()
        hook_refs = dict.fromkeys(_full_hook_refs())
        hook_refs["audit"] = sentinel.audit

        _build_retroactive_hook_specs(coord, hook_refs)

        spec = coord.specs["audit"]
        assert spec.write_hooks == (sentinel.audit,)
        assert spec.write_batch_hooks == (sentinel.audit,)
        assert spec.delete_hooks == (sentinel.audit,)
        assert spec.rename_hooks == (sentinel.audit,)
        assert spec.mkdir_hooks == (sentinel.audit,)
        assert spec.rmdir_hooks == (sentinel.audit,)
        assert spec.total_hooks == 6


# ---------------------------------------------------------------------------
# Tiger cache — combined rename + write
# ---------------------------------------------------------------------------


class TestTigerCacheHookSpec:
    def test_tiger_cache_combined(self) -> None:
        """Both rename + write hooks present → combined into one spec."""
        coord = _FakeCoordinator()
        hook_refs = dict.fromkeys(_full_hook_refs())
        hook_refs["tiger_rename_hook"] = sentinel.tiger_rename
        hook_refs["tiger_write_hook"] = sentinel.tiger_write

        _build_retroactive_hook_specs(coord, hook_refs)

        spec = coord.specs["tiger_cache"]
        assert spec.rename_hooks == (sentinel.tiger_rename,)
        assert spec.write_hooks == (sentinel.tiger_write,)
        assert spec.total_hooks == 2

    def test_tiger_rename_only(self) -> None:
        """Only rename hook → write_hooks is empty tuple."""
        coord = _FakeCoordinator()
        hook_refs = dict.fromkeys(_full_hook_refs())
        hook_refs["tiger_rename_hook"] = sentinel.tiger_rename

        _build_retroactive_hook_specs(coord, hook_refs)

        spec = coord.specs["tiger_cache"]
        assert spec.rename_hooks == (sentinel.tiger_rename,)
        assert spec.write_hooks == ()

    def test_tiger_write_only(self) -> None:
        """Only write hook → rename_hooks is empty tuple."""
        coord = _FakeCoordinator()
        hook_refs = dict.fromkeys(_full_hook_refs())
        hook_refs["tiger_write_hook"] = sentinel.tiger_write

        _build_retroactive_hook_specs(coord, hook_refs)

        spec = coord.specs["tiger_cache"]
        assert spec.rename_hooks == ()
        assert spec.write_hooks == (sentinel.tiger_write,)


# ---------------------------------------------------------------------------
# Single-channel hook groups
# ---------------------------------------------------------------------------


class TestSingleChannelHookSpecs:
    @pytest.mark.parametrize(
        ("ref_key", "spec_name", "field"),
        [
            ("viewer_hook", "viewer", "read_hooks"),
            ("auto_parse_hook", "auto_parse", "write_hooks"),
            ("vview_resolver", "virtual_view", "resolvers"),
            ("bus_observer", "event_bus", "observers"),
            ("rev_observer", "revision_tracking", "observers"),
        ],
    )
    def test_single_channel(self, ref_key: str, spec_name: str, field: str) -> None:
        coord = _FakeCoordinator()
        hook_refs = dict.fromkeys(_full_hook_refs())
        hook_refs[ref_key] = sentinel.hook_obj

        _build_retroactive_hook_specs(coord, hook_refs)

        spec = coord.specs[spec_name]
        assert getattr(spec, field) == (sentinel.hook_obj,)
        assert not spec.is_empty
