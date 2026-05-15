"""Unit tests for ContextBranchService — core branch operations (Issue #1315).

Tests: ensure_main_branch, create_branch, list_branches, get_branch,
       get_current_branch, delete_branch, commit, checkout, log, diff.
"""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from nexus.contracts.exceptions import (
    BranchExistsError,
    BranchNotFoundError,
    BranchProtectedError,
    BranchStateError,
)
from nexus.services.workspace.context_branch import (
    DEFAULT_BRANCH,
    ContextBranchService,
    _slugify,
)
from nexus.storage.models import WorkspaceSnapshotModel
from nexus.storage.models._base import Base
from nexus.storage.models.context_branch import ContextBranchModel


@pytest.fixture
def engine():
    """In-memory SQLite engine."""
    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    return eng


@pytest.fixture
def session_factory(engine):
    """Session factory for in-memory SQLite."""
    return sessionmaker(bind=engine)


@pytest.fixture
def mock_workspace_manager():
    """Mocked WorkspaceManager with realistic behavior."""
    wm = MagicMock()
    wm.metadata = MagicMock()
    wm.backend = MagicMock()

    # Default: create_snapshot returns a valid snapshot dict
    wm.create_snapshot.return_value = {
        "snapshot_id": "snap-001",
        "snapshot_number": 1,
        "manifest_hash": "abc123",
        "file_count": 5,
        "total_size_bytes": 1024,
        "description": "test",
        "created_by": "agent-1",
        "tags": [],
        "created_at": datetime.now(UTC),
    }

    # Default: restore_snapshot returns a valid result
    wm.restore_snapshot.return_value = {
        "files_restored": 5,
        "files_deleted": 0,
        "snapshot_info": {"snapshot_id": "snap-001"},
    }

    # Default: list_snapshots returns empty
    wm.list_snapshots.return_value = []

    # Default: diff_snapshots returns empty diff
    wm.diff_snapshots.return_value = {
        "added": [],
        "removed": [],
        "modified": [],
        "unchanged": 0,
    }

    return wm


@pytest.fixture
def record_store(session_factory):
    return SimpleNamespace(session_factory=session_factory)


@pytest.fixture
def service(mock_workspace_manager, record_store):
    """ContextBranchService with mocked WM and real DB."""
    return ContextBranchService(
        workspace_manager=mock_workspace_manager,
        record_store=record_store,
        rebac_manager=None,  # No ReBAC for unit tests (allows all)
        default_zone_id="test-zone",
        default_agent_id="agent-1",
    )


def _add_snapshot(session_factory, workspace_path: str, snapshot_id: str, number: int) -> str:
    """Helper: insert a WorkspaceSnapshotModel."""
    with session_factory() as session:
        snap = WorkspaceSnapshotModel(
            snapshot_id=snapshot_id,
            workspace_path=workspace_path,
            snapshot_number=number,
            manifest_hash=f"hash-{snapshot_id}",
            file_count=3,
            total_size_bytes=512,
            description=f"Snapshot {number}",
        )
        session.add(snap)
        session.commit()
    return snapshot_id


# ==================================================================
# ensure_main_branch
# ==================================================================


class TestEnsureMainBranch:
    def test_creates_main_on_first_call(self, service, session_factory):
        result = service.ensure_main_branch("/workspace/test")
        assert result.branch_name == DEFAULT_BRANCH
        assert result.is_current is True
        assert result.status == "active"
        assert result.zone_id == "test-zone"

    def test_idempotent_on_second_call(self, service, session_factory):
        r1 = service.ensure_main_branch("/workspace/test")
        r2 = service.ensure_main_branch("/workspace/test")
        assert r1.id == r2.id

    def test_picks_up_latest_snapshot(self, service, session_factory):
        _add_snapshot(session_factory, "/workspace/test", "snap-latest", 1)
        result = service.ensure_main_branch("/workspace/test")
        assert result.head_snapshot_id == "snap-latest"

    def test_no_snapshot_head_is_none(self, service, session_factory):
        result = service.ensure_main_branch("/workspace/test")
        assert result.head_snapshot_id is None


# ==================================================================
# create_branch
# ==================================================================


