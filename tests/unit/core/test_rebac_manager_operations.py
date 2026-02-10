"""Comprehensive unit tests for ReBAC operations via NexusFS.

These tests serve as the behavioral contract for async migration.
When creating async versions, the same test cases should pass
with identical behavior.

Tests cover:
- rebac_create: Create relationship tuples
- rebac_delete: Delete tuples
- rebac_check: Permission checking with graph traversal
- rebac_check_batch: Batch permission checks
- rebac_expand: Find subjects with permission
- Cross-zone isolation
- TTL/expiry handling
- Permission hierarchy

Note: Uses `direct_owner` relation which grants `read` permission in the
default ReBAC namespace configuration.
"""

from __future__ import annotations

import tempfile
import uuid
from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from nexus import LocalBackend, NexusFS
from nexus.factory import create_nexus_fs
from nexus.storage.raft_metadata_store import RaftMetadataStore
from nexus.storage.record_store import SQLAlchemyRecordStore

# Mark all tests in this module to run sequentially to avoid locking issues
# when running tests in parallel with pytest-xdist
pytestmark = pytest.mark.xdist_group(name="rebac_sqlite")


@pytest.fixture
def temp_dir() -> Generator[Path, None, None]:
    """Create a temporary directory for tests."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def nx(temp_dir: Path) -> Generator[NexusFS, None, None]:
    """Create a NexusFS instance with ReBAC enabled."""
    nx = create_nexus_fs(
        backend=LocalBackend(temp_dir),
        metadata_store=RaftMetadataStore.embedded(str(temp_dir / "raft-metadata")),
        record_store=SQLAlchemyRecordStore(db_path=temp_dir / "metadata.db"),
        auto_parse=False,
        enforce_permissions=True,
    )
    yield nx
    nx.close()


class TestRebacCreate:
    """Tests for rebac_create method."""

    def test_create_basic_tuple(self, nx: NexusFS) -> None:
        """Test creating a basic relationship tuple."""
        result = nx.rebac_create(
            subject=("user", "alice"),
            relation="direct_owner",
            object=("file", "/doc.txt"),
            zone_id="default",
        )
        assert result is not None
        assert isinstance(result, dict)
        assert isinstance(result["tuple_id"], str)

    def test_create_with_different_zone(self, nx: NexusFS) -> None:
        """Test creating tuple with specific zone_id."""
        tuple_id = nx.rebac_create(
            subject=("user", "alice"),
            relation="direct_owner",
            object=("file", "/doc.txt"),
            zone_id="acme",
        )
        assert tuple_id is not None

    def test_create_with_expiration(self, nx: NexusFS) -> None:
        """Test creating tuple with TTL expiration."""
        future_time = datetime.now(UTC) + timedelta(hours=1)
        tuple_id = nx.rebac_create(
            subject=("user", "alice"),
            relation="direct_owner",
            object=("file", "/doc.txt"),
            zone_id="default",
            expires_at=future_time,
        )
        assert tuple_id is not None

    def test_create_userset_as_subject(self, nx: NexusFS) -> None:
        """Test creating tuple with userset-as-subject (3-tuple)."""
        # First create group membership
        nx.rebac_create(
            subject=("user", "alice"),
            relation="member-of",
            object=("group", "engineering"),
            zone_id="default",
        )
        # Then grant permission to group members
        tuple_id = nx.rebac_create(
            subject=("group", "engineering", "member"),
            relation="direct_owner",
            object=("file", "/docs/readme.txt"),
            zone_id="default",
        )
        assert tuple_id is not None

    def test_create_invalid_subject_raises(self, nx: NexusFS) -> None:
        """Test that invalid subject raises ValueError."""
        with pytest.raises(ValueError, match="subject must be"):
            nx.rebac_create(
                subject="invalid",  # type: ignore
                relation="direct_owner",
                object=("file", "/doc.txt"),
                zone_id="default",
            )

    def test_create_invalid_object_raises(self, nx: NexusFS) -> None:
        """Test that invalid object raises ValueError."""
        with pytest.raises(ValueError, match="object must be"):
            nx.rebac_create(
                subject=("user", "alice"),
                relation="direct_owner",
                object="invalid",  # type: ignore
                zone_id="default",
            )

    def test_create_prevents_cycles(self, nx: NexusFS) -> None:
        """Test that cycle detection prevents circular parent relations."""
        nx.rebac_create(
            subject=("file", "/a"),
            relation="parent",
            object=("file", "/b"),
            zone_id="default",
        )
        nx.rebac_create(
            subject=("file", "/b"),
            relation="parent",
            object=("file", "/c"),
            zone_id="default",
        )
        with pytest.raises(ValueError, match="[Cc]ycle"):
            nx.rebac_create(
                subject=("file", "/c"),
                relation="parent",
                object=("file", "/a"),
                zone_id="default",
            )


class TestRebacDelete:
    """Tests for rebac_delete method."""

    def test_delete_existing_tuple(self, nx: NexusFS) -> None:
        """Test deleting an existing tuple."""
        write_result = nx.rebac_create(
            subject=("user", "alice"),
            relation="direct_owner",
            object=("file", "/doc.txt"),
            zone_id="default",
        )
        result = nx.rebac_delete(write_result["tuple_id"])
        assert result is True

    def test_delete_nonexistent_tuple(self, nx: NexusFS) -> None:
        """Test deleting a non-existent tuple returns False."""
        fake_id = str(uuid.uuid4())
        result = nx.rebac_delete(fake_id)
        assert result is False

    def test_delete_revokes_permission(self, nx: NexusFS) -> None:
        """Test that deleting a tuple revokes the permission."""
        write_result = nx.rebac_create(
            subject=("user", "alice"),
            relation="direct_owner",
            object=("file", "/doc.txt"),
            zone_id="default",
        )
        # Verify permission exists
        assert nx.rebac_check(
            subject=("user", "alice"),
            permission="read",
            object=("file", "/doc.txt"),
            zone_id="default",
        )
        # Delete and verify permission revoked
        nx.rebac_delete(write_result["tuple_id"])
        assert not nx.rebac_check(
            subject=("user", "alice"),
            permission="read",
            object=("file", "/doc.txt"),
            zone_id="default",
        )


class TestRebacCheck:
    """Tests for rebac_check method - core permission checking."""

    def test_check_direct_permission(self, nx: NexusFS) -> None:
        """Test checking direct permission via relation."""
        nx.rebac_create(
            subject=("user", "alice"),
            relation="direct_owner",
            object=("file", "/doc.txt"),
            zone_id="default",
        )
        assert nx.rebac_check(
            subject=("user", "alice"),
            permission="read",
            object=("file", "/doc.txt"),
            zone_id="default",
        )

    def test_check_no_permission(self, nx: NexusFS) -> None:
        """Test that users without relation have no permission."""
        assert not nx.rebac_check(
            subject=("user", "unknown"),
            permission="read",
            object=("file", "/doc.txt"),
            zone_id="default",
        )

    def test_check_invalid_subject_raises(self, nx: NexusFS) -> None:
        """Test that invalid subject raises ValueError."""
        with pytest.raises(ValueError, match="subject must be"):
            nx.rebac_check(
                subject="invalid",  # type: ignore
                permission="read",
                object=("file", "/doc.txt"),
                zone_id="default",
            )

    def test_check_invalid_object_raises(self, nx: NexusFS) -> None:
        """Test that invalid object raises ValueError."""
        with pytest.raises(ValueError, match="object must be"):
            nx.rebac_check(
                subject=("user", "alice"),
                permission="read",
                object="invalid",  # type: ignore
                zone_id="default",
            )

    def test_check_through_group_membership(self, nx: NexusFS) -> None:
        """Test permission inheritance through group membership."""
        # Create group membership
        nx.rebac_create(
            subject=("user", "alice"),
            relation="member-of",
            object=("group", "engineering"),
            zone_id="default",
        )
        # Grant permission to group members
        nx.rebac_create(
            subject=("group", "engineering", "member"),
            relation="direct_owner",
            object=("file", "/team-docs/readme.txt"),
            zone_id="default",
        )
        # Alice should have access through group
        assert nx.rebac_check(
            subject=("user", "alice"),
            permission="read",
            object=("file", "/team-docs/readme.txt"),
            zone_id="default",
        )
        # Non-member should not have access
        assert not nx.rebac_check(
            subject=("user", "bob"),
            permission="read",
            object=("file", "/team-docs/readme.txt"),
            zone_id="default",
        )


class TestRebacCheckBatch:
    """Tests for rebac_check_batch method."""

    def test_check_batch_multiple_permissions(self, nx: NexusFS) -> None:
        """Test batch checking multiple permissions."""
        nx.rebac_create(
            subject=("user", "batch_alice"),
            relation="direct_owner",
            object=("file", "/batch_doc1.txt"),
            zone_id="default",
        )
        nx.rebac_create(
            subject=("user", "batch_alice"),
            relation="direct_owner",
            object=("file", "/batch_doc2.txt"),
            zone_id="default",
        )
        # rebac_check_batch takes tuples: (subject, permission, object)
        checks = [
            (("user", "batch_alice"), "read", ("file", "/batch_doc1.txt")),
            (("user", "batch_alice"), "read", ("file", "/batch_doc2.txt")),
            (("user", "batch_bob"), "read", ("file", "/batch_doc1.txt")),  # No permission
        ]
        results = nx.rebac_check_batch(checks)
        assert len(results) == 3
        assert all(isinstance(r, bool) for r in results)

    def test_check_batch_empty_list(self, nx: NexusFS) -> None:
        """Test batch with empty list returns empty results."""
        results = nx.rebac_check_batch([])
        assert results == []


class TestCrossZone:
    """Tests for cross-zone isolation."""

    def test_zone_isolation(self, nx: NexusFS) -> None:
        """Test strict zone isolation."""
        nx.rebac_create(
            subject=("user", "zone_iso_alice"),
            relation="direct_owner",
            object=("file", "/zone_iso_doc.txt"),
            zone_id="iso_zone_a",
        )
        # Access in same zone
        assert nx.rebac_check(
            subject=("user", "zone_iso_alice"),
            permission="read",
            object=("file", "/zone_iso_doc.txt"),
            zone_id="iso_zone_a",
        )
        # No access in different zone
        assert not nx.rebac_check(
            subject=("user", "zone_iso_alice"),
            permission="read",
            object=("file", "/zone_iso_doc.txt"),
            zone_id="iso_zone_b",
        )

    def test_different_zones_no_cross_access(self, nx: NexusFS) -> None:
        """Test that permissions in one zone don't grant access in another."""
        nx.rebac_create(
            subject=("user", "cross_alice"),
            relation="direct_owner",
            object=("file", "/cross_doc.txt"),
            zone_id="cross_zone_a",
        )
        # Has permission in cross_zone_a
        assert nx.rebac_check(
            subject=("user", "cross_alice"),
            permission="read",
            object=("file", "/cross_doc.txt"),
            zone_id="cross_zone_a",
        )
        # No permission in cross_zone_b
        assert not nx.rebac_check(
            subject=("user", "cross_alice"),
            permission="read",
            object=("file", "/cross_doc.txt"),
            zone_id="cross_zone_b",
        )


