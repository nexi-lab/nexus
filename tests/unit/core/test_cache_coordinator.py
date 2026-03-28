"""Unit tests for CacheCoordinator critical paths.

Issue #3192 decision 9C: Test coordinator invalidation ordering,
callback isolation, eager recompute resilience, and stats tracking.

Tests the coordinator with DT_STREAM integration, callback failure
isolation, and async eager recompute (break→continue fix verified).
"""

import logging
from unittest.mock import MagicMock

import pytest

pytest.importorskip("pyroaring")

from nexus.bricks.rebac.cache.coordinator import CacheCoordinator

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def l1_cache() -> MagicMock:
    """Mock L1 permission cache with standard interface."""
    cache = MagicMock()
    cache.clear = MagicMock()
    cache.invalidate_subject = MagicMock()
    cache.invalidate_object = MagicMock()
    return cache


@pytest.fixture
def boundary_cache() -> MagicMock:
    """Mock boundary cache with standard interface."""
    cache = MagicMock()
    cache.clear = MagicMock()
    cache.invalidate_permission_change = MagicMock()
    return cache


@pytest.fixture
def iterator_cache() -> MagicMock:
    """Mock iterator cache with standard interface."""
    cache = MagicMock()
    cache.invalidate_zone = MagicMock()
    cache.clear = MagicMock()
    return cache


@pytest.fixture
def coordinator(l1_cache, boundary_cache, iterator_cache) -> CacheCoordinator:
    """CacheCoordinator wired up with mock caches."""
    return CacheCoordinator(
        l1_cache=l1_cache,
        boundary_cache=boundary_cache,
        iterator_cache=iterator_cache,
        zone_graph_cache={"zone-a": {"tuples": []}, "zone-b": {"tuples": []}},
    )


# ---------------------------------------------------------------------------
# Tests — Invalidation ordering
# ---------------------------------------------------------------------------


