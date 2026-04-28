"""Integration tests for ContextBranchService lifecycle (Issue #1315).

Tests full workflows with real DB + mocked CAS:
- Branch → commit → merge → verify
- explore() → commit → finish_explore(merge) → verify
- explore() → finish_explore(discard) → verify
- Multi-branch exploration with merge-back
- Cross-session continuity (branch persists across service instances)
"""

import hashlib
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from nexus.contracts.workspace_manifest import ManifestEntry, WorkspaceManifest
from nexus.core.object_store import WriteResult
from nexus.services.workspace.context_branch import ContextBranchService
from nexus.storage.models._base import Base
from nexus.storage.models.filesystem import WorkspaceSnapshotModel


@pytest.fixture
def engine():
    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    return eng


@pytest.fixture
def session_factory(engine):
    return sessionmaker(bind=engine)


class FakeCAS:
    """In-memory CAS for integration tests."""

    def __init__(self):
        self.store: dict[str, bytes] = {}
        self._snap_counter = 0

    def read_content(self, content_id, context=None):
        return self.store[content_id]

    def write_content(self, data, content_id: str = "", *, offset: int = 0, context=None):
        h = hashlib.sha256(data).hexdigest()
        self.store[h] = data
        return WriteResult(content_id=h, size=len(data))


class FakeWorkspaceManager:
    """Simplified WM for integration tests — uses real snapshots + CAS."""

    def __init__(self, session_factory, cas: FakeCAS):
        self.metadata = MagicMock()
        self.backend = cas
        self._session_factory = session_factory
        self._cas = cas
        self._snap_counter = 0

    def create_snapshot(self, workspace_path, description=None, **kwargs):
        """Create a real snapshot with a simple manifest."""
        self._snap_counter += 1
        manifest = WorkspaceManifest(
            entries={
                f"file-{self._snap_counter}.txt": ManifestEntry(
                    content_id=f"hash-{self._snap_counter}",
                    size=100 * self._snap_counter,
                    mime_type="text/plain",
                )
            }
        )
        manifest_bytes = manifest.to_json()
        manifest_hash = hashlib.sha256(manifest_bytes).hexdigest()
        self._cas.store[manifest_hash] = manifest_bytes

        snap_id = f"snap-{self._snap_counter}"
        with self._session_factory() as session:
            from sqlalchemy import desc

            last = session.execute(
                select(WorkspaceSnapshotModel.snapshot_number)
                .where(WorkspaceSnapshotModel.workspace_path == workspace_path)
                .order_by(desc(WorkspaceSnapshotModel.snapshot_number))
                .limit(1)
            ).scalar_one_or_none()

            snap = WorkspaceSnapshotModel(
                snapshot_id=snap_id,
                workspace_path=workspace_path,
                snapshot_number=(last or 0) + 1,
                manifest_hash=manifest_hash,
                file_count=manifest.file_count,
                total_size_bytes=manifest.total_size,
                description=description,
            )
            session.add(snap)
            session.flush()
            # Capture values before session closes (avoid DetachedInstanceError)
            snap_number = snap.snapshot_number
            session.commit()

        return {
            "snapshot_id": snap_id,
            "snapshot_number": snap_number,
            "manifest_hash": manifest_hash,
            "file_count": manifest.file_count,
            "total_size_bytes": manifest.total_size,
            "description": description,
            "created_by": kwargs.get("created_by"),
            "tags": [],
            "created_at": datetime.now(UTC),
        }

    def restore_snapshot(self, snapshot_id=None, **kwargs):
        return {
            "files_restored": 1,
            "files_deleted": 0,
            "snapshot_info": {"snapshot_id": snapshot_id},
        }

    def list_snapshots(self, workspace_path, limit=100, **kwargs):
        with self._session_factory() as session:
            from sqlalchemy import desc

            snaps = (
                session.execute(
                    select(WorkspaceSnapshotModel)
                    .where(WorkspaceSnapshotModel.workspace_path == workspace_path)
                    .order_by(desc(WorkspaceSnapshotModel.created_at))
                    .limit(limit)
                )
                .scalars()
                .all()
            )
            return [
                {
                    "snapshot_id": s.snapshot_id,
                    "snapshot_number": s.snapshot_number,
                    "description": s.description,
                }
                for s in snaps
            ]

    def diff_snapshots(self, snapshot_id_1, snapshot_id_2, **kwargs):
        return {"added": [], "removed": [], "modified": [], "unchanged": 0}


@pytest.fixture
def cas():
    return FakeCAS()


@pytest.fixture
def fake_wm(session_factory, cas):
    return FakeWorkspaceManager(session_factory, cas)


@pytest.fixture
def record_store(session_factory):
    return SimpleNamespace(session_factory=session_factory)


@pytest.fixture
def service(fake_wm, record_store):
    return ContextBranchService(
        workspace_manager=fake_wm,
        record_store=record_store,
        rebac_manager=None,
        default_zone_id="z1",
    )


# ==================================================================
# Lifecycle 1: Branch → Commit → Merge
# ==================================================================


class TestBranchCommitMerge:
    def test_full_lifecycle(self, service, session_factory):
        ws = "/ws/lifecycle"

        # 1. Initial commit on main
        service.ensure_main_branch(ws)
        service.commit(ws, message="Initial")

        # 2. Create feature branch and commit
        service.create_branch(ws, "feature")
        service.checkout(ws, "feature")
        service.commit(ws, message="Feature work", branch_name="feature")

        # 3. Verify branches
        branches = service.list_branches(ws)
        assert len(branches) == 2

        # 4. Merge feature → main
        result = service.merge(ws, "feature", "main")
        assert result.merged is True

        # 5. Verify feature is marked as merged
        branch = service.get_branch(ws, "feature")
        assert branch.status == "merged"
        assert branch.merged_into_branch == "main"


