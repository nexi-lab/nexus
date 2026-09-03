"""ReBACManager ``consistency="strong"`` and revocation invalidation (Issue #4739).

- ``rebac_check`` / ``rebac_check_bulk`` with ``consistency="strong"`` never
  serve a cached decision (L1 result cache, Tiger bitmap); the fresh result
  repairs the cache.
- ``rebac_delete`` routes through ``CacheCoordinator.invalidate_for_write``
  (permission leases, Tiger L1/L2 eviction, cross-process hints) and drops the
  subject's Tiger bitmap when a *directory* grant is revoked, so revocation is
  bounded by the delete itself rather than the 3600 s Tiger TTL.

All tests use in-memory SQLite; Tiger is a MagicMock because the real bitmap
store is PostgreSQL-only.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

pytest.importorskip("pyroaring")

from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool

from nexus.bricks.rebac.cache.tiger.facade import TigerFacade
from nexus.bricks.rebac.consistency.metastore_namespace_store import MetastoreNamespaceStore
from nexus.bricks.rebac.default_namespaces import DEFAULT_FILE_NAMESPACE
from nexus.bricks.rebac.manager import AsyncReBACManager, ReBACManager
from nexus.contracts.rebac_types import ConsistencyLevel, is_strong_consistency
from nexus.storage.models import Base
from tests.testkit.metadata import InMemoryNexusFS

ALICE = ("user", "alice")
BOB = ("user", "bob")
DOC = ("file", "/ws/doc.txt")
TEAM_DIR = ("file", "/ws/team")
ZONE = "root"


def _make_manager(*, zone_aware: bool) -> ReBACManager:
    # StaticPool: one shared DBAPI connection so background/cleanup threads and
    # asyncio.to_thread see the same in-memory database (same recipe as
    # tests/unit/services/permissions/test_rebac_manager_snapshot.py).
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        isolation_level="AUTOCOMMIT",
    )
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS rebac_group_closure (
                    member_type VARCHAR(50) NOT NULL,
                    member_id VARCHAR(255) NOT NULL,
                    group_type VARCHAR(50) NOT NULL,
                    group_id VARCHAR(255) NOT NULL,
                    zone_id VARCHAR(255) NOT NULL,
                    depth INTEGER NOT NULL,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (member_type, member_id, group_type, group_id, zone_id)
                )
                """
            )
        )
    manager = ReBACManager(
        engine=engine,
        cache_ttl_seconds=300,
        max_depth=10,
        enforce_zone_isolation=zone_aware,
        enable_tiger_cache=False,  # SQLite: Tiger is mocked where needed
        namespace_store=MetastoreNamespaceStore(InMemoryNexusFS()),
    )
    manager.create_namespace(DEFAULT_FILE_NAMESPACE)
    return manager


@pytest.fixture
def base_manager():
    """enforce_zone_isolation=False — single checks consult the L1 result cache."""
    manager = _make_manager(zone_aware=False)
    try:
        yield manager
    finally:
        manager.close()


@pytest.fixture(params=[False, True], ids=["base-path", "zone-isolated"])
def any_manager(request):
    manager = _make_manager(zone_aware=request.param)
    try:
        yield manager
    finally:
        manager.close()


def _poison_l1(manager: ReBACManager, subject, permission, obj, value: bool) -> None:
    assert manager._l1_cache is not None
    manager._l1_cache.set(subject[0], subject[1], permission, obj[0], obj[1], value, ZONE)


def _attach_tiger_mock(manager: ReBACManager) -> MagicMock:
    tiger = MagicMock(name="tiger_cache")
    manager._tiger_cache = tiger
    manager._tiger_facade = TigerFacade(tiger_cache=tiger)
    return tiger


# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------


class TestConsistencyVocabulary:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (None, False),
            ("strong", True),
            (" STRONG ", True),
            ("eventual", False),
            ("bounded", False),
            (ConsistencyLevel.STRONG, True),
            (ConsistencyLevel.EVENTUAL, False),
            (object(), False),
            (True, False),
        ],
    )
    def test_is_strong_consistency(self, value, expected):
        assert is_strong_consistency(value) is expected

    def test_levels_are_plain_strings(self):
        assert ConsistencyLevel.STRONG == "strong"
        assert ConsistencyLevel("eventual") is ConsistencyLevel.EVENTUAL


# ---------------------------------------------------------------------------
# Single check
# ---------------------------------------------------------------------------


