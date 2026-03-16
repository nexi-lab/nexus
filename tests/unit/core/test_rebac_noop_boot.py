"""Tests for NoOp ReBAC implementations + boot demotion (Issue #2440).

Validates:
- NoOp classes implement their respective Protocols structurally
- All NoOp methods return correct types (True, [], {}, etc.)
- Boot falls back to NoOp when ReBAC is disabled or fails
- File operations work correctly with NoOp ReBAC
"""

from __future__ import annotations

from nexus.contracts.noop_rebac import (
    NoOpAuditStore,
    NoOpEntityRegistry,
    NoOpPermissionEnforcer,
    NoOpReBACManager,
)
from nexus.contracts.protocols.entity_registry import EntityRegistryProtocol
from nexus.contracts.protocols.permission_enforcer import PermissionEnforcerProtocol
from nexus.contracts.protocols.rebac import ReBACBrickProtocol
from nexus.contracts.rebac_types import WriteResult

# ── Protocol compliance ─────────────────────────────────────────────


class TestNoOpProtocolCompliance:
    """Verify NoOp classes implement the runtime-checkable Protocols."""

    def test_noop_rebac_manager_protocol_compliance(self) -> None:
        mgr = NoOpReBACManager()
        assert isinstance(mgr, ReBACBrickProtocol)

    def test_noop_permission_enforcer_protocol_compliance(self) -> None:
        enf = NoOpPermissionEnforcer()
        assert isinstance(enf, PermissionEnforcerProtocol)

    def test_noop_entity_registry_protocol_compliance(self) -> None:
        reg = NoOpEntityRegistry()
        assert isinstance(reg, EntityRegistryProtocol)


# ── NoOpReBACManager return types ───────────────────────────────────


class TestNoOpReBACManager:
    """Verify all NoOpReBACManager methods return correct safe defaults."""

    def test_rebac_check_returns_true(self) -> None:
        mgr = NoOpReBACManager()
        result = mgr.rebac_check(
            subject=("user", "alice"),
            permission="read",
            object=("file", "/doc.txt"),
        )
        assert result is True

    def test_rebac_write_returns_write_result(self) -> None:
        mgr = NoOpReBACManager()
        result = mgr.rebac_write(
            subject=("user", "alice"),
            relation="viewer-of",
            object=("file", "/doc.txt"),
        )
        assert isinstance(result, WriteResult)
        assert result.tuple_id == "noop"
        assert result.revision == 0
        assert result.consistency_token == "noop"
        assert result.written_at_ms == 0.0

    def test_rebac_delete_returns_true(self) -> None:
        mgr = NoOpReBACManager()
        assert mgr.rebac_delete("some-tuple-id") is True

    def test_rebac_expand_returns_empty_list(self) -> None:
        mgr = NoOpReBACManager()
        result = mgr.rebac_expand(
            permission="read",
            object=("file", "/doc.txt"),
        )
        assert result == []

    def test_rebac_check_bulk_returns_all_true(self) -> None:
        mgr = NoOpReBACManager()
        checks = [
            (("user", "alice"), "read", ("file", "/a.txt")),
            (("user", "bob"), "write", ("file", "/b.txt")),
        ]
        result = mgr.rebac_check_bulk(checks, zone_id="z1")
        assert len(result) == 2
        assert all(v is True for v in result.values())

    def test_rebac_list_objects_returns_empty(self) -> None:
        mgr = NoOpReBACManager()
        result = mgr.rebac_list_objects(
            subject=("user", "alice"),
            permission="read",
        )
        assert result == []

    def test_rebac_list_tuples_returns_empty(self) -> None:
        mgr = NoOpReBACManager()
        result = mgr.rebac_list_tuples(
            subject=("user", "alice"),
            relation="viewer-of",
        )
        assert result == []

    def test_get_zone_revision_returns_zero(self) -> None:
        mgr = NoOpReBACManager()
        assert mgr.get_zone_revision(zone_id="z1") == 0

    def test_invalidate_zone_graph_cache_noop(self) -> None:
        mgr = NoOpReBACManager()
        mgr.invalidate_zone_graph_cache(zone_id="z1")  # should not raise

    def test_lifecycle_methods_noop(self) -> None:
        mgr = NoOpReBACManager()
        mgr.initialize()
        mgr.shutdown()
        mgr.close()
        # All should complete without raising

    def test_verify_imports_returns_empty(self) -> None:
        mgr = NoOpReBACManager()
        assert mgr.verify_imports() == {}


# ── NoOpPermissionEnforcer return types ─────────────────────────────


class TestNoOpPermissionEnforcer:
    """Verify all NoOpPermissionEnforcer methods return correct safe defaults."""

    def test_check_returns_true(self) -> None:
        enf = NoOpPermissionEnforcer()
        result = enf.check(path="/doc.txt", permission="read", context=None)
        assert result is True

    def test_filter_list_returns_all_paths(self) -> None:
        enf = NoOpPermissionEnforcer()
        paths = ["/a.txt", "/b.txt", "/c.txt"]
        result = enf.filter_list(paths=paths, context=None)
        assert result == paths
        assert result is paths  # returns same list, not a copy

    def test_has_accessible_descendants_returns_true(self) -> None:
        enf = NoOpPermissionEnforcer()
        assert enf.has_accessible_descendants(prefix="/dir/", context=None) is True

    def test_has_accessible_descendants_batch_returns_all_true(self) -> None:
        enf = NoOpPermissionEnforcer()
        prefixes = ["/dir1/", "/dir2/", "/dir3/"]
        result = enf.has_accessible_descendants_batch(prefixes=prefixes, context=None)
        assert len(result) == 3
        assert all(v is True for v in result.values())

    def test_invalidate_cache_noop(self) -> None:
        enf = NoOpPermissionEnforcer()
        enf.invalidate_cache(subject_type="user", subject_id="alice", zone_id="z1")