class TestCacheCoordinatorCriticalPaths:
    """Tests for coordinator invalidation ordering and callback dispatch."""

    def test_invalidate_for_write_calls_all_layers_in_order(
        self, coordinator, l1_cache, boundary_cache, iterator_cache
    ):
        """All cache layers must be invalidated in the documented order."""
        call_order: list[str] = []

        def boundary_cb(zone_id, subj_type, subj_id, perm, obj_path):
            call_order.append("boundary_cb")

        def visibility_cb(zone_id, obj_path):
            call_order.append("visibility_cb")

        def namespace_cb(subj_type, subj_id, zone_id):
            call_order.append("namespace_cb")

        coordinator.register_boundary_invalidator("b1", boundary_cb)
        coordinator.register_visibility_invalidator("v1", visibility_cb)
        coordinator.register_namespace_invalidator("n1", namespace_cb)

        # Patch internal caches to record call order
        l1_cache.invalidate_subject.side_effect = lambda *a, **kw: call_order.append("l1")
        iterator_cache.invalidate_zone.side_effect = lambda *a, **kw: call_order.append("iterator")

        # Act -- use a file object so boundary/visibility callbacks fire
        coordinator.invalidate_for_write(
            zone_id="zone-a",
            subject=("user", "alice"),
            relation="direct_editor",
            object=("file", "/doc.txt"),
        )

        # Assert -- all layers called
        assert "l1" in call_order
        assert "boundary_cb" in call_order
        assert "visibility_cb" in call_order
        assert "namespace_cb" in call_order
        assert "iterator" in call_order

        # L1 before iterator (step 2 before step 6)
        assert call_order.index("l1") < call_order.index("iterator")
        # Boundary before visibility (step 3 before step 4)
        assert call_order.index("boundary_cb") < call_order.index("visibility_cb")
        # Visibility before namespace (step 4 before step 5)
        assert call_order.index("visibility_cb") < call_order.index("namespace_cb")

    def test_boundary_callback_invocation_order(self, coordinator):
        """Boundary invalidators must fire in registration order.

        The coordinator iterates callbacks in the outer loop and permissions
        in the inner loop.  For a relation like ``direct_editor`` that maps
        to [read, write], callback-1 fires for both permissions before
        callback-2 fires.
        """
        call_order: list[str] = []

        def first_cb(zone_id, subj_type, subj_id, perm, obj_path):
            call_order.append(f"first:{perm}")

        def second_cb(zone_id, subj_type, subj_id, perm, obj_path):
            call_order.append(f"second:{perm}")

        coordinator.register_boundary_invalidator("b-first", first_cb)
        coordinator.register_boundary_invalidator("b-second", second_cb)

        coordinator.invalidate_for_write(
            zone_id="zone-a",
            subject=("user", "alice"),
            relation="direct_editor",
            object=("file", "/doc.txt"),
        )

        # first_cb handles all permissions before second_cb starts
        first_indices = [i for i, c in enumerate(call_order) if c.startswith("first:")]
        second_indices = [i for i, c in enumerate(call_order) if c.startswith("second:")]
        assert len(first_indices) >= 1
        assert len(second_indices) >= 1
        assert max(first_indices) < min(second_indices)

    def test_boundary_callback_failure_does_not_block_others(self, coordinator):
        """A failing boundary callback must not prevent subsequent callbacks."""
        second_called = False

        def failing_cb(zone_id, subj_type, subj_id, perm, obj_path):
            raise RuntimeError("boom")

        def second_cb(zone_id, subj_type, subj_id, perm, obj_path):
            nonlocal second_called
            second_called = True

        coordinator.register_boundary_invalidator("fail", failing_cb)
        coordinator.register_boundary_invalidator("ok", second_cb)

        # Should not raise
        coordinator.invalidate_for_write(
            zone_id="zone-a",
            subject=("user", "alice"),
            relation="direct_viewer",
            object=("file", "/doc.txt"),
        )

        assert second_called is True

    def test_visibility_callback_failure_isolation(self, coordinator):
        """A failing visibility callback must not prevent subsequent callbacks."""
        second_called = False

        def failing_cb(zone_id, obj_path):
            raise ValueError("vis-boom")

        def second_cb(zone_id, obj_path):
            nonlocal second_called
            second_called = True

        coordinator.register_visibility_invalidator("fail", failing_cb)
        coordinator.register_visibility_invalidator("ok", second_cb)

        coordinator.invalidate_for_write(
            zone_id="zone-a",
            subject=("user", "alice"),
            relation="viewer",
            object=("file", "/doc.txt"),
        )

        assert second_called is True

    def test_namespace_callback_failure_isolation(self, coordinator):
        """A failing namespace callback must not prevent subsequent callbacks."""
        second_called = False

        def failing_cb(subj_type, subj_id, zone_id):
            raise TypeError("ns-boom")

        def second_cb(subj_type, subj_id, zone_id):
            nonlocal second_called
            second_called = True

        coordinator.register_namespace_invalidator("fail", failing_cb)
        coordinator.register_namespace_invalidator("ok", second_cb)

        coordinator.invalidate_for_write(
            zone_id="zone-a",
            subject=("user", "alice"),
            relation="editor",
            object=("file", "/doc.txt"),
        )

        assert second_called is True

    def test_tiger_l2_invalidation_in_pipeline_order(self, coordinator, l1_cache, iterator_cache):
        """Tiger L2 invalidation must fire after L1 but before leases (step 2.5).

        Issue #3395: Explicit L2 Dragonfly cache invalidation for Tiger bitmaps.
        The coordinator expands relation → permissions, so the callback receives
        individual permissions (e.g. "read") not the raw relation ("direct_viewer").
        """
        call_order: list[str] = []

        def tiger_l2_cb(subj_type, subj_id, permission, res_type, zone_id):
            call_order.append("tiger_l2")

        def lease_cb(zone_id):
            call_order.append("lease")

        l1_cache.invalidate_subject.side_effect = lambda *a, **kw: call_order.append("l1")
        iterator_cache.invalidate_zone.side_effect = lambda *a, **kw: call_order.append("iterator")

        coordinator.register_tiger_l2_invalidator("t1", tiger_l2_cb)
        coordinator.register_lease_invalidator("lease1", lease_cb)

        coordinator.invalidate_for_write(
            zone_id="zone-a",
            subject=("user", "alice"),
            relation="direct_viewer",
            object=("file", "/doc.txt"),
        )

        assert "l1" in call_order
        assert "tiger_l2" in call_order
        assert "lease" in call_order
        # L1 (step 2) < Tiger L2 (step 2.5) < leases (step 3)
        assert call_order.index("l1") < call_order.index("tiger_l2")
        assert call_order.index("tiger_l2") < call_order.index("lease")

    def test_tiger_l2_callback_failure_does_not_block_leases(self, coordinator):
        """A failing Tiger L2 callback must not prevent lease/boundary invalidation.

        Issue #3395: Fail-open — Dragonfly errors must not block the write path.
        """
        lease_called = False

        def failing_tiger_cb(subj_type, subj_id, permission, res_type, zone_id):
            raise RuntimeError("dragonfly down")

        def lease_cb(zone_id):
            nonlocal lease_called
            lease_called = True

        coordinator.register_tiger_l2_invalidator("t1", failing_tiger_cb)
        coordinator.register_lease_invalidator("lease1", lease_cb)

        # Should not raise
        coordinator.invalidate_for_write(
            zone_id="zone-a",
            subject=("user", "alice"),
            relation="direct_viewer",
            object=("file", "/doc.txt"),
        )

        assert lease_called is True
        assert coordinator.get_stats()["callback_failure_count"] >= 1

    def test_callback_failure_increments_boundary_invalidation_count(self, coordinator):
        """Even when callbacks fail, the boundary invalidation count increments."""

        def failing_boundary(zone_id, subj_type, subj_id, perm, obj_path):
            raise RuntimeError("fail")

        coordinator.register_boundary_invalidator("fail", failing_boundary)

        coordinator.invalidate_for_write(
            zone_id="zone-a",
            subject=("user", "alice"),
            relation="direct_viewer",
            object=("file", "/doc.txt"),
        )

        stats = coordinator.get_stats()
        # The boundary invalidation is counted even when individual callbacks fail
        assert stats["boundary_invalidations"] >= 1

    def test_callback_failure_logs_debug(self, coordinator, caplog):
        """Callback failures must emit a log message (at DEBUG level)."""

        def failing_ns(subj_type, subj_id, zone_id):
            raise RuntimeError("ns-fail")

        coordinator.register_namespace_invalidator("fail", failing_ns)

        with caplog.at_level(logging.DEBUG, logger="nexus.bricks.rebac.cache.coordinator"):
            coordinator.invalidate_for_write(
                zone_id="zone-a",
                subject=("user", "alice"),
                relation="viewer",
                object=("file", "/doc.txt"),
            )

        ns_messages = [r for r in caplog.records if "Namespace invalidator" in r.message]
        assert len(ns_messages) > 0


