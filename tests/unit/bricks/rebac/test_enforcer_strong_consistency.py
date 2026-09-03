"""PermissionEnforcer honours ``OperationContext.consistency`` (Issue #4739).

``consistency="strong"`` must (a) reach ``rebac_check`` / ``rebac_check_bulk``
so the manager skips its L1 / Tiger phases, (b) skip the enforcer-side
boundary cache, and (c) run ``filter_list`` through the strong chain, which
never consults the Tiger bitmap or Leopard index.  Cached (default) calls
must not gain a ``consistency`` kwarg — older managers and test doubles do
not accept it.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from nexus.bricks.rebac.enforcer import PermissionEnforcer
from nexus.contracts.types import OperationContext, Permission


def _ctx(consistency: str | None) -> OperationContext:
    return OperationContext(user_id="alice", groups=[], zone_id="root", consistency=consistency)


def _rebac(allowed: bool = True) -> MagicMock:
    rebac = MagicMock()
    rebac.rebac_check.return_value = allowed

    def _bulk(checks, **_kw):
        return dict.fromkeys(checks, allowed)

    rebac.rebac_check_bulk.side_effect = _bulk
    return rebac


class TestSinglePathCheck:
    def test_shallow_path_forwards_strong_to_rebac_check(self):
        rebac = _rebac()
        enforcer = PermissionEnforcer(rebac_manager=rebac)

        assert enforcer.check("/doc.txt", Permission.READ, _ctx("strong")) is True

        kwargs = rebac.rebac_check.call_args.kwargs
        assert kwargs["consistency"] == "strong"

    def test_shallow_path_default_has_no_consistency_kwarg(self):
        rebac = _rebac()
        enforcer = PermissionEnforcer(rebac_manager=rebac)

        enforcer.check("/doc.txt", Permission.READ, _ctx(None))

        assert "consistency" not in rebac.rebac_check.call_args.kwargs

    def test_deep_path_forwards_strong_to_bulk(self):
        rebac = _rebac()
        enforcer = PermissionEnforcer(rebac_manager=rebac)

        assert enforcer.check("/a/b/c/d/e.txt", Permission.READ, _ctx("strong")) is True

        kwargs = rebac.rebac_check_bulk.call_args.kwargs
        assert kwargs["zone_id"] == "root"
        assert kwargs["consistency"] == "strong"

    def test_deep_path_default_has_no_consistency_kwarg(self):
        rebac = _rebac()
        enforcer = PermissionEnforcer(rebac_manager=rebac)

        enforcer.check("/a/b/c/d/e.txt", Permission.READ, _ctx(None))

        assert "consistency" not in rebac.rebac_check_bulk.call_args.kwargs

    def test_strong_skips_boundary_cache(self):
        rebac = _rebac(allowed=False)
        enforcer = PermissionEnforcer(rebac_manager=rebac)
        # A cached boundary that would short-circuit an eventual check.
        enforcer._boundary_cache.set_boundary("root", "user", "alice", "read", "/a/b.txt", "/a")
        enforcer._boundary_cache.get_boundary = MagicMock(
            side_effect=AssertionError("boundary cache consulted under strong")
        )

        assert enforcer.check("/a/b.txt", Permission.READ, _ctx("strong")) is False


class TestFilterList:
    def test_strong_uses_authoritative_chain_only(self):
        rebac = _rebac()
        enforcer = PermissionEnforcer(rebac_manager=rebac)
        enforcer._cache.try_bitmap_filter = MagicMock(
            side_effect=AssertionError("Tiger consulted under strong")
        )
        enforcer._cache.try_leopard_lookup = MagicMock(
            side_effect=AssertionError("Leopard consulted under strong")
        )

        allowed = enforcer.filter_list(["/ws/a.txt", "/ws/b.txt"], _ctx("strong"))

        assert allowed == ["/ws/a.txt", "/ws/b.txt"]
        kwargs = rebac.rebac_check_bulk.call_args.kwargs
        assert kwargs["consistency"] == "strong"

    def test_default_chain_consults_tiger_first(self):
        rebac = _rebac()
        enforcer = PermissionEnforcer(rebac_manager=rebac)
        enforcer._cache.try_bitmap_filter = MagicMock(return_value=None)

        enforcer.filter_list(["/ws/a.txt"], _ctx(None))

        enforcer._cache.try_bitmap_filter.assert_called_once()
        assert "consistency" not in rebac.rebac_check_bulk.call_args.kwargs

    def test_strong_denies_when_tuple_store_denies(self):
        rebac = _rebac(allowed=False)
        enforcer = PermissionEnforcer(rebac_manager=rebac)
        enforcer._cache.try_bitmap_filter = MagicMock(
            return_value=(["/ws/a.txt"], [])  # stale Tiger "allow"
        )

        assert enforcer.filter_list(["/ws/a.txt"], _ctx("strong")) == []


class TestFilterSearchResults:
    def test_strong_is_forwarded(self):
        rebac = _rebac()
        enforcer = PermissionEnforcer(rebac_manager=rebac)

        out = enforcer.filter_search_results(
            ["/ws/a.txt"], user_id="alice", zone_id="root", consistency="strong"
        )

        assert out == ["/ws/a.txt"]
        assert rebac.rebac_check_bulk.call_args.kwargs["consistency"] == "strong"

    def test_default_has_no_consistency_kwarg(self):
        rebac = _rebac()
        enforcer = PermissionEnforcer(rebac_manager=rebac)

        enforcer.filter_search_results(["/ws/a.txt"], user_id="alice", zone_id="root")

        assert "consistency" not in rebac.rebac_check_bulk.call_args.kwargs


class TestDescendantVisibility:
    """Tiger misses are confirmed from the tuple store under strong."""

    @pytest.fixture
    def enforcer_with_empty_bitmap(self):
        rebac = _rebac(allowed=False)
        tiger = MagicMock()
        tiger.get_accessible_paths_with_status.return_value = (None, True)  # no bitmap yet
        rebac._tiger_cache = tiger
        return PermissionEnforcer(rebac_manager=rebac), rebac

    def test_eventual_stays_fail_closed(self, enforcer_with_empty_bitmap):
        enforcer, rebac = enforcer_with_empty_bitmap

        assert enforcer.has_accessible_descendants_batch(["/ws/"], _ctx(None)) == {"/ws/": False}
        rebac.rebac_check_bulk.assert_not_called()

    def test_strong_confirms_miss_from_tuple_store(self, enforcer_with_empty_bitmap):
        enforcer, rebac = enforcer_with_empty_bitmap

        def _bulk(checks, **_kw):
            return {c: c[2][1] == "/ws" for c in checks}

        rebac.rebac_check_bulk.side_effect = _bulk

        result = enforcer.has_accessible_descendants_batch(["/ws/", "/other/"], _ctx("strong"))

        assert result == {"/ws/": True, "/other/": False}
        assert rebac.rebac_check_bulk.call_args.kwargs["consistency"] == "strong"

    def test_strong_keeps_tiger_hits(self):
        rebac = _rebac(allowed=False)
        tiger = MagicMock()
        tiger.get_accessible_paths_with_status.return_value = (["/ws/deep/file.txt"], True)
        rebac._tiger_cache = tiger
        enforcer = PermissionEnforcer(rebac_manager=rebac)

        result = enforcer.has_accessible_descendants_batch(["/ws/", "/other/"], _ctx("strong"))

        # /ws/ visible via the bitmap; /other/ confirmed denied by the tuple store.
        assert result == {"/ws/": True, "/other/": False}
        checked_paths = {c[2][1] for c in rebac.rebac_check_bulk.call_args.args[0]}
        assert "/other" in checked_paths
        assert "/ws" not in checked_paths