class TestCreateBranch:
    def test_creates_branch_from_main(self, service, session_factory):
        _add_snapshot(session_factory, "/ws", "snap-1", 1)
        service.ensure_main_branch("/ws")

        branch = service.create_branch("/ws", "feature-x")
        assert branch.branch_name == "feature-x"
        assert branch.parent_branch == DEFAULT_BRANCH
        assert branch.status == "active"
        assert branch.is_current is False

    def test_creates_branch_from_specific_snapshot(self, service, session_factory):
        snap_id = _add_snapshot(session_factory, "/ws", "snap-42", 1)
        branch = service.create_branch("/ws", "hotfix", from_snapshot_id=snap_id)
        assert branch.fork_point_id == snap_id
        assert branch.head_snapshot_id == snap_id

    def test_creates_branch_from_named_branch(self, service, session_factory):
        _add_snapshot(session_factory, "/ws", "snap-1", 1)
        service.ensure_main_branch("/ws")
        service.create_branch("/ws", "dev")
        branch = service.create_branch("/ws", "feature-from-dev", from_branch="dev")
        assert branch.parent_branch == "dev"

    def test_duplicate_name_raises(self, service, session_factory):
        service.ensure_main_branch("/ws")
        service.create_branch("/ws", "dup")
        with pytest.raises(BranchExistsError) as exc_info:
            service.create_branch("/ws", "dup")
        assert "already exists" in str(exc_info.value)

    def test_from_nonexistent_branch_raises(self, service, session_factory):
        service.ensure_main_branch("/ws")
        with pytest.raises(BranchNotFoundError):
            service.create_branch("/ws", "new", from_branch="ghost")

    def test_from_nonexistent_snapshot_raises(self, service, session_factory):
        from nexus.contracts.exceptions import NexusFileNotFoundError

        with pytest.raises(NexusFileNotFoundError):
            service.create_branch("/ws", "new", from_snapshot_id="nonexistent")


# ==================================================================
# list_branches / get_branch / get_current_branch
# ==================================================================


class TestBranchQueries:
    def test_list_returns_all_active(self, service, session_factory):
        service.ensure_main_branch("/ws")
        service.create_branch("/ws", "a")
        service.create_branch("/ws", "b")
        branches = service.list_branches("/ws")
        names = [b.branch_name for b in branches]
        assert DEFAULT_BRANCH in names
        assert "a" in names
        assert "b" in names

    def test_list_excludes_inactive_by_default(self, service, session_factory):
        service.ensure_main_branch("/ws")
        service.create_branch("/ws", "temp")
        service.delete_branch("/ws", "temp")
        branches = service.list_branches("/ws")
        assert all(b.branch_name != "temp" for b in branches)

    def test_list_includes_inactive_when_requested(self, service, session_factory):
        service.ensure_main_branch("/ws")
        service.create_branch("/ws", "temp")
        service.delete_branch("/ws", "temp")
        branches = service.list_branches("/ws", include_inactive=True)
        assert any(b.branch_name == "temp" for b in branches)

    def test_get_branch_found(self, service, session_factory):
        service.ensure_main_branch("/ws")
        service.create_branch("/ws", "dev")
        branch = service.get_branch("/ws", "dev")
        assert branch.branch_name == "dev"

    def test_get_branch_not_found(self, service, session_factory):
        service.ensure_main_branch("/ws")
        with pytest.raises(BranchNotFoundError):
            service.get_branch("/ws", "ghost")

    def test_get_current_branch_returns_main_by_default(self, service, session_factory):
        service.ensure_main_branch("/ws")
        current = service.get_current_branch("/ws")
        assert current.branch_name == DEFAULT_BRANCH


# ==================================================================
# delete_branch
# ==================================================================


class TestDeleteBranch:
    def test_delete_marks_discarded(self, service, session_factory):
        service.ensure_main_branch("/ws")
        service.create_branch("/ws", "temp")
        result = service.delete_branch("/ws", "temp")
        assert result.status == "discarded"

    def test_delete_main_raises(self, service, session_factory):
        service.ensure_main_branch("/ws")
        with pytest.raises(BranchProtectedError) as exc_info:
            service.delete_branch("/ws", "main")
        assert "protected" in str(exc_info.value).lower()

    def test_delete_nonexistent_raises(self, service, session_factory):
        service.ensure_main_branch("/ws")
        with pytest.raises(BranchNotFoundError):
            service.delete_branch("/ws", "ghost")

    def test_delete_already_merged_raises(self, service, session_factory):
        service.ensure_main_branch("/ws")
        service.create_branch("/ws", "feature")
        # Manually mark as merged
        with session_factory() as session:
            branch = session.execute(
                select(ContextBranchModel).where(ContextBranchModel.branch_name == "feature")
            ).scalar_one()
            branch.status = "merged"
            session.commit()
        with pytest.raises(BranchStateError):
            service.delete_branch("/ws", "feature")