# ---------------------------------------------------------------------------
# Tests — Eager recompute
# ---------------------------------------------------------------------------


class TestCacheCoordinatorEagerRecompute:
    """Tests for the eager recompute path (_eager_recompute)."""

    def test_eager_recompute_break_on_first_failure(self):
        """Eager recompute continues past failures (break→continue fix).

        When one permission computation fails, subsequent permissions
        are still recomputed. The ``continue`` statement ensures
        partial failures don't block other permissions.
        """
        # Arrange -- namespace with two permissions mapped to "editor"
        namespace = MagicMock()
        namespace.config = {
            "relations": {
                "read": {"union": ["editor", "viewer"]},
                "write": {"union": ["editor", "owner"]},
            }
        }

        def compute_permission(subj, perm, obj, visited, depth, ctx, zone):
            if perm == "read":
                raise RuntimeError("compute read failed")
            return True

        cache_calls: list[str] = []

        def cache_result(subj, perm, obj, result, zone, conn, delta):
            cache_calls.append(perm)

        coordinator = CacheCoordinator(
            get_namespace_cb=lambda _obj_type: namespace,
            compute_permission_cb=compute_permission,
            cache_check_result_cb=cache_result,
        )

        subject = MagicMock()
        subject.entity_type = "user"
        subject.entity_id = "alice"
        obj = MagicMock()
        obj.entity_type = "file"
        obj.entity_id = "/doc.txt"

        # Act
        coordinator._eager_recompute(subject, "editor", obj, "zone-a", MagicMock())

        # Fix landed (break -> continue): "write" IS now recomputed
        # even though "read" failed.
        assert "write" in cache_calls

    def test_eager_recompute_fallback_on_failure(self):
        """If all compute callbacks fail, no crash and stats are intact."""
        namespace = MagicMock()
        namespace.config = {
            "relations": {
                "read": {"union": ["editor"]},
                "write": {"union": ["editor"]},
            }
        }

        def compute_always_fail(subj, perm, obj, visited, depth, ctx, zone):
            raise RuntimeError("total failure")

        coordinator = CacheCoordinator(
            get_namespace_cb=lambda _obj_type: namespace,
            compute_permission_cb=compute_always_fail,
            cache_check_result_cb=MagicMock(),
        )

        subject = MagicMock()
        subject.entity_type = "user"
        subject.entity_id = "alice"
        obj = MagicMock()
        obj.entity_type = "file"
        obj.entity_id = "/doc.txt"

        # Act -- should not raise
        coordinator._eager_recompute(subject, "editor", obj, "zone-a", MagicMock())

        # Assert -- coordinator still returns sensible stats
        stats = coordinator.get_stats()
        assert isinstance(stats, dict)
        assert "total_invalidations" in stats