class TestRebacExpand:
    """Tests for rebac_expand method."""

    def test_expand_returns_list(self, nx: NexusFS) -> None:
        """Test expanding to find subjects with permission returns a list."""
        nx.rebac_create(
            subject=("user", "expand_user1"),
            relation="direct_owner",
            object=("file", "/expand_test_doc.txt"),
            zone_id="default",
        )
        nx.rebac_create(
            subject=("user", "expand_user2"),
            relation="direct_owner",
            object=("file", "/expand_test_doc.txt"),
            zone_id="default",
        )
        subjects = nx.rebac_expand(
            permission="read",
            object=("file", "/expand_test_doc.txt"),
        )
        # rebac_expand returns a list
        assert isinstance(subjects, list)

    def test_expand_nonexistent_object(self, nx: NexusFS) -> None:
        """Test expand on nonexistent object returns a list."""
        subjects = nx.rebac_expand(
            permission="read",
            object=("file", "/totally_unique_nonexistent_expand.txt"),
        )
        # Should return a list (possibly empty)
        assert isinstance(subjects, list)


class TestRebacExplain:
    """Tests for rebac_explain method."""

    def test_explain_direct_access(self, nx: NexusFS) -> None:
        """Test explaining direct access path."""
        nx.rebac_create(
            subject=("user", "alice"),
            relation="direct_owner",
            object=("file", "/explain_doc.txt"),
            zone_id="default",
        )
        explanation = nx.rebac_explain(
            subject=("user", "alice"),
            permission="read",
            object=("file", "/explain_doc.txt"),
            zone_id="default",
        )
        assert explanation is not None

    def test_explain_no_access(self, nx: NexusFS) -> None:
        """Test explaining denied access."""
        explanation = nx.rebac_explain(
            subject=("user", "unknown"),
            permission="read",
            object=("file", "/explain_doc.txt"),
            zone_id="default",
        )
        assert explanation is not None