class TestSingleCheckStrong:
    def test_strong_ignores_poisoned_l1_allow(self, base_manager):
        # No tuple grants alice anything, but the L1 says "allow" (stale grant).
        _poison_l1(base_manager, ALICE, "read", DOC, True)

        assert base_manager.rebac_check(ALICE, "read", DOC, zone_id=ZONE) is True
        assert base_manager.rebac_check(ALICE, "read", DOC, zone_id=ZONE, consistency="strong") is (
            False
        )
        # The strong result repaired the cache.
        assert base_manager.rebac_check(ALICE, "read", DOC, zone_id=ZONE) is False

    def test_strong_repairs_stale_denial(self, base_manager):
        base_manager.rebac_write(ALICE, "direct_viewer", DOC, zone_id=ZONE)
        # Simulate a denial cached inside the deferred-grant window (60 s TTL).
        _poison_l1(base_manager, ALICE, "read", DOC, False)

        assert base_manager.rebac_check(ALICE, "read", DOC, zone_id=ZONE) is False
        assert (
            base_manager.rebac_check(
                ALICE, "read", DOC, zone_id=ZONE, consistency=ConsistencyLevel.STRONG
            )
            is True
        )
        assert base_manager.rebac_check(ALICE, "read", DOC, zone_id=ZONE) is True

    def test_eventual_is_the_default_and_accepts_explicit_value(self, base_manager):
        _poison_l1(base_manager, ALICE, "read", DOC, True)

        assert base_manager.rebac_check(ALICE, "read", DOC, zone_id=ZONE, consistency=None) is True
        assert (
            base_manager.rebac_check(ALICE, "read", DOC, zone_id=ZONE, consistency="eventual")
            is True
        )

    def test_strong_bypasses_tiger_allow(self, any_manager):
        tiger = _attach_tiger_mock(any_manager)
        tiger.check_access.return_value = True  # stale bitmap says allow

        assert any_manager.rebac_check(ALICE, "read", DOC, zone_id=ZONE) is True
        assert (
            any_manager.rebac_check(ALICE, "read", DOC, zone_id=ZONE, consistency="strong") is False
        )
        # The eventual check consulted Tiger; the strong one did not add a call.
        assert tiger.check_access.call_count == 1

    def test_async_facade_forwards_consistency(self, base_manager):
        _poison_l1(base_manager, ALICE, "read", DOC, True)
        facade = AsyncReBACManager(base_manager)

        cached = asyncio.run(facade.rebac_check(ALICE, "read", DOC, None, ZONE))
        fresh = asyncio.run(facade.rebac_check(ALICE, "read", DOC, None, ZONE, "strong"))

        assert cached is True
        assert fresh is False


# ---------------------------------------------------------------------------
# Bulk check
# ---------------------------------------------------------------------------


class TestBulkCheckStrong:
    def test_strong_bulk_ignores_poisoned_l1(self, any_manager):
        check = (ALICE, "read", DOC)
        _poison_l1(any_manager, ALICE, "read", DOC, True)

        assert any_manager.rebac_check_bulk([check], zone_id=ZONE)[check] is True
        assert any_manager.rebac_check_bulk([check], zone_id=ZONE, consistency="strong")[check] is (
            False
        )

    def test_strong_bulk_sees_fresh_grant_over_cached_denial(self, any_manager):
        check = (BOB, "read", DOC)
        any_manager.rebac_write(BOB, "direct_viewer", DOC, zone_id=ZONE)
        _poison_l1(any_manager, BOB, "read", DOC, False)

        assert any_manager.rebac_check_bulk([check], zone_id=ZONE)[check] is False
        assert any_manager.rebac_check_bulk([check], zone_id=ZONE, consistency="strong")[check] is (
            True
        )

    def test_strong_bulk_skips_tiger_phase(self, any_manager):
        tiger = _attach_tiger_mock(any_manager)
        tiger.check_access_bulk.return_value = {}
        check = (ALICE, "read", DOC)

        any_manager.rebac_check_bulk([check], zone_id=ZONE, consistency="strong")

        tiger.check_access_bulk.assert_not_called()


# ---------------------------------------------------------------------------
# Revocation
# ---------------------------------------------------------------------------