# ---------------------------------------------------------------------------
# Tests — Zone graph and bulk invalidation
# ---------------------------------------------------------------------------


class TestCacheCoordinatorZoneGraphAndBulk:
    """Tests for zone graph invalidation and invalidate_all."""

    def test_invalidate_zone_graph_specific_zone(self):
        """Invalidating a specific zone must leave other zones intact."""
        zone_cache: dict = {"zone-a": {"data": 1}, "zone-b": {"data": 2}}
        coordinator = CacheCoordinator(zone_graph_cache=zone_cache)

        coordinator.invalidate_zone_graph("zone-a")

        assert "zone-a" not in zone_cache
        assert "zone-b" in zone_cache
        assert zone_cache["zone-b"] == {"data": 2}

    def test_invalidate_all_clears_everything(self):
        """invalidate_all(None) must clear all cache layers."""
        l1 = MagicMock()
        boundary = MagicMock()
        iterator = MagicMock()
        zone_cache: dict = {"zone-a": {"data": 1}, "zone-b": {"data": 2}}
        coordinator = CacheCoordinator(
            l1_cache=l1,
            boundary_cache=boundary,
            iterator_cache=iterator,
            zone_graph_cache=zone_cache,
        )

        coordinator.invalidate_all(zone_id=None)

        assert len(zone_cache) == 0
        l1.clear.assert_called_once()
        boundary.clear.assert_called_once()
        iterator.clear.assert_called_once()


# ---------------------------------------------------------------------------
# Tests — Callback registration lifecycle
# ---------------------------------------------------------------------------


class TestCacheCoordinatorRegistration:
    """Tests for callback registration and unregistration."""

    def test_register_unregister_callbacks(self):
        """Register and unregister each callback type cleanly."""
        coordinator = CacheCoordinator()

        # Boundary
        coordinator.register_boundary_invalidator("b1", lambda *a: None)
        assert coordinator.get_stats()["registered_boundary_invalidators"] == 1
        assert coordinator.unregister_boundary_invalidator("b1") is True
        assert coordinator.get_stats()["registered_boundary_invalidators"] == 0
        assert coordinator.unregister_boundary_invalidator("b1") is False

        # Visibility
        coordinator.register_visibility_invalidator("v1", lambda *a: None)
        assert coordinator.get_stats()["registered_visibility_invalidators"] == 1
        assert coordinator.unregister_visibility_invalidator("v1") is True
        assert coordinator.get_stats()["registered_visibility_invalidators"] == 0
        assert coordinator.unregister_visibility_invalidator("v1") is False

        # Namespace
        coordinator.register_namespace_invalidator("n1", lambda *a: None)
        assert coordinator.get_stats()["registered_namespace_invalidators"] == 1
        assert coordinator.unregister_namespace_invalidator("n1") is True
        assert coordinator.get_stats()["registered_namespace_invalidators"] == 0
        assert coordinator.unregister_namespace_invalidator("n1") is False

        # Tiger L2 (Issue #3395)
        coordinator.register_tiger_l2_invalidator("t1", lambda *a: None)
        assert coordinator.get_stats()["registered_tiger_l2_invalidators"] == 1
        assert coordinator.unregister_tiger_l2_invalidator("t1") is True
        assert coordinator.get_stats()["registered_tiger_l2_invalidators"] == 0
        assert coordinator.unregister_tiger_l2_invalidator("t1") is False

    def test_duplicate_registration_is_idempotent(self):
        """Re-registering the same callback_id must not create duplicates."""
        coordinator = CacheCoordinator()

        cb = lambda *a: None  # noqa: E731
        coordinator.register_boundary_invalidator("b1", cb)
        coordinator.register_boundary_invalidator("b1", cb)

        assert coordinator.get_stats()["registered_boundary_invalidators"] == 1
