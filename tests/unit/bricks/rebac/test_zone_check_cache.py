"""Zone-aware single-check decision cache.

The zone-aware ``rebac_check`` path lost its result cache when the L2 SQL
cache was removed — its hooks were no-ops, so every repeated check re-walked
the graph (the permissions demo's check benchmark measured 60-260 ms per
repeated check, all in ``_fresh_compute``).  Decisions are now cached and
served only while the zone's tuple revision (and the manager's tuple
version) still equal what was read before computing — so every change,
direct or indirect, is visible on the very next check.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool

from nexus.bricks.rebac.cache.zone_check_cache import ZoneRevisionCheckCache
from nexus.bricks.rebac.consistency.metastore_namespace_store import MetastoreNamespaceStore
from nexus.bricks.rebac.default_namespaces import DEFAULT_FILE_NAMESPACE, DEFAULT_GROUP_NAMESPACE
from nexus.bricks.rebac.manager import ReBACManager
from nexus.storage.models import Base
from tests.testkit.metadata import InMemoryNexusFS

ZONE = "root"
BOB = ("user", "bob")
DOC = ("file", "/ws/team/doc.txt")
TEAM_DIR = ("file", "/ws/team")
KEY = (ZONE, "user", "bob", "read", "file", "/ws/team/doc.txt")


# ── cache unit ───────────────────────────────────────────────────────────


def test_entry_is_served_only_at_its_revision() -> None:
    cache = ZoneRevisionCheckCache()
    cache.put(KEY, (3, 0), True)
    assert cache.get(KEY, (3, 0)) is True
    assert cache.get(KEY, (4, 0)) is None, "a later zone revision retires it"
    cache.put(KEY, (4, 0), False)
    assert cache.get(KEY, (4, 1)) is None, "a later tuple version retires it"


def test_ttl_and_lru_bound_the_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    import nexus.bricks.rebac.cache.zone_check_cache as mod

    now = [1_000.0]
    monkeypatch.setattr(mod.time, "monotonic", lambda: now[0])
    cache = ZoneRevisionCheckCache(max_entries=2, ttl_seconds=10)
    cache.put(("z", "a", "1", "read", "file", "/1"), (1, 0), True)
    cache.put(("z", "a", "2", "read", "file", "/2"), (1, 0), True)
    cache.put(("z", "a", "3", "read", "file", "/3"), (1, 0), True)
    assert len(cache) == 2
    assert cache.get(("z", "a", "1", "read", "file", "/1"), (1, 0)) is None
    now[0] += 11
    assert cache.get(("z", "a", "3", "read", "file", "/3"), (1, 0)) is None


# ── manager integration ──────────────────────────────────────────────────


@pytest.fixture
def manager() -> ReBACManager:
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
    mgr = ReBACManager(
        engine=engine,
        cache_ttl_seconds=300,
        max_depth=10,
        enforce_zone_isolation=True,
        enable_tiger_cache=False,
        namespace_store=MetastoreNamespaceStore(InMemoryNexusFS()),
    )
    mgr.create_namespace(DEFAULT_FILE_NAMESPACE)
    mgr.create_namespace(DEFAULT_GROUP_NAMESPACE)
    # The L1 cache and boundary cache answer some checks before the
    # zone-aware path; disable them so these tests exercise that path.
    mgr._l1_cache = None
    mgr._boundary_cache = None
    return mgr


def _count_fresh(mgr: ReBACManager) -> list[int]:
    calls = [0]
    original = mgr._fresh_compute

    def counting(*args: Any, **kwargs: Any) -> Any:
        calls[0] += 1
        return original(*args, **kwargs)

    setattr(mgr, "_fresh_compute", counting)  # noqa: B010 — instance-level patch
    return calls


def _read(mgr: ReBACManager, **kw: Any) -> bool:
    return mgr.rebac_check(BOB, "read", DOC, zone_id=ZONE, **kw)


def test_repeated_checks_skip_the_traversal(manager: ReBACManager) -> None:
    manager.rebac_write(BOB, "direct_viewer", DOC, zone_id=ZONE)
    calls = _count_fresh(manager)
    assert all(_read(manager) for _ in range(10))
    assert calls[0] == 1, "one traversal, nine cache hits"
    assert not any(
        manager.rebac_check(("user", "eve"), "read", DOC, zone_id=ZONE) for _ in range(5)
    )
    assert calls[0] == 2, "denials are cached too"


def test_strong_consistency_never_uses_the_cache(manager: ReBACManager) -> None:
    manager.rebac_write(BOB, "direct_viewer", DOC, zone_id=ZONE)
    calls = _count_fresh(manager)
    _read(manager)
    _read(manager, consistency="strong")
    _read(manager, consistency="strong")
    assert calls[0] == 3


def test_direct_revoke_is_visible_on_the_next_check(manager: ReBACManager) -> None:
    grant = manager.rebac_write(BOB, "direct_viewer", DOC, zone_id=ZONE)
    assert _read(manager) and _read(manager)
    manager.rebac_delete(grant)
    assert _read(manager) is False


def test_parent_directory_revoke_is_visible_on_the_next_check(manager: ReBACManager) -> None:
    # Indirect: the cached (bob, read, doc) decision derives from a grant on
    # the parent — exact subject/object invalidation would miss it.
    manager.rebac_write(DOC, "parent", TEAM_DIR, zone_id=ZONE)
    grant = manager.rebac_write(BOB, "direct_viewer", TEAM_DIR, zone_id=ZONE)
    assert _read(manager) and _read(manager)
    manager.rebac_delete(grant)
    assert _read(manager) is False


def test_group_membership_removal_is_visible_on_the_next_check(manager: ReBACManager) -> None:
    manager.rebac_write(("group", "eng", "member"), "direct_viewer", DOC, zone_id=ZONE)
    membership = manager.rebac_write(BOB, "member", ("group", "eng"), zone_id=ZONE)
    assert _read(manager) and _read(manager)
    manager.rebac_delete(membership)
    assert _read(manager) is False


def test_expired_grant_is_visible_on_the_next_check(manager: ReBACManager) -> None:
    manager.rebac_write(
        BOB,
        "direct_viewer",
        DOC,
        zone_id=ZONE,
        expires_at=datetime.now(UTC) + timedelta(seconds=1),
    )
    assert _read(manager) and _read(manager)
    import time

    time.sleep(1.2)
    manager._last_cleanup_time = None  # let the next check run cleanup now
    assert _read(manager) is False


def test_rename_does_not_serve_the_old_path_decision(manager: ReBACManager) -> None:
    manager.rebac_write(BOB, "direct_viewer", DOC, zone_id=ZONE)
    assert _read(manager) and _read(manager)
    manager.update_object_path(DOC[1], "/ws/team/moved.txt", "file", False)
    assert _read(manager) is False, "the grant moved with the file"
    assert manager.rebac_check(BOB, "read", ("file", "/ws/team/moved.txt"), zone_id=ZONE)


def test_namespace_change_clears_the_cache(manager: ReBACManager) -> None:
    manager.rebac_write(BOB, "direct_viewer", DOC, zone_id=ZONE)
    _read(manager)
    assert manager._zone_check_cache is not None and len(manager._zone_check_cache) == 1
    manager.create_namespace(DEFAULT_FILE_NAMESPACE)
    assert len(manager._zone_check_cache) == 0


def test_kill_switch_disables_the_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEXUS_REBAC_CHECK_CACHE", "0")
    engine = create_engine("sqlite:///:memory:", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    mgr = ReBACManager(engine=engine, enable_tiger_cache=False, enforce_zone_isolation=True)
    assert mgr._zone_check_cache is None
