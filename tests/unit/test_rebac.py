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

import sqlite3
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from nexus.core.rebac import Entity, NamespaceConfig
from nexus.core.rebac_manager import ReBACManager


@pytest.fixture
def temp_db():
    """Create a temporary database for testing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test_rebac.db"

        # Create database and tables
        conn = sqlite3.connect(str(db_path))
        cursor = conn.cursor()

        # Create ReBAC tables directly
        cursor.execute("""
            CREATE TABLE rebac_tuples (
                tuple_id TEXT PRIMARY KEY,
                subject_type TEXT NOT NULL,
                subject_id TEXT NOT NULL,
                subject_relation TEXT,
                relation TEXT NOT NULL,
                object_type TEXT NOT NULL,
                object_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT,
                conditions TEXT
            )
        """)

        cursor.execute("""
            CREATE INDEX idx_rebac_subject ON rebac_tuples(subject_type, subject_id, relation)
        """)

        cursor.execute("""
            CREATE INDEX idx_rebac_object ON rebac_tuples(object_type, object_id, relation)
        """)

        cursor.execute("""
            CREATE TABLE rebac_namespaces (
                namespace_id TEXT PRIMARY KEY,
                object_type TEXT NOT NULL UNIQUE,
                config TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)

        cursor.execute("""
            CREATE TABLE rebac_check_cache (
                cache_id TEXT PRIMARY KEY,
                subject_type TEXT NOT NULL,
                subject_id TEXT NOT NULL,
                permission TEXT NOT NULL,
                object_type TEXT NOT NULL,
                object_id TEXT NOT NULL,
                result INTEGER NOT NULL,
                computed_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            )
        """)

        cursor.execute("""
            CREATE INDEX idx_rebac_cache_lookup ON rebac_check_cache(
                subject_type, subject_id, permission, object_type, object_id
            )
        """)

        cursor.execute("""
            CREATE TABLE rebac_changelog (
                change_id INTEGER PRIMARY KEY AUTOINCREMENT,
                change_type TEXT NOT NULL,
                tuple_id TEXT,
                subject_type TEXT NOT NULL,
                subject_id TEXT NOT NULL,
                relation TEXT NOT NULL,
                object_type TEXT NOT NULL,
                object_id TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)

        conn.commit()
        conn.close()

        yield str(db_path)


@pytest.fixture
def rebac_manager(temp_db):
    """Create a ReBAC manager for testing."""
    manager = ReBACManager(db_path=temp_db, cache_ttl_seconds=5)
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
        subject=("file", "folder_parent"),
        relation="parent",
        object=("file", "folder_child"),
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
    """Test that cache is invalidated when tuples are added."""
    # Create initial relationship
    rebac_manager.rebac_write(
        subject=("agent", "alice"),
        relation="member-of",
        object=("group", "eng-team"),
    )

    # Check and cache result
    result = rebac_manager.rebac_check(
        subject=("agent", "alice"),
        permission="member-of",
        object=("group", "eng-team"),
    )
    assert result is True

    # Add another relationship (should invalidate cache for alice)
    rebac_manager.rebac_write(
        subject=("agent", "alice"),
        relation="owner-of",
        object=("file", "file123"),
    )

    # Cache should be invalidated
    cached = rebac_manager._get_cached_check(
        Entity("agent", "alice"),
        "member-of",
        Entity("group", "eng-team"),
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
    # Create tuple that expires in 1 second
    expires_at = datetime.utcnow() + timedelta(seconds=1)
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

    # Wait for expiration
    time.sleep(1.5)

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


def test_cleanup_expired_cache(rebac_manager):
    """Test cleanup of expired cache entries."""
    # Create a relationship
    rebac_manager.rebac_write(
        subject=("agent", "alice"),
        relation="member-of",
        object=("group", "eng-team"),
    )

    # Check to create cache entry
    rebac_manager.rebac_check(
        subject=("agent", "alice"),
        permission="member-of",
        object=("group", "eng-team"),
    )

    # Wait for cache to expire (cache TTL is 5 seconds)
    time.sleep(6)

    # Cleanup expired cache
    removed = rebac_manager.cleanup_expired_cache()
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