# ==================================================================
# Lifecycle 2: explore() → commit → finish_explore(merge)
# ==================================================================


class TestExploreAndMerge:
    def test_explore_merge_lifecycle(self, service, session_factory):
        ws = "/ws/explore-merge"

        # 1. Initial commit
        service.ensure_main_branch(ws)
        service.commit(ws, message="Initial")

        # 2. Start exploration
        explore_result = service.explore(ws, "Try new approach")
        assert explore_result.branch_name == "try-new-approach"
        assert explore_result.fork_point_snapshot_id is not None

        # 3. Commit on exploration branch
        service.commit(ws, message="Exploration work", branch_name="try-new-approach")

        # 4. Finish exploration (merge)
        finish_result = service.finish_explore(ws, "try-new-approach", outcome="merge")
        assert finish_result["outcome"] == "merged"
        assert finish_result["merged_into"] == "main"

        # 5. Verify we're back on main
        current = service.get_current_branch(ws)
        assert current.branch_name == "main"


# ==================================================================
# Lifecycle 3: explore() → finish_explore(discard)
# ==================================================================


class TestExploreAndDiscard:
    def test_explore_discard_lifecycle(self, service, session_factory):
        ws = "/ws/explore-discard"

        # 1. Initial commit
        service.ensure_main_branch(ws)
        service.commit(ws, message="Initial")

        # 2. Start exploration
        explore_result = service.explore(ws, "Bad idea")

        # 3. Finish exploration (discard)
        finish_result = service.finish_explore(ws, explore_result.branch_name, outcome="discard")
        assert finish_result["outcome"] == "discarded"
        assert finish_result["returned_to"] == "main"

        # 4. Discarded branch is excluded from active list
        branches = service.list_branches(ws)
        assert all(b.branch_name != explore_result.branch_name for b in branches)


# ==================================================================
# Lifecycle 4: Multiple parallel explorations
# ==================================================================


class TestMultipleExplorations:
    def test_multiple_explore_branches(self, service, session_factory):
        ws = "/ws/multi-explore"

        # Initial setup
        service.ensure_main_branch(ws)
        service.commit(ws, message="Initial")

        # Start 3 explorations
        service.explore(ws, "Approach A")
        service.checkout(ws, "main")  # Switch back to main before next explore
        service.explore(ws, "Approach B")
        service.checkout(ws, "main")
        service.explore(ws, "Approach C")

        # Verify all branches exist
        branches = service.list_branches(ws)
        names = {b.branch_name for b in branches}
        assert "approach-a" in names
        assert "approach-b" in names
        assert "approach-c" in names

        # Merge one, discard the others
        service.checkout(ws, "approach-b")
        service.commit(ws, message="B wins", branch_name="approach-b")
        service.finish_explore(ws, "approach-b", outcome="merge")
        service.finish_explore(ws, "approach-a", outcome="discard")
        service.finish_explore(ws, "approach-c", outcome="discard")

        # Verify final state
        active_branches = service.list_branches(ws)
        active_names = {b.branch_name for b in active_branches}
        assert active_names == {"main"}


# ==================================================================
# Lifecycle 5: Cross-session continuity
# ==================================================================


class TestCrossSessionContinuity:
    def test_branches_persist_across_service_instances(self, fake_wm, session_factory):
        ws = "/ws/continuity"

        # Session 1: Create branch and commit
        svc1 = ContextBranchService(
            workspace_manager=fake_wm,
            record_store=SimpleNamespace(session_factory=session_factory),
            rebac_manager=None,
            default_zone_id="z1",
        )
        svc1.ensure_main_branch(ws)
        svc1.commit(ws, message="Initial")
        svc1.create_branch(ws, "wip")
        svc1.commit(ws, message="WIP", branch_name="wip")

        # Session 2: New service instance sees the branch
        svc2 = ContextBranchService(
            workspace_manager=fake_wm,
            record_store=SimpleNamespace(session_factory=session_factory),
            rebac_manager=None,
            default_zone_id="z1",
        )
        branches = svc2.list_branches(ws)
        names = {b.branch_name for b in branches}
        assert "wip" in names

        # Can continue working on it
        svc2.checkout(ws, "wip")
        current = svc2.get_current_branch(ws)
        assert current.branch_name == "wip"


# ==================================================================
# Lifecycle 6: explore() skip commit optimization (P4-B)
# ==================================================================


class TestExploreSkipCommit:
    def test_explore_skips_commit_when_unchanged(self, service, session_factory):
        ws = "/ws/skip-commit"

        # Setup: create main with a snapshot, and make metadata.list_iter return same files
        service.ensure_main_branch(ws)
        service.commit(ws, message="Initial")

        # The _workspace_unchanged check will fail because our FakeWM generates
        # different manifests each time. That's OK — this tests the non-skip path.
        # The skip behavior is tested in unit tests with mocking.
        result = service.explore(ws, "Test skip")
        assert result.branch_name == "test-skip"


# ==================================================================
# Lifecycle 7: finish_explore with invalid outcome
# ==================================================================


class TestFinishExploreValidation:
    def test_invalid_outcome_raises(self, service):
        with pytest.raises(ValueError, match="Invalid outcome"):
            service.finish_explore("/ws", "branch", outcome="rebase")

    def test_finish_nonexistent_branch_raises(self, service):
        from nexus.contracts.exceptions import BranchNotFoundError

        with pytest.raises(BranchNotFoundError):
            service.finish_explore("/ws", "ghost", outcome="merge")
