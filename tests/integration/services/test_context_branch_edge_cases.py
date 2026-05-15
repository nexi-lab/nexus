"""Unit tests for ContextBranchService edge cases (Issue #1315, C3-A).

Tests all 8 edge cases identified during review:
1. Branch from non-existent workspace
2. Merge into self
3. Merge already-merged branch
4. Branch name collisions
5. Checkout with no snapshot
6. Delete "main" branch
7. Circular branch references (not possible by design — verify)
8. Empty workspace branching
"""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from nexus.contracts.exceptions import (
    BranchExistsError,
    BranchProtectedError,
    BranchStateError,
)
from nexus.services.workspace.context_branch import ContextBranchService
from nexus.storage.models._base import Base
from nexus.storage.models.context_branch import ContextBranchModel


@pytest.fixture
def engine():
    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    return eng


@pytest.fixture
def session_factory(engine):
    return sessionmaker(bind=engine)


@pytest.fixture
def record_store(session_factory):
    return SimpleNamespace(session_factory=session_factory)


@pytest.fixture
def service(record_store):
    wm = MagicMock()
    wm.metadata = MagicMock()
    wm.backend = MagicMock()
    wm.create_snapshot.return_value = {
        "snapshot_id": "snap-new",
        "snapshot_number": 1,
        "manifest_hash": "hash-new",
        "file_count": 0,
        "total_size_bytes": 0,
        "description": None,
        "created_by": None,
        "tags": [],
        "created_at": datetime.now(UTC),
    }
    wm.restore_snapshot.return_value = {
        "files_restored": 0,
        "files_deleted": 0,
        "snapshot_info": {"snapshot_id": "snap-new"},
    }
    return ContextBranchService(
        workspace_manager=wm,
        record_store=record_store,
        rebac_manager=None,
        default_zone_id="z1",
    )


# ==================================================================
# Edge Case 1: Branch from non-existent workspace
# ==================================================================


class TestBranchFromNonExistentWorkspace:
    """Branching on a workspace that has no snapshots should still work —
    the main branch just has head_snapshot_id=None."""

    def test_ensure_main_on_empty_workspace(self, service):
        result = service.ensure_main_branch("/empty-workspace")
        assert result.head_snapshot_id is None
        assert result.branch_name == "main"

    def test_create_branch_on_empty_workspace(self, service):
        service.ensure_main_branch("/empty-workspace")
        branch = service.create_branch("/empty-workspace", "feature")
        assert branch.head_snapshot_id is None
        assert branch.fork_point_id is None


# ==================================================================
# Edge Case 2: Merge into self
# ==================================================================


class TestMergeIntoSelf:
    def test_self_merge_raises_state_error(self, service):
        with service._session_factory() as session:
            main = ContextBranchModel(
                zone_id="z1",
                workspace_path="/ws",
                branch_name="main",
                is_current=True,
                status="active",
            )
            session.add(main)
            session.commit()

        with pytest.raises(BranchStateError, match="Cannot merge a branch into itself"):
            service.merge("/ws", "main", "main")


# ==================================================================
# Edge Case 3: Merge already-merged branch
# ==================================================================


class TestMergeAlreadyMerged:
    def test_merge_merged_branch_raises(self, service, session_factory):
        with session_factory() as session:
            main = ContextBranchModel(
                zone_id="z1",
                workspace_path="/ws",
                branch_name="main",
                is_current=True,
                status="active",
            )
            feature = ContextBranchModel(
                zone_id="z1",
                workspace_path="/ws",
                branch_name="done",
                parent_branch="main",
                status="merged",
                merged_into_branch="main",
            )
            session.add_all([main, feature])
            session.commit()

        with pytest.raises(BranchStateError, match="already merged"):
            service.merge("/ws", "done", "main")


# ==================================================================
# Edge Case 4: Branch name collisions
# ==================================================================


class TestBranchNameCollisions:
    def test_duplicate_name_raises_exists_error(self, service):
        service.ensure_main_branch("/ws")
        service.create_branch("/ws", "feature")
        with pytest.raises(BranchExistsError, match="already exists"):
            service.create_branch("/ws", "feature")

    def test_same_name_different_workspaces_ok(self, service):
        service.ensure_main_branch("/ws-a")
        service.ensure_main_branch("/ws-b")
        branch_a = service.create_branch("/ws-a", "feature")
        branch_b = service.create_branch("/ws-b", "feature")
        assert branch_a.id != branch_b.id

    def test_same_name_different_zones_ok(self, service, session_factory):
        """Same workspace, same branch name, different zones → OK."""
        # Zone z1 (default)
        service.ensure_main_branch("/ws")
        service.create_branch("/ws", "feature")

        # Zone z2 — need a separate service instance
        svc2 = ContextBranchService(
            workspace_manager=service._wm,
            record_store=SimpleNamespace(session_factory=session_factory),
            rebac_manager=None,
            default_zone_id="z2",
        )
        svc2.ensure_main_branch("/ws")
        branch = svc2.create_branch("/ws", "feature")
        assert branch.zone_id == "z2"


# ==================================================================
# Edge Case 5: Checkout with no snapshot
# ==================================================================


class TestCheckoutNoSnapshot:
    def test_checkout_branch_with_no_head(self, service):
        """Branch with head_snapshot_id=None should checkout without restoring."""
        service.ensure_main_branch("/ws")
        service.create_branch("/ws", "empty-branch")
        result = service.checkout("/ws", "empty-branch")
        assert result["head_snapshot_id"] is None
        assert result["restore_info"] is None

    def test_checkout_discarded_branch_raises(self, service):
        service.ensure_main_branch("/ws")
        service.create_branch("/ws", "temp")
        service.delete_branch("/ws", "temp")
        with pytest.raises(BranchStateError, match="status"):
            service.checkout("/ws", "temp")


# ==================================================================
# Edge Case 6: Delete "main" branch
# ==================================================================


class TestDeleteMainBranch:
    def test_delete_main_raises_protected_error(self, service):
        service.ensure_main_branch("/ws")
        with pytest.raises(BranchProtectedError, match="protected"):
            service.delete_branch("/ws", "main")

    def test_protected_check_happens_before_db_lookup(self, service):
        """BranchProtectedError should be raised even if workspace doesn't exist."""
        with pytest.raises(BranchProtectedError):
            service.delete_branch("/nonexistent-ws", "main")


# ==================================================================
# Edge Case 7: Circular references (should be impossible)
# ==================================================================


class TestCircularReferences:
    def test_no_circular_by_design(self, service):
        """Fork points are snapshot IDs (immutable), not branch names.
        Circular references cannot occur because branches reference snapshots,
        not other branches, for their fork points."""
        service.ensure_main_branch("/ws")
        branch_a = service.create_branch("/ws", "a")
        branch_b = service.create_branch("/ws", "b", from_branch="a")

        # b's fork_point is a snapshot ID, not a branch — so no circular ref
        assert branch_b.parent_branch == "a"
        assert branch_b.fork_point_id == branch_a.head_snapshot_id


# ==================================================================
# Edge Case 8: Empty workspace branching
# ==================================================================


class TestEmptyWorkspaceBranching:
    def test_branch_empty_workspace(self, service):
        """Branching on an empty workspace (no files) should work."""
        service.ensure_main_branch("/empty")
        branch = service.create_branch("/empty", "feature")
        assert branch.head_snapshot_id is None

    def test_commit_on_empty_branch(self, service):
        """Committing on a branch with no prior snapshot should work."""
        service.ensure_main_branch("/empty")
        result = service.commit("/empty", message="First commit")
        assert result["pointer_advanced"] is True
