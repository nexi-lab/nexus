"""Integration tests for namespace manager with PermissionEnforcer (Issue #1239).

Tests the full integration of namespace visibility with ReBAC permissions:
- PermissionEnforcer.check() with namespace visibility
- PermissionEnforcer.filter_list() with namespace pre-filter
- Performance validation
- Defense in depth (namespace + ReBAC)

Uses in-memory SQLite and synchronous PermissionEnforcer for fast, reliable testing.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import create_engine

from nexus.core.exceptions import NexusFileNotFoundError
from nexus.core.permissions import OperationContext, Permission, PermissionEnforcer
from nexus.services.permissions.namespace_manager import NamespaceManager
from nexus.services.permissions.rebac_manager_enhanced import EnhancedReBACManager
from nexus.storage.models import Base

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine


@pytest.fixture
def engine() -> Engine:
    """In-memory SQLite engine for tests."""
    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    return eng


@pytest.fixture
def rebac_manager(engine: Engine) -> EnhancedReBACManager:
    """EnhancedReBACManager for ReBAC grants."""
    return EnhancedReBACManager(
        engine=engine,
        cache_ttl_seconds=300,
        max_depth=10,
    )


@pytest.fixture
def namespace_manager(rebac_manager: EnhancedReBACManager) -> NamespaceManager:
    """NamespaceManager for per-subject visibility."""
    return NamespaceManager(
        rebac_manager=rebac_manager,
        cache_maxsize=10_000,
        cache_ttl=300,
        revision_window=10,
    )


@pytest.fixture
def permission_enforcer(
    rebac_manager: EnhancedReBACManager, namespace_manager: NamespaceManager
) -> PermissionEnforcer:
    """PermissionEnforcer with namespace manager integrated."""
    return PermissionEnforcer(
        rebac_manager=rebac_manager,
        namespace_manager=namespace_manager,
        allow_admin_bypass=True,
        allow_system_bypass=True,
    )


@pytest.fixture
def alice_context() -> OperationContext:
    """Operation context for user alice."""
    return OperationContext(
        subject_type="user",
        subject_id="alice",
        zone_id="test",
    )


@pytest.fixture
def bob_context() -> OperationContext:
    """Operation context for user bob."""
    return OperationContext(
        subject_type="user",
        subject_id="bob",
        zone_id="test",
    )


@pytest.fixture
def admin_context() -> OperationContext:
    """Operation context for admin user."""
    return OperationContext(
        subject_type="user",
        subject_id="admin",
        zone_id="test",
        require_admin=True,
    )


# =============================================================================
# Integration Tests: PermissionEnforcer + NamespaceManager
# =============================================================================


def test_zero_grants_raises_not_found(
    permission_enforcer: PermissionEnforcer,
    alice_context: OperationContext,
):
    """Subject with no grants gets 404 on all paths (fail-closed)."""
    # Alice has no ReBAC grants → all paths are invisible → 404
    with pytest.raises(NexusFileNotFoundError) as exc_info:
        permission_enforcer.check("/workspace/secret.txt", Permission.READ, alice_context)

    assert "not found" in str(exc_info.value).lower()


def test_per_subject_namespace_isolation(
    rebac_manager: EnhancedReBACManager,
    permission_enforcer: PermissionEnforcer,
    alice_context: OperationContext,
    bob_context: OperationContext,
):
    """Each subject sees only their granted paths."""
    alice_path = "/workspace/alice-project/data.txt"
    bob_path = "/workspace/bob-project/data.txt"

    # Grant alice viewer-of alice_path
    alice_grant = rebac_manager.rebac_write(
        subject=("user", "alice"),
        relation="direct_viewer",
        object=("file", alice_path),
        zone_id="test",
    )
    alice_tid = alice_grant.tuple_id if hasattr(alice_grant, "tuple_id") else alice_grant

    # Grant bob viewer-of bob_path
    bob_grant = rebac_manager.rebac_write(
        subject=("user", "bob"),
        relation="direct_viewer",
        object=("file", bob_path),
        zone_id="test",
    )
    bob_tid = bob_grant.tuple_id if hasattr(bob_grant, "tuple_id") else bob_grant

    # Alice can read alice_path (visible + permission granted)
    assert permission_enforcer.check(alice_path, Permission.READ, alice_context) is True

    # Alice CANNOT read bob_path (invisible → 404, not 403)
    with pytest.raises(NexusFileNotFoundError):
        permission_enforcer.check(bob_path, Permission.READ, alice_context)

    # Bob can read bob_path (visible + permission granted)
    assert permission_enforcer.check(bob_path, Permission.READ, bob_context) is True

    # Bob CANNOT read alice_path (invisible → 404)
    with pytest.raises(NexusFileNotFoundError):
        permission_enforcer.check(alice_path, Permission.READ, bob_context)

    # Cleanup
    rebac_manager.rebac_delete(tuple_id=alice_tid)
    rebac_manager.rebac_delete(tuple_id=bob_tid)


def test_admin_bypasses_namespace(
    rebac_manager: EnhancedReBACManager,
    permission_enforcer: PermissionEnforcer,
    alice_context: OperationContext,
    admin_context: OperationContext,
):
    """Admin user bypasses namespace checks and sees all paths."""
    secret_path = "/admin/secret.txt"

    # No grants for alice or admin
    # Alice cannot see it (no grant → path invisible)
    with pytest.raises(NexusFileNotFoundError):
        permission_enforcer.check(secret_path, Permission.READ, alice_context)

    # Admin CAN see it (admin bypass)
    assert permission_enforcer.check(secret_path, Permission.READ, admin_context) is True


def test_defense_in_depth_namespace_then_rebac(
    rebac_manager: EnhancedReBACManager,
    permission_enforcer: PermissionEnforcer,
    alice_context: OperationContext,
):
    """Namespace visibility check THEN ReBAC permission check (defense in depth).

    Alice has viewer-of (read-only) grant on /workspace/shared/doc.txt:
    - READ: passes namespace → passes ReBAC → allowed
    - WRITE: passes namespace → FAILS ReBAC → 403 (not 404)
    """
    doc_path = "/workspace/shared/doc.txt"

    # Grant alice VIEWER (read-only)
    grant = rebac_manager.rebac_write(
        subject=("user", "alice"),
        relation="direct_viewer",  # viewer = read permission only
        object=("file", doc_path),
        zone_id="test",
    )
    tid = grant.tuple_id if hasattr(grant, "tuple_id") else grant

    # Alice can READ (visible + read permission)
    assert permission_enforcer.check(doc_path, Permission.READ, alice_context) is True

    # Alice CANNOT WRITE (visible but no write permission → FALSE, not exception)
    # Note: PermissionEnforcer.check() returns False for permission denial,
    # raises NexusFileNotFoundError only for invisible paths
    assert permission_enforcer.check(doc_path, Permission.WRITE, alice_context) is False

    # Cleanup
    rebac_manager.rebac_delete(tuple_id=tid)


def test_grant_revocation_makes_path_invisible(
    rebac_manager: EnhancedReBACManager,
    permission_enforcer: PermissionEnforcer,
    alice_context: OperationContext,
):
    """Revoking a grant makes the path invisible (404)."""
    project_path = "/workspace/project/data.txt"

    # Grant alice viewer
    grant = rebac_manager.rebac_write(
        subject=("user", "alice"),
        relation="direct_viewer",
        object=("file", project_path),
        zone_id="test",
    )
    tid = grant.tuple_id if hasattr(grant, "tuple_id") else grant

    # Alice can read
    assert permission_enforcer.check(project_path, Permission.READ, alice_context) is True

    # Revoke grant
    rebac_manager.rebac_delete(tuple_id=tid)

    # Trigger zone revision increment
    rebac_manager._increment_zone_revision("test")

    # Alice now gets 404 (path invisible)
    with pytest.raises(NexusFileNotFoundError):
        permission_enforcer.check(project_path, Permission.READ, alice_context)


def test_filter_list_with_namespace(
    rebac_manager: EnhancedReBACManager,
    permission_enforcer: PermissionEnforcer,
    alice_context: OperationContext,
):
    """filter_list() pre-filters by namespace visibility."""
    alice_path1 = "/workspace/alice/file1.txt"
    alice_path2 = "/workspace/alice/file2.txt"
    bob_path = "/workspace/bob/file.txt"

    # Grant alice viewer on alice paths
    grant1 = rebac_manager.rebac_write(
        subject=("user", "alice"),
        relation="direct_viewer",
        object=("file", alice_path1),
        zone_id="test",
    )
    tid1 = grant1.tuple_id if hasattr(grant1, "tuple_id") else grant1

    grant2 = rebac_manager.rebac_write(
        subject=("user", "alice"),
        relation="direct_viewer",
        object=("file", alice_path2),
        zone_id="test",
    )
    tid2 = grant2.tuple_id if hasattr(grant2, "tuple_id") else grant2

    # Grant bob viewer on bob_path (alice has no grant)
    grant3 = rebac_manager.rebac_write(
        subject=("user", "bob"),
        relation="direct_viewer",
        object=("file", bob_path),
        zone_id="test",
    )
    tid3 = grant3.tuple_id if hasattr(grant3, "tuple_id") else grant3

    # filter_list for alice should only return alice paths
    all_paths = [alice_path1, alice_path2, bob_path]
    filtered = permission_enforcer.filter_list(all_paths, Permission.READ, alice_context)

    assert set(filtered) == {alice_path1, alice_path2}
    assert bob_path not in filtered

    # Cleanup
    rebac_manager.rebac_delete(tuple_id=tid1)
    rebac_manager.rebac_delete(tuple_id=tid2)
    rebac_manager.rebac_delete(tuple_id=tid3)


# =============================================================================
# Performance Tests
# =============================================================================


def test_namespace_check_performance(
    rebac_manager: EnhancedReBACManager,
    permission_enforcer: PermissionEnforcer,
    alice_context: OperationContext,
):
    """Validate namespace visibility check has acceptable performance.

    Expected: <10ms per check (O(log m) bisect lookup + ReBAC check).
    """
    # Grant alice access to 100 different paths
    paths = [f"/workspace/perf-test/file-{i:03d}.txt" for i in range(100)]
    tids = []

    for p in paths:
        grant = rebac_manager.rebac_write(
            subject=("user", "alice"),
            relation="direct_viewer",
            object=("file", p),
            zone_id="test",
        )
        tid = grant.tuple_id if hasattr(grant, "tuple_id") else grant
        tids.append(tid)

    # Test path in middle of sorted list (worst case for bisect)
    test_path = paths[50]

    # Measure 100 visibility checks
    start = time.perf_counter()
    for _ in range(100):
        assert permission_enforcer.check(test_path, Permission.READ, alice_context) is True

    elapsed_ms = (time.perf_counter() - start) * 1000
    avg_ms = elapsed_ms / 100

    print(f"\n[PERF] 100 checks: {elapsed_ms:.1f}ms total, {avg_ms:.2f}ms avg")

    # Assert: Average per-check latency should be reasonable
    # Namespace check alone: O(log 100) ≈ 7 comparisons, should be <1ms
    # Full check (namespace + ReBAC): should be <10ms
    assert avg_ms < 10, f"Namespace check too slow: {avg_ms:.2f}ms avg (expected <10ms)"

    # Cleanup
    for tid in tids:
        rebac_manager.rebac_delete(tuple_id=tid)


def test_namespace_cache_performance(
    rebac_manager: EnhancedReBACManager,
    namespace_manager: NamespaceManager,
    alice_context: OperationContext,
):
    """Validate namespace mount table cache hit performance.

    Expected: Cache hits should be <0.1ms (pure bisect lookup, no DB).
    """
    # Grant alice access to 50 paths
    paths = [f"/workspace/cache-test/file-{i:02d}.txt" for i in range(50)]
    tids = []

    for p in paths:
        grant = rebac_manager.rebac_write(
            subject=("user", "alice"),
            relation="direct_viewer",
            object=("file", p),
            zone_id="test",
        )
        tid = grant.tuple_id if hasattr(grant, "tuple_id") else grant
        tids.append(tid)

    # Prime the cache
    test_path = paths[25]
    subject = alice_context.get_subject()
    namespace_manager.is_visible(subject, test_path, "test")

    # Measure cache hits (should be pure bisect, no DB query)
    start = time.perf_counter()
    for _ in range(1000):
        assert namespace_manager.is_visible(subject, test_path, "test") is True

    elapsed_ms = (time.perf_counter() - start) * 1000
    avg_ms = elapsed_ms / 1000

    print(f"\n[CACHE-PERF] 1000 cache hits: {elapsed_ms:.1f}ms total, {avg_ms:.3f}ms avg")

    # Cache hits should be very fast (pure in-memory bisect)
    assert avg_ms < 0.1, f"Cache hit too slow: {avg_ms:.3f}ms avg (expected <0.1ms)"

    # Cleanup
    for tid in tids:
        rebac_manager.rebac_delete(tuple_id=tid)