# ==================================================================
# commit
# ==================================================================


class TestCommit:
    def test_commit_creates_snapshot_and_advances_head(
        self, service, session_factory, mock_workspace_manager
    ):
        service.ensure_main_branch("/ws")
        result = service.commit("/ws", message="Initial commit")
        assert result["branch"] == DEFAULT_BRANCH
        assert result["pointer_advanced"] is True
        mock_workspace_manager.create_snapshot.assert_called_once()

    def test_commit_to_named_branch(self, service, session_factory, mock_workspace_manager):
        service.ensure_main_branch("/ws")
        service.create_branch("/ws", "dev")
        result = service.commit("/ws", message="Dev work", branch_name="dev")
        assert result["branch"] == "dev"

    def test_commit_to_nonexistent_branch_raises(self, service, session_factory):
        service.ensure_main_branch("/ws")
        with pytest.raises(BranchNotFoundError):
            service.commit("/ws", message="Test", branch_name="ghost")

    def test_commit_to_merged_branch_raises(self, service, session_factory):
        service.ensure_main_branch("/ws")
        service.create_branch("/ws", "done")
        with session_factory() as session:
            branch = session.execute(
                select(ContextBranchModel).where(ContextBranchModel.branch_name == "done")
            ).scalar_one()
            branch.status = "merged"
            session.commit()
        with pytest.raises(BranchStateError):
            service.commit("/ws", message="Test", branch_name="done")


# ==================================================================
# checkout
# ==================================================================


class TestCheckout:
    def test_checkout_switches_current(self, service, session_factory):
        _add_snapshot(session_factory, "/ws", "snap-1", 1)
        service.ensure_main_branch("/ws")
        service.create_branch("/ws", "dev")
        result = service.checkout("/ws", "dev")
        assert result["branch"] == "dev"

        # Verify is_current flags
        current = service.get_current_branch("/ws")
        assert current.branch_name == "dev"

    def test_checkout_restores_workspace(self, service, session_factory, mock_workspace_manager):
        snap_id = _add_snapshot(session_factory, "/ws", "snap-1", 1)
        service.ensure_main_branch("/ws")
        service.create_branch("/ws", "dev", from_snapshot_id=snap_id)
        service.checkout("/ws", "dev")
        mock_workspace_manager.restore_snapshot.assert_called_once()

    def test_checkout_nonexistent_raises(self, service, session_factory):
        service.ensure_main_branch("/ws")
        with pytest.raises(BranchNotFoundError):
            service.checkout("/ws", "ghost")

    def test_checkout_discarded_branch_raises(self, service, session_factory):
        service.ensure_main_branch("/ws")
        service.create_branch("/ws", "temp")
        service.delete_branch("/ws", "temp")
        with pytest.raises(BranchStateError):
            service.checkout("/ws", "temp")


# ==================================================================
# log / diff (delegation tests)
# ==================================================================


class TestLogDiff:
    def test_log_delegates_to_workspace_manager(self, service, mock_workspace_manager):
        service.log("/ws", limit=10)
        mock_workspace_manager.list_snapshots.assert_called_once_with(
            workspace_path="/ws",
            limit=10,
            user_id=None,
            agent_id=None,
            zone_id=None,
        )

    def test_diff_delegates_to_workspace_manager(self, service, mock_workspace_manager):
        service.diff("/ws", "snap-1", "snap-2")
        mock_workspace_manager.diff_snapshots.assert_called_once_with(
            snapshot_id_1="snap-1",
            snapshot_id_2="snap-2",
            user_id=None,
            agent_id=None,
            zone_id=None,
        )


# ==================================================================
# _slugify helper
# ==================================================================


class TestSlugify:
    def test_basic_slugification(self):
        assert _slugify("Try Event Sourcing Refactor") == "try-event-sourcing-refactor"

    def test_special_characters_removed(self):
        assert _slugify("Fix: the bug! (v2)") == "fix-the-bug-v2"

    def test_multiple_spaces_collapsed(self):
        assert _slugify("a   b   c") == "a-b-c"

    def test_leading_trailing_hyphens_stripped(self):
        assert _slugify("--hello--") == "hello"

    def test_empty_string_returns_default(self):
        assert _slugify("!!!") == "unnamed-branch"

    def test_truncated_to_64_chars(self):
        long_text = "a" * 100
        assert len(_slugify(long_text)) == 64