# ── NoOpEntityRegistry return types ─────────────────────────────────


class TestNoOpEntityRegistry:
    """Verify all NoOpEntityRegistry methods return correct safe defaults."""

    def test_register_entity_returns_none(self) -> None:
        reg = NoOpEntityRegistry()
        assert reg.register_entity("user", "alice") is None

    def test_get_entity_returns_none(self) -> None:
        reg = NoOpEntityRegistry()
        assert reg.get_entity("user", "alice") is None

    def test_lookup_entity_by_id_returns_empty(self) -> None:
        reg = NoOpEntityRegistry()
        assert reg.lookup_entity_by_id("alice") == []

    def test_get_entities_by_type_returns_empty(self) -> None:
        reg = NoOpEntityRegistry()
        assert reg.get_entities_by_type("user") == []

    def test_get_children_returns_empty(self) -> None:
        reg = NoOpEntityRegistry()
        assert reg.get_children("zone", "z1") == []

    def test_delete_entity_returns_false(self) -> None:
        reg = NoOpEntityRegistry()
        assert reg.delete_entity("user", "alice") is False


# ── NoOpAuditStore ──────────────────────────────────────────────────


class TestNoOpAuditStore:
    """Verify NoOpAuditStore close is a no-op."""

    def test_close_noop(self) -> None:
        store = NoOpAuditStore()
        store.close()  # should not raise


# ── NoOp never raises ───────────────────────────────────────────────


class TestNoOpNeverRaises:
    """Exhaustive test: every NoOp method completes without exception."""

    def test_noop_rebac_manager_never_raises(self) -> None:
        mgr = NoOpReBACManager()
        mgr.rebac_check(("user", "u"), "perm", ("file", "/f"))
        mgr.rebac_write(("user", "u"), "rel", ("file", "/f"))
        mgr.rebac_delete("tid")
        mgr.rebac_expand("perm", ("file", "/f"))
        mgr.rebac_check_bulk([], zone_id="z")
        mgr.rebac_list_objects(("user", "u"), "perm")
        mgr.rebac_list_tuples()
        mgr.get_zone_revision(None)
        mgr.invalidate_zone_graph_cache()
        mgr.register_dir_visibility_invalidator("n", lambda: None)
        mgr.initialize()
        mgr.shutdown()
        mgr.close()
        mgr.verify_imports()

    def test_noop_permission_enforcer_never_raises(self) -> None:
        enf = NoOpPermissionEnforcer()
        enf.check("/p", "r", None)
        enf.filter_list([], None)
        enf.has_accessible_descendants("/p", None)
        enf.has_accessible_descendants_batch([], None)
        enf.invalidate_cache()

    def test_noop_entity_registry_never_raises(self) -> None:
        reg = NoOpEntityRegistry()
        reg.register_entity("t", "i")
        reg.get_entity("t", "i")
        reg.lookup_entity_by_id("i")
        reg.get_entities_by_type("t")
        reg.get_children("t", "i")
        reg.delete_entity("t", "i")

    def test_noop_audit_store_never_raises(self) -> None:
        store = NoOpAuditStore()
        store.close()


# ── Boot integration (factory/_system.py) ───────────────────────────


class TestBootDemotion:
    """Verify factory boots with NoOp when ReBAC is disabled/fails."""

    def test_boot_with_rebac_disabled(self) -> None:
        """_make_gate returns False for 'permissions' → NoOp stubs selected.

        We test the gate logic + import path rather than the full boot
        (which requires DB, record store, etc.).
        """
        from nexus.factory._helpers import _make_gate

        # Simulate brick_on returning False for 'permissions'
        # (canonical brick name per deployment_profile.py BRICK_PERMISSIONS)
        gate = _make_gate(lambda name: name != "permissions")
        assert gate("permissions") is False
        assert gate("write_observer") is True

        # When gate("permissions") is False, factory code selects NoOp stubs.
        # We verify the stubs are importable from the canonical location.
        from nexus.contracts.noop_rebac import (
            NoOpAuditStore as _A,
        )
        from nexus.contracts.noop_rebac import (
            NoOpEntityRegistry as _E,
        )
        from nexus.contracts.noop_rebac import (
            NoOpPermissionEnforcer as _P,
        )
        from nexus.contracts.noop_rebac import (
            NoOpReBACManager as _R,
        )

        assert isinstance(_R(), ReBACBrickProtocol)
        assert isinstance(_P(), PermissionEnforcerProtocol)
        assert isinstance(_E(), EntityRegistryProtocol)
        _A().close()  # no-op, no error

    def test_noop_rebac_manager_is_safe_substitute(self) -> None:
        """NoOp can be used anywhere ReBACBrickProtocol is expected."""
        mgr = NoOpReBACManager()
        # Simulate usage patterns from NexusFS
        assert mgr.rebac_check(("user", "test"), "read", ("file", "/test.txt")) is True
        wr = mgr.rebac_write(("user", "test"), "viewer-of", ("file", "/test.txt"))
        assert isinstance(wr, WriteResult)
        assert mgr.rebac_delete(wr.tuple_id) is True
        assert mgr.rebac_list_tuples() == []
        assert mgr.get_zone_revision("z1") == 0
