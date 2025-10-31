"""Unit tests for ReBAC (Relationship-Based Access Control).

Tests cover:
- Direct relationship checks
- Inherited permissions via graph traversal
- Union relations
- TupleToUserset expansion
- Caching with TTL
- Expiring tuples
- Cycle detection
- Expand API
"""

from datetime import UTC, datetime, timedelta

import pytest
from freezegun import freeze_time
from sqlalchemy import create_engine

from nexus.core.rebac import Entity, NamespaceConfig
from nexus.core.rebac_manager import ReBACManager
from nexus.storage.models import Base


@pytest.fixture
def engine():
    """Create in-memory SQLite database for testing."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return engine


@pytest.fixture
def rebac_manager(engine):
    """Create a ReBAC manager for testing with 5-minute cache TTL."""
    manager = ReBACManager(
        engine=engine,
        cache_ttl_seconds=300,  # 5 minutes for normal tests
        max_depth=10,
    )
    yield manager
    manager.close()


@pytest.fixture
def rebac_manager_fast_cache(engine):
    """Create a ReBAC manager with fast cache expiration for cache testing."""
    manager = ReBACManager(
        engine=engine,
        cache_ttl_seconds=1,  # 1 second for cache expiration tests
        max_depth=10,
    )
    yield manager
    manager.close()


def test_direct_relationship(rebac_manager):
    """Test direct relationship check."""
    # Create a direct relationship: alice member-of eng-team
    rebac_manager.rebac_write(
        subject=("agent", "alice"),
        relation="member-of",
        object=("group", "eng-team"),
    )

    # Check if alice is member of eng-team
    result = rebac_manager.rebac_check(
        subject=("agent", "alice"),
        permission="member-of",
        object=("group", "eng-team"),
    )
    assert result is True

    # Check if bob is member of eng-team (should be False)
    result = rebac_manager.rebac_check(
        subject=("agent", "bob"),
        permission="member-of",
        object=("group", "eng-team"),
    )
    assert result is False


def test_inherited_permission_via_group(rebac_manager):
    """Test inherited permission via group membership.

    Scenario:
    - alice is member-of eng-team
    - eng-team is owner-of file123
    - alice should have owner permission on file123
    """
    # Create namespace config for file with group-based permissions
    namespace = NamespaceConfig(
        namespace_id="file-ns",
        object_type="file",
        config={
            "relations": {
                "owner": {"union": ["direct_owner", "group_owner"]},
                "direct_owner": {},
                "group_owner": {
                    "tupleToUserset": {"tupleset": "owned_by_group", "computedUserset": "member-of"}
                },
            }
        },
    )
    rebac_manager.create_namespace(namespace)

    # alice is member of eng-team
    rebac_manager.rebac_write(
        subject=("agent", "alice"),
        relation="member-of",
        object=("group", "eng-team"),
    )

    # file123 is owned by eng-team
    rebac_manager.rebac_write(
        subject=("group", "eng-team"),
        relation="owned_by_group",
        object=("file", "file123"),
    )

    # alice should have member-of permission on eng-team
    result = rebac_manager.rebac_check(
        subject=("agent", "alice"),
        permission="member-of",
        object=("group", "eng-team"),
    )
    assert result is True


def test_hierarchical_permission_parent_child(rebac_manager):
    """Test hierarchical permission via parent-child relationship.

    Scenario:
    - alice is owner-of folder/parent
    - folder/parent is parent-of folder/child
    - alice should have owner permission on folder/child
    """
    # Create namespace config for file with parent inheritance
    namespace = NamespaceConfig(
        namespace_id="file-ns",
        object_type="file",
        config={
            "relations": {
                "owner": {"union": ["direct_owner", "parent_owner"]},
                "direct_owner": {},
                "parent_owner": {
                    "tupleToUserset": {"tupleset": "parent", "computedUserset": "owner"}
                },
            }
        },
    )
    rebac_manager.create_namespace(namespace)

    # alice is direct owner of parent folder
    rebac_manager.rebac_write(
        subject=("agent", "alice"),
        relation="direct_owner",
        object=("file", "folder_parent"),
    )

    # child folder has parent folder as parent
    rebac_manager.rebac_write(
        subject=("file", "folder_child"),
        relation="parent",
        object=("file", "folder_parent"),
    )

    # alice should have direct_owner permission on parent
    result = rebac_manager.rebac_check(
        subject=("agent", "alice"),
        permission="direct_owner",
        object=("file", "folder_parent"),
    )
    assert result is True

    # alice should have owner permission on parent (via union)
    result = rebac_manager.rebac_check(
        subject=("agent", "alice"),
        permission="owner",
        object=("file", "folder_parent"),
    )
    assert result is True

    # alice should have owner permission on child (via parent)
    result = rebac_manager.rebac_check(
        subject=("agent", "alice"),
        permission="owner",
        object=("file", "folder_child"),
    )
    assert result is True


def test_caching(rebac_manager):
    """Test that check results are cached."""
    # Create a direct relationship
    rebac_manager.rebac_write(
        subject=("agent", "alice"),
        relation="member-of",
        object=("group", "eng-team"),
    )

    # First check - should compute and cache
    result1 = rebac_manager.rebac_check(
        subject=("agent", "alice"),
        permission="member-of",
        object=("group", "eng-team"),
    )
    assert result1 is True

    # Second check - should use cache
    result2 = rebac_manager.rebac_check(
        subject=("agent", "alice"),
        permission="member-of",
        object=("group", "eng-team"),
    )
    assert result2 is True

    # Verify cache entry exists
    cached = rebac_manager._get_cached_check(
        Entity("agent", "alice"),
        "member-of",
        Entity("group", "eng-team"),
    )
    assert cached is True


def test_cache_invalidation_on_write(rebac_manager):
    """Test that cache is invalidated when tuples for the same subject-object pair are added."""
    # Create initial relationship
    rebac_manager.rebac_write(
        subject=("agent", "alice"),
        relation="viewer-of",
        object=("file", "file123"),
    )

    # Check and cache result
    result = rebac_manager.rebac_check(
        subject=("agent", "alice"),
        permission="viewer-of",
        object=("file", "file123"),
    )
    assert result is True

    # Add another relationship for same subject-object pair (should invalidate cache)
    rebac_manager.rebac_write(
        subject=("agent", "alice"),
        relation="editor-of",
        object=("file", "file123"),
    )

    # Cache should be invalidated for this specific subject-object pair
    cached = rebac_manager._get_cached_check(
        Entity("agent", "alice"),
        "viewer-of",
        Entity("file", "file123"),
    )
    # After invalidation, cache should be empty (None)
    assert cached is None


def test_cache_invalidation_on_delete(rebac_manager):
    """Test that cache is invalidated when tuples are deleted."""
    # Create relationship
    tuple_id = rebac_manager.rebac_write(
        subject=("agent", "alice"),
        relation="member-of",
        object=("group", "eng-team"),
    )

    # Check and cache
    result = rebac_manager.rebac_check(
        subject=("agent", "alice"),
        permission="member-of",
        object=("group", "eng-team"),
    )
    assert result is True

    # Delete relationship
    deleted = rebac_manager.rebac_delete(tuple_id)
    assert deleted is True

    # Cache should be invalidated
    cached = rebac_manager._get_cached_check(
        Entity("agent", "alice"),
        "member-of",
        Entity("group", "eng-team"),
    )
    assert cached is None

    # Check should now return False
    result = rebac_manager.rebac_check(
        subject=("agent", "alice"),
        permission="member-of",
        object=("group", "eng-team"),
    )
    assert result is False


def test_expiring_tuples(rebac_manager):
    """Test that expired tuples are not considered."""
    with freeze_time("2025-01-01 12:00:00") as frozen_time:
        # Create tuple that expires in 1 second
        expires_at = datetime.now(UTC) + timedelta(seconds=1)
        rebac_manager.rebac_write(
            subject=("agent", "alice"),
            relation="viewer-of",
            object=("file", "temp-file"),
            expires_at=expires_at,
        )

        # Check immediately - should be True
        result = rebac_manager.rebac_check(
            subject=("agent", "alice"),
            permission="viewer-of",
            object=("file", "temp-file"),
        )
        assert result is True

        # Advance time by 1.5 seconds to expire the tuple
        frozen_time.tick(delta=timedelta(seconds=1.5))

        # Check after expiration - should be False
        result = rebac_manager.rebac_check(
            subject=("agent", "alice"),
            permission="viewer-of",
            object=("file", "temp-file"),
        )
        assert result is False


def test_cycle_detection(rebac_manager):
    """Test that cycle detection prevents infinite loops.

    Scenario:
    - group1 is member-of group2
    - group2 is member-of group3
    - group3 is member-of group1 (cycle!)

    Should not cause infinite recursion.
    """
    # Create namespace for groups
    namespace = NamespaceConfig(
        namespace_id="group-ns",
        object_type="group",
        config={
            "relations": {
                "member": {"union": ["direct_member", "indirect_member"]},
                "direct_member": {},
                "indirect_member": {
                    "tupleToUserset": {"tupleset": "member-of", "computedUserset": "member"}
                },
            }
        },
    )
    rebac_manager.create_namespace(namespace)

    # Create cycle
    rebac_manager.rebac_write(
        subject=("group", "group1"),
        relation="member-of",
        object=("group", "group2"),
    )
    rebac_manager.rebac_write(
        subject=("group", "group2"),
        relation="member-of",
        object=("group", "group3"),
    )
    rebac_manager.rebac_write(
        subject=("group", "group3"),
        relation="member-of",
        object=("group", "group1"),
    )

    # This should not hang or raise an error
    result = rebac_manager.rebac_check(
        subject=("group", "group1"),
        permission="member-of",
        object=("group", "group2"),
    )
    assert result is True  # Direct relation exists


def test_expand_api_direct(rebac_manager):
    """Test expand API for finding all subjects with direct permission."""
    # Create multiple relationships
    rebac_manager.rebac_write(
        subject=("agent", "alice"),
        relation="viewer-of",
        object=("file", "file123"),
    )
    rebac_manager.rebac_write(
        subject=("agent", "bob"),
        relation="viewer-of",
        object=("file", "file123"),
    )
    rebac_manager.rebac_write(
        subject=("agent", "charlie"),
        relation="owner-of",
        object=("file", "file456"),
    )

    # Expand to find all viewers of file123
    subjects = rebac_manager.rebac_expand(
        permission="viewer-of",
        object=("file", "file123"),
    )

    assert ("agent", "alice") in subjects
    assert ("agent", "bob") in subjects
    assert ("agent", "charlie") not in subjects
    assert len(subjects) == 2


def test_expand_api_with_union(rebac_manager):
    """Test expand API with union relations.

    Scenario:
    - alice is direct_owner of file123
    - bob is direct_viewer of file123
    - owner = union(direct_owner, parent_owner)
    - viewer = union(owner, direct_viewer)

    Expanding viewer should return both alice and bob.
    """
    # Create namespace with union
    namespace = NamespaceConfig(
        namespace_id="file-ns",
        object_type="file",
        config={
            "relations": {
                "owner": {"union": ["direct_owner"]},
                "direct_owner": {},
                "viewer": {"union": ["owner", "direct_viewer"]},
                "direct_viewer": {},
            }
        },
    )
    rebac_manager.create_namespace(namespace)

    # alice is owner
    rebac_manager.rebac_write(
        subject=("agent", "alice"),
        relation="direct_owner",
        object=("file", "file123"),
    )

    # bob is viewer
    rebac_manager.rebac_write(
        subject=("agent", "bob"),
        relation="direct_viewer",
        object=("file", "file123"),
    )

    # Expand viewer - should include both alice (via owner) and bob (via direct_viewer)
    subjects = rebac_manager.rebac_expand(
        permission="viewer",
        object=("file", "file123"),
    )

    assert ("agent", "alice") in subjects
    assert ("agent", "bob") in subjects


def test_cleanup_expired_cache(rebac_manager_fast_cache):
    """Test cleanup of expired cache entries."""
    with freeze_time("2025-01-01 12:00:00") as frozen_time:
        # Create a relationship
        rebac_manager_fast_cache.rebac_write(
            subject=("agent", "alice"),
            relation="member-of",
            object=("group", "eng-team"),
        )

        # Check to create cache entry
        rebac_manager_fast_cache.rebac_check(
            subject=("agent", "alice"),
            permission="member-of",
            object=("group", "eng-team"),
        )

        # Advance time by 2 seconds to expire the cache (TTL is 1 second)
        frozen_time.tick(delta=timedelta(seconds=2))

        # Cleanup expired cache
        removed = rebac_manager_fast_cache.cleanup_expired_cache()
        assert removed > 0


def test_delete_nonexistent_tuple(rebac_manager):
    """Test deleting a non-existent tuple."""
    result = rebac_manager.rebac_delete("nonexistent-id")
    assert result is False


def test_namespace_creation_and_retrieval(rebac_manager):
    """Test creating and retrieving namespace configs."""
    # Create custom namespace
    namespace = NamespaceConfig(
        namespace_id="custom-ns",
        object_type="workspace",
        config={
            "relations": {
                "admin": {},
                "member": {"union": ["admin", "direct_member"]},
                "direct_member": {},
            }
        },
    )
    rebac_manager.create_namespace(namespace)

    # Retrieve namespace
    retrieved = rebac_manager.get_namespace("workspace")
    assert retrieved is not None
    assert retrieved.object_type == "workspace"
    assert "relations" in retrieved.config
    assert "admin" in retrieved.config["relations"]


def test_max_depth_limit(rebac_manager):
    """Test that graph traversal respects max depth limit."""
    # Create a long chain of relationships
    rebac_manager.rebac_write(
        subject=("agent", "alice"),
        relation="member-of",
        object=("group", "g0"),
    )

    for i in range(15):  # Create chain longer than max_depth (10)
        rebac_manager.rebac_write(
            subject=("group", f"g{i}"),
            relation="member-of",
            object=("group", f"g{i + 1}"),
        )

    # Direct check should work
    result = rebac_manager.rebac_check(
        subject=("agent", "alice"),
        permission="member-of",
        object=("group", "g0"),
    )
    assert result is True

    # But checking deep in the chain should fail due to depth limit
    result = rebac_manager.rebac_check(
        subject=("agent", "alice"),
        permission="member-of",
        object=("group", "g15"),
    )
    assert result is False


def test_cross_tenant_relationship_blocked(rebac_manager):
    """Test that cross-tenant relationships are blocked (tenant isolation security).

    SECURITY: This test verifies the fix for the tenant isolation bypass vulnerability
    where validation could be bypassed by passing None for tenant IDs.
    """
    # Try to create cross-tenant relationship with mismatched subject_tenant_id
    with pytest.raises(ValueError, match="Cross-tenant relationship not allowed.*subject tenant"):
        rebac_manager.rebac_write(
            subject=("user", "alice"),
            relation="viewer",
            object=("file", "doc1"),
            tenant_id="tenant_a",
            subject_tenant_id="tenant_b",  # Mismatch!
            object_tenant_id="tenant_a",
        )

    # Try to create cross-tenant relationship with mismatched object_tenant_id
    with pytest.raises(ValueError, match="Cross-tenant relationship not allowed.*object tenant"):
        rebac_manager.rebac_write(
            subject=("user", "alice"),
            relation="viewer",
            object=("file", "doc1"),
            tenant_id="tenant_a",
            subject_tenant_id="tenant_a",
            object_tenant_id="tenant_b",  # Mismatch!
        )


def test_cross_tenant_validation_with_none_tenant_ids(rebac_manager):
    """Test that providing None for tenant IDs doesn't bypass validation.

    SECURITY: This tests the fix for CVE-like vulnerability where None could bypass
    the "if A and B and C" validation logic.
    """
    # Create a tuple with tenant_id but None for subject_tenant_id
    # This should be ALLOWED (no validation when subject_tenant_id is None)
    tuple_id = rebac_manager.rebac_write(
        subject=("user", "alice"),
        relation="viewer",
        object=("file", "doc1"),
        tenant_id="tenant_a",
        subject_tenant_id=None,  # No subject tenant validation
        object_tenant_id="tenant_a",
    )
    assert tuple_id is not None

    # If we later provide a mismatched tenant, it should be blocked
    with pytest.raises(ValueError, match="Cross-tenant relationship"):
        rebac_manager.rebac_write(
            subject=("user", "bob"),
            relation="viewer",
            object=("file", "doc2"),
            tenant_id="tenant_a",
            subject_tenant_id="tenant_b",  # This MUST be validated and blocked
            object_tenant_id="tenant_a",
        )


def test_same_tenant_relationships_allowed(rebac_manager):
    """Test that same-tenant relationships are allowed."""
    # Create relationship with matching tenant IDs - should succeed
    tuple_id = rebac_manager.rebac_write(
        subject=("user", "alice"),
        relation="direct_editor",
        object=("file", "doc1"),
        tenant_id="tenant_a",
        subject_tenant_id="tenant_a",
        object_tenant_id="tenant_a",
    )
    assert tuple_id is not None

    # Verify the relationship was created
    # Check the 'editor' permission which is a union that includes 'direct_editor'
    result = rebac_manager.rebac_check(
        subject=("user", "alice"),
        permission="editor",
        object=("file", "doc1"),
        tenant_id="tenant_a",
    )
    assert result is True


def test_group_based_file_permissions_issue_338(rebac_manager):
    """Test group-based file permissions (Issue #338).

    This test verifies that permissions granted to groups are correctly
    inherited by group members through tupleToUserset traversal.

    Scenario (from Issue #338):
    - user 'joe' is a member of group 'tenant_users'
    - file '/workspace/shared' has group 'tenant_users' as direct_editor
    - user 'joe' should inherit write permission on the file

    This tests the fix for Issue #338 where group membership was not being
    traversed during permission checks.

    Note: For tupleToUserset to work, the tuple direction must be:
    [file] --[direct_editor]--> [group], not [group] --[direct_editor]--> [file]
    This allows _find_related_objects to find groups that have editor permission.
    """
    # Step 1: Create group membership relationship
    # [user, joe] --[member]--> [group, tenant_users]
    rebac_manager.rebac_write(
        subject=("user", "joe"),
        relation="member",
        object=("group", "tenant_users"),
    )

    # Step 2: Grant group permission on file
    # For tupleToUserset to work correctly, we need:
    # [file, /workspace/shared] --[direct_editor]--> [group, tenant_users]
    # This way, _find_related_objects(file, "direct_editor") will find the group
    rebac_manager.rebac_write(
        subject=("file", "/workspace/shared"),
        relation="direct_editor",
        object=("group", "tenant_users"),
    )

    # Step 3: Verify user inherits write permission via group membership
    result = rebac_manager.rebac_check(
        subject=("user", "joe"),
        permission="write",
        object=("file", "/workspace/shared"),
    )
    assert result is True, "User should inherit write permission via group membership"

    # Step 4: Verify user also inherits read permission (since editor can read)
    result = rebac_manager.rebac_check(
        subject=("user", "joe"),
        permission="read",
        object=("file", "/workspace/shared"),
    )
    assert result is True, "User should inherit read permission via group membership"

    # Step 5: Verify user does NOT have execute permission (only owner has execute)
    result = rebac_manager.rebac_check(
        subject=("user", "joe"),
        permission="execute",
        object=("file", "/workspace/shared"),
    )
    assert result is False, "User should not have execute permission (editor != owner)"

    # Step 6: Test with direct_viewer permission
    rebac_manager.rebac_write(
        subject=("file", "/workspace/public"),
        relation="direct_viewer",
        object=("group", "tenant_users"),
    )

    result = rebac_manager.rebac_check(
        subject=("user", "joe"),
        permission="read",
        object=("file", "/workspace/public"),
    )
    assert result is True, "User should inherit read permission via group viewer role"

    # Viewer should not have write permission
    result = rebac_manager.rebac_check(
        subject=("user", "joe"),
        permission="write",
        object=("file", "/workspace/public"),
    )
    assert result is False, "User should not have write permission (viewer != editor)"

    # Step 7: Test with direct_owner permission
    rebac_manager.rebac_write(
        subject=("file", "/workspace/owned"),
        relation="direct_owner",
        object=("group", "tenant_users"),
    )

    # Owner should have all permissions
    result = rebac_manager.rebac_check(
        subject=("user", "joe"),
        permission="execute",
        object=("file", "/workspace/owned"),
    )
    assert result is True, "User should inherit execute permission via group owner role"

    result = rebac_manager.rebac_check(
        subject=("user", "joe"),
        permission="write",
        object=("file", "/workspace/owned"),
    )
    assert result is True, "User should inherit write permission via group owner role"

    result = rebac_manager.rebac_check(
        subject=("user", "joe"),
        permission="read",
        object=("file", "/workspace/owned"),
    )
    assert result is True, "User should inherit read permission via group owner role"