class TestConcurrency:
    """Tests for concurrent operations.

    Note: SQLite has limitations with concurrent writes, so we use
    sequential operations for reliability in tests.
    """

    def test_sequential_creates(self, nx: NexusFS) -> None:
        """Test that multiple creates work correctly."""
        results = []
        for i in range(5):
            result = nx.rebac_create(
                subject=("user", f"seq_user_{i}"),
                relation="direct_owner",
                object=("file", f"/seq_doc_{i}.txt"),
                zone_id="default",
            )
            results.append(result)
        assert len(results) == 5
        assert all(isinstance(r, dict) for r in results)

    def test_sequential_checks(self, nx: NexusFS) -> None:
        """Test that multiple permission checks work correctly."""
        import time

        # Pre-create tuples with small delay to avoid SQLite locking
        for i in range(3):
            nx.rebac_create(
                subject=("user", f"seqcheck_user_{i}"),
                relation="direct_owner",
                object=("file", f"/seqcheck_doc_{i}.txt"),
                zone_id="default",
            )
            time.sleep(0.05)  # Small delay to let background cache operations complete

        # Check permissions
        results = []
        for i in range(3):
            result = nx.rebac_check(
                subject=("user", f"seqcheck_user_{i}"),
                permission="read",
                object=("file", f"/seqcheck_doc_{i}.txt"),
                zone_id="default",
            )
            results.append(result)
            time.sleep(0.05)  # Small delay for cache sync
        assert len(results) == 3
        assert all(r is True for r in results)