class TestDeleteInvalidation:
    def test_revoked_viewer_is_denied_immediately(self, any_manager):
        grant = any_manager.rebac_write(BOB, "direct_viewer", DOC, zone_id=ZONE)
        assert any_manager.rebac_check(BOB, "read", DOC, zone_id=ZONE) is True
        assert any_manager.rebac_check_bulk([(BOB, "read", DOC)], zone_id=ZONE) == {
            (BOB, "read", DOC): True
        }

        assert any_manager.rebac_delete(grant) is True

        assert any_manager.rebac_check(BOB, "read", DOC, zone_id=ZONE) is False
        assert any_manager.rebac_check_bulk([(BOB, "read", DOC)], zone_id=ZONE) == {
            (BOB, "read", DOC): False
        }

    def test_delete_notifies_lease_and_tiger_l2_invalidators(self, any_manager):
        lease_spy = MagicMock(name="lease_invalidator")
        tiger_l2_spy = MagicMock(name="tiger_l2_invalidator")
        any_manager._cache_coordinator.register_lease_invalidator("spy", lease_spy)
        any_manager._cache_coordinator.register_tiger_l2_invalidator("spy", tiger_l2_spy)

        grant = any_manager.rebac_write(BOB, "direct_viewer", DOC, zone_id=ZONE)
        lease_spy.reset_mock()
        tiger_l2_spy.reset_mock()

        any_manager.rebac_delete(grant)

        lease_spy.assert_called_once_with(ZONE, BOB, "direct_viewer", DOC)
        tiger_l2_spy.assert_called_once_with("user", "bob", "read", "file", ZONE)

    def test_grant_does_not_notify_lease_invalidators(self, any_manager):
        """A new tuple cannot make a cached allow wrong — no lease churn on writes."""
        lease_spy = MagicMock(name="lease_invalidator")
        any_manager._cache_coordinator.register_lease_invalidator("spy", lease_spy)

        any_manager.rebac_write(BOB, "direct_viewer", DOC, zone_id=ZONE)

        lease_spy.assert_not_called()

    def test_directory_grant_revoke_drops_subject_bitmap(self, any_manager):
        tiger = _attach_tiger_mock(any_manager)
        grant = any_manager.rebac_write(BOB, "direct_viewer", TEAM_DIR, zone_id=ZONE)
        tiger.reset_mock()

        any_manager.rebac_delete(grant)

        tiger.persist_single_revoke.assert_called_once_with(
            subject_type="user",
            subject_id="bob",
            permission="read",
            resource_type="file",
            resource_id="/ws/team",
            zone_id=ZONE,
        )
        tiger.remove_directory_grant.assert_called_once_with(
            subject_type="user",
            subject_id="bob",
            permission="read",
            directory_path="/ws/team",
            zone_id=ZONE,
        )
        tiger.invalidate.assert_called_once_with(
            subject_type="user",
            subject_id="bob",
            permission="read",
            resource_type="file",
            zone_id=ZONE,
        )

    def test_file_grant_revoke_keeps_rest_of_bitmap(self, any_manager):
        tiger = _attach_tiger_mock(any_manager)
        grant = any_manager.rebac_write(BOB, "direct_viewer", DOC, zone_id=ZONE)
        tiger.reset_mock()

        any_manager.rebac_delete(grant)

        tiger.persist_single_revoke.assert_called_once()
        tiger.remove_directory_grant.assert_not_called()
        tiger.invalidate.assert_not_called()

    def test_editor_directory_revoke_covers_every_permission(self, any_manager):
        tiger = _attach_tiger_mock(any_manager)
        grant = any_manager.rebac_write(BOB, "direct_editor", TEAM_DIR, zone_id=ZONE)
        tiger.reset_mock()

        any_manager.rebac_delete(grant)

        revoked = {c.kwargs["permission"] for c in tiger.persist_single_revoke.call_args_list}
        dropped = {c.kwargs["permission"] for c in tiger.invalidate.call_args_list}
        assert revoked == {"read", "write"}
        assert dropped == {"read", "write"}


# ---------------------------------------------------------------------------
# Batch APIs (rebac_check_batch / rebac_check_batch_fast / service wrappers)
# ---------------------------------------------------------------------------


class TestBatchApisStrong:
    def test_rebac_check_batch_strong_ignores_poisoned_l1(self, base_manager):
        check = (ALICE, "read", DOC)
        _poison_l1(base_manager, ALICE, "read", DOC, True)

        assert base_manager.rebac_check_batch([check]) == [True]
        assert base_manager.rebac_check_batch([check], consistency="strong") == [False]
        # The strong pass repaired the cache.
        assert base_manager.rebac_check_batch([check]) == [False]

    def test_rebac_check_batch_fast_strong_ignores_poisoned_l1(self, base_manager):
        check = (ALICE, "read", DOC)
        _poison_l1(base_manager, ALICE, "read", DOC, True)

        assert base_manager.rebac_check_batch_fast([check]) == [True]
        assert base_manager.rebac_check_batch_fast([check], consistency="strong") == [False]
        assert base_manager.rebac_check_batch_fast([check]) == [False]

    def test_service_batch_sync_forwards_consistency(self):
        from nexus.bricks.rebac.rebac_service import ReBACService

        mgr = MagicMock()
        mgr.rebac_check_batch_fast.return_value = [True]
        svc = ReBACService(rebac_manager=mgr, enable_audit_logging=False)

        assert svc.rebac_check_batch_sync([(ALICE, "read", DOC)], consistency="strong") == [True]

        assert mgr.rebac_check_batch_fast.call_args.kwargs["consistency"] == "strong"

    def test_service_batch_async_forwards_consistency_per_check(self):
        from nexus.bricks.rebac.rebac_service import ReBACService

        mgr = MagicMock()
        mgr.rebac_check.return_value = True
        svc = ReBACService(rebac_manager=mgr, enable_audit_logging=False)

        out = asyncio.run(
            svc.rebac_check_batch([(ALICE, "read", DOC)], zone_id=ZONE, consistency="strong")
        )

        assert out == [True]
        assert mgr.rebac_check.call_args.kwargs["consistency"] == "strong"