class TestCacheBehavior:
    """Tests for cache behavior."""

    def test_cache_invalidated_on_write(self, nx: NexusFS) -> None:
        """Test that cache is invalidated when tuples are written."""
        # Initially no access
        assert not nx.rebac_check(
            subject=("user", "cache_alice"),
            permission="read",
            object=("file", "/cache_doc.txt"),
            zone_id="default",
        )
        # Write tuple
        nx.rebac_create(
            subject=("user", "cache_alice"),
            relation="direct_owner",
            object=("file", "/cache_doc.txt"),
            zone_id="default",
        )
        # Now should have access (cache invalidated)
        assert nx.rebac_check(
            subject=("user", "cache_alice"),
            permission="read",
            object=("file", "/cache_doc.txt"),
            zone_id="default",
        )

    def test_cache_hit_on_repeated_check(self, nx: NexusFS) -> None:
        """Test that repeated checks use cache."""
        nx.rebac_create(
            subject=("user", "repeat_alice"),
            relation="direct_owner",
            object=("file", "/repeat_doc.txt"),
            zone_id="default",
        )
        # Multiple checks should all succeed (using cache)
        for _ in range(5):
            assert nx.rebac_check(
                subject=("user", "repeat_alice"),
                permission="read",
                object=("file", "/repeat_doc.txt"),
                zone_id="default",
            )


class TestRebacListTuples:
    """Tests for rebac_list_tuples method."""

    def test_list_tuples_by_subject(self, nx: NexusFS) -> None:
        """Test listing tuples by subject."""
        nx.rebac_create(
            subject=("user", "list_alice"),
            relation="direct_owner",
            object=("file", "/list_doc1.txt"),
            zone_id="default",
        )
        nx.rebac_create(
            subject=("user", "list_alice"),
            relation="direct_owner",
            object=("file", "/list_doc2.txt"),
            zone_id="default",
        )
        tuples = nx.rebac_list_tuples(
            subject=("user", "list_alice"),
        )
        assert len(tuples) >= 2

    def test_list_tuples_by_object(self, nx: NexusFS) -> None:
        """Test listing tuples by object."""
        nx.rebac_create(
            subject=("user", "obj_alice"),
            relation="direct_owner",
            object=("file", "/obj_doc.txt"),
            zone_id="default",
        )
        nx.rebac_create(
            subject=("user", "obj_bob"),
            relation="direct_owner",
            object=("file", "/obj_doc.txt"),
            zone_id="default",
        )
        tuples = nx.rebac_list_tuples(
            object=("file", "/obj_doc.txt"),
        )
        assert len(tuples) >= 2

    def test_list_tuples_by_relation(self, nx: NexusFS) -> None:
        """Test listing tuples by relation."""
        nx.rebac_create(
            subject=("user", "rel_alice"),
            relation="direct_owner",
            object=("file", "/rel_doc.txt"),
            zone_id="default",
        )
        tuples = nx.rebac_list_tuples(
            relation="direct_owner",
        )
        assert len(tuples) >= 1
