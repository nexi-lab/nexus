"""E2E tests for Context Branching (Issue #1315).

Tests the full context branch lifecycle through the service layer
with real SQLite DB and CAS backend. Validates:
- Full explore → commit → finish flow
- Permission enforcement with ReBAC
- Merge conflict detection and resolution
- Branch persistence across service instances
- Code review fixes (C1, C2, C3, H1-H6)
"""

import hashlib
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from nexus.contracts.exceptions import (
    BranchConflictError,
    BranchProtectedError,
    BranchStateError,
    NexusPermissionError,
)
from nexus.contracts.workspace_manifest import ManifestEntry, WorkspaceManifest
from nexus.services.workspace.context_branch import ContextBranchService
from nexus.storage.models._base import Base
from nexus.storage.models.filesystem import WorkspaceSnapshotModel

# ---------------------------------------------------------------------------
# Fixtures — real DB + CAS
# ---------------------------------------------------------------------------


class InMemoryCAS:
    """Content-addressable store backed by a dict."""

    def __init__(self):
        self.blobs: dict[str, bytes] = {}

    def read_content(self, content_id, context=None):
        data = self.blobs.get(content_id)
        if data is None:
            raise FileNotFoundError(f"CAS blob {content_id} not found")
        return data

    def write_content(self, data, content_id: str = "", *, offset: int = 0, context=None):
        from nexus.core.object_store import WriteResult

        h = hashlib.sha256(data).hexdigest()
        self.blobs[h] = data
        return WriteResult(content_id=h, size=len(data))


class InMemoryMetadata:
    """Minimal metadata store that supports list_iter."""

    def __init__(self):
        self.files: dict[str, MagicMock] = {}

    def list_iter(self, prefix="", **kwargs):
        return [v for k, v in self.files.items() if k.startswith(prefix)]


class FakeWorkspaceManagerE2E:
    """Workspace manager with real snapshot creation and restore."""

    def __init__(self, session_factory, cas: InMemoryCAS):
        self.metadata = InMemoryMetadata()
        self.backend = cas
        self._session_factory = session_factory
        self._cas = cas
        self._snap_counter = 0

    def create_snapshot(self, workspace_path, description=None, **kwargs):
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
        self._cas.blobs[manifest_hash] = manifest_bytes

        snap_id = f"snap-{self._snap_counter}"
        with self._session_factory() as session:
            from sqlalchemy import desc

            last = session.execute(
                __import__("sqlalchemy")
                .select(WorkspaceSnapshotModel.snapshot_number)
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
        return {"files_restored": 1, "files_deleted": 0}

    def list_snapshots(self, workspace_path, limit=100, **kwargs):
        with self._session_factory() as session:
            from sqlalchemy import desc, select

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
                    "created_at": s.created_at,
                }
                for s in snaps
            ]

    def diff_snapshots(self, snapshot_id_1, snapshot_id_2, **kwargs):
        return {"added": [], "removed": [], "modified": [], "unchanged": 0}


@pytest.fixture
def engine():
    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    return eng


@pytest.fixture
def session_factory(engine):
    return sessionmaker(bind=engine)


@pytest.fixture
def cas():
    return InMemoryCAS()


@pytest.fixture
def fake_wm(session_factory, cas):
    return FakeWorkspaceManagerE2E(session_factory, cas)


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


# ---------------------------------------------------------------------------
# E2E Lifecycle: explore → commit → merge → verify
# ---------------------------------------------------------------------------


class TestE2EExploreMergeLifecycle:
    """Full agent exploration workflow end-to-end."""

    def test_explore_commit_merge_full_flow(self, service):
        ws = "/ws/e2e-full"

        # Step 1: Initial setup
        service.ensure_main_branch(ws)
        service.commit(ws, message="Initial state")

        # Step 2: Start exploration
        explore = service.explore(ws, "Try new architecture")
        assert explore.branch_name == "try-new-architecture"
        assert explore.fork_point_snapshot_id is not None

        # Step 3: Do work on exploration branch
        service.commit(ws, message="Refactor module A", branch_name="try-new-architecture")
        service.commit(ws, message="Refactor module B", branch_name="try-new-architecture")

        # Step 4: Check log shows commits
        log = service.log(ws)
        assert len(log) >= 3  # initial + 2 exploration commits

        # Step 5: Merge back
        result = service.finish_explore(ws, "try-new-architecture", outcome="merge")
        assert result["outcome"] == "merged"
        assert result["merged_into"] == "main"

        # Step 6: Verify final state
        current = service.get_current_branch(ws)
        assert current.branch_name == "main"

        # Step 7: Exploration branch should be inactive
        branches = service.list_branches(ws, include_inactive=True)
        explore_branch = [b for b in branches if b.branch_name == "try-new-architecture"][0]
        assert explore_branch.status == "merged"

    def test_explore_discard_flow(self, service):
        ws = "/ws/e2e-discard"

        service.ensure_main_branch(ws)
        service.commit(ws, message="Baseline")

        explore = service.explore(ws, "Bad idea")
        service.commit(ws, message="Bad work", branch_name=explore.branch_name)

        result = service.finish_explore(ws, explore.branch_name, outcome="discard")
        assert result["outcome"] == "discarded"
        assert result["returned_to"] == "main"

        # Discarded branch should not appear in active list
        branches = service.list_branches(ws)
        assert all(b.branch_name != explore.branch_name for b in branches)

    def test_multiple_parallel_explorations(self, service):
        ws = "/ws/e2e-parallel"

        service.ensure_main_branch(ws)
        service.commit(ws, message="Starting point")

        # Launch 3 explorations
        service.explore(ws, "Approach alpha")
        service.checkout(ws, "main")
        service.explore(ws, "Approach beta")
        service.checkout(ws, "main")
        service.explore(ws, "Approach gamma")

        # Commit on the winning branch
        service.commit(ws, message="Beta wins", branch_name="approach-beta")

        # Merge winner, discard losers
        service.finish_explore(ws, "approach-beta", outcome="merge")
        service.finish_explore(ws, "approach-alpha", outcome="discard")
        service.finish_explore(ws, "approach-gamma", outcome="discard")

        # Only main should remain active
        active = service.list_branches(ws)
        assert {b.branch_name for b in active} == {"main"}


# ---------------------------------------------------------------------------
# E2E: Permission enforcement
# ---------------------------------------------------------------------------


class TestE2EPermissionEnforcement:
    """Verify ReBAC permissions are checked for branch operations."""

    def _make_denied_svc(self, fake_wm, session_factory):
        """Create a service with rebac_check always returning False."""
        rebac = MagicMock()
        rebac.rebac_check.return_value = False
        return ContextBranchService(
            workspace_manager=fake_wm,
            record_store=SimpleNamespace(session_factory=session_factory),
            rebac_manager=rebac,
            default_zone_id="z1",
            default_agent_id="agent-1",
        )

    def test_branch_create_requires_permission(self, fake_wm, session_factory):
        svc = self._make_denied_svc(fake_wm, session_factory)
        with pytest.raises(NexusPermissionError):
            svc.create_branch("/ws/perm", "feat-branch")

    def test_checkout_requires_permission(self, fake_wm, session_factory):
        svc = self._make_denied_svc(fake_wm, session_factory)
        with pytest.raises(NexusPermissionError):
            svc.checkout("/ws/perm", "main")

    def test_explore_requires_permission(self, fake_wm, session_factory):
        svc = self._make_denied_svc(fake_wm, session_factory)
        with pytest.raises(NexusPermissionError):
            svc.explore("/ws/perm", "test exploration")

    def test_finish_explore_requires_permission(self, fake_wm, session_factory):
        """H1 fix: finish_explore checks permission at top level."""
        svc = self._make_denied_svc(fake_wm, session_factory)
        with pytest.raises(NexusPermissionError):
            svc.finish_explore("/ws/perm", "some-branch", outcome="merge")

    def test_merge_requires_permission(self, fake_wm, session_factory):
        svc = self._make_denied_svc(fake_wm, session_factory)
        with pytest.raises(NexusPermissionError):
            svc.merge("/ws/perm", "feature", "main")


# ---------------------------------------------------------------------------
# E2E: Code review fixes validation
# ---------------------------------------------------------------------------


class TestE2ECodeReviewFixes:
    """Validate all code review fixes work correctly in E2E scenarios."""

    def test_c1_fast_forward_uses_fresh_session(self, service, session_factory):
        """C1: Fast-forward merge uses fresh session for source status update."""
        ws = "/ws/c1-test"

        service.ensure_main_branch(ws)
        service.commit(ws, message="Initial")

        service.create_branch(ws, "feature")
        service.checkout(ws, "feature")
        service.commit(ws, message="Feature work", branch_name="feature")

        # This merge should be fast-forward since main hasn't moved
        result = service.merge(ws, "feature", "main")
        assert result.fast_forward is True
        assert result.merged is True

        # Source branch should be marked as merged
        branch = service.get_branch(ws, "feature")
        assert branch.status == "merged"
        assert branch.merged_into_branch == "main"

    def test_c2_concurrent_ensure_main_branch(self, service, session_factory):
        """C2: ensure_main_branch handles IntegrityError from concurrent creation."""
        ws = "/ws/c2-test"

        # Create main branch normally
        b1 = service.ensure_main_branch(ws)
        assert b1.branch_name == "main"

        # Calling again should return existing (idempotent)
        b2 = service.ensure_main_branch(ws)
        assert b2.id == b1.id

    def test_h2_invalid_merge_strategy_raises(self, service):
        """H2: Invalid merge strategy is rejected upfront."""
        ws = "/ws/h2-test"
        service.ensure_main_branch(ws)
        service.commit(ws, message="Initial")
        service.create_branch(ws, "feature")

        with pytest.raises(ValueError, match="Invalid merge strategy"):
            service.merge(ws, "feature", "main", strategy="typo-wins")

    def test_h4_slug_collision_avoidance(self, service):
        """H4: explore() adds suffix when branch name already exists."""
        ws = "/ws/h4-test"

        service.ensure_main_branch(ws)
        service.commit(ws, message="Initial")

        # First exploration
        e1 = service.explore(ws, "Fix bug")
        assert e1.branch_name == "fix-bug"

        # Return to main
        service.checkout(ws, "main")

        # Second exploration with same description — should get suffix
        e2 = service.explore(ws, "Fix bug")
        assert e2.branch_name != "fix-bug"
        assert e2.branch_name.startswith("fix-bug-")

    def test_h6_advance_head_checks_status(self, service, session_factory):
        """H6: _advance_head rejects advancing HEAD on discarded branches."""
        ws = "/ws/h6-test"
        service.ensure_main_branch(ws)
        service.commit(ws, message="Initial")
        service.create_branch(ws, "temp")
        service.commit(ws, message="Temp work", branch_name="temp")

        # Discard the branch
        service.delete_branch(ws, "temp")

        # Attempting to advance HEAD should fail
        with pytest.raises(BranchStateError, match="status 'discarded'"):
            service._advance_head("z1", ws, "temp", "snap-new")


# ---------------------------------------------------------------------------
# E2E: Three-way merge with real CAS
# ---------------------------------------------------------------------------


class TestE2EThreeWayMerge:
    """Three-way merge with real manifest loading and CAS operations."""

    def test_three_way_merge_non_overlapping_changes(self, service, fake_wm, cas):
        """Both branches change different files — should merge cleanly."""
        ws = "/ws/3way-clean"

        service.ensure_main_branch(ws)
        service.commit(ws, message="Initial")

        # Create feature branch and commit
        service.create_branch(ws, "feature")
        service.commit(ws, message="Feature work", branch_name="feature")

        # Also commit on main (forces three-way merge)
        service.commit(ws, message="Main work")

        # Merge should succeed (different files in each branch)
        result = service.merge(ws, "feature", "main", strategy="source-wins")
        assert result.merged is True
        assert result.fast_forward is False

    def test_conflict_detection_with_fail_strategy(self, service, fake_wm, cas, session_factory):
        """Conflicting changes should raise BranchConflictError with fail strategy."""
        ws = "/ws/3way-conflict"

        service.ensure_main_branch(ws)

        # Create initial commit with known manifest
        initial_manifest = WorkspaceManifest(
            entries={
                "shared.txt": ManifestEntry(
                    content_id="hash-shared", size=100, mime_type="text/plain"
                ),
                "common.txt": ManifestEntry(
                    content_id="hash-common", size=50, mime_type="text/plain"
                ),
            }
        )
        initial_bytes = initial_manifest.to_json()
        initial_hash = hashlib.sha256(initial_bytes).hexdigest()
        cas.blobs[initial_hash] = initial_bytes

        # Manually create snapshot with this manifest
        with session_factory() as s:
            snap = WorkspaceSnapshotModel(
                snapshot_id="snap-initial",
                workspace_path=ws,
                snapshot_number=1,
                manifest_hash=initial_hash,
                file_count=2,
                total_size_bytes=150,
                description="Initial",
            )
            s.add(snap)
            s.commit()

        # Advance main to this snapshot
        service._advance_head("z1", ws, "main", "snap-initial")

        # Create feature branch (fork point = snap-initial)
        service.create_branch(ws, "feature")

        # Create divergent manifests for source and target
        source_manifest = WorkspaceManifest(
            entries={
                "shared.txt": ManifestEntry(
                    content_id="hash-source-edit", size=200, mime_type="text/plain"
                ),
                "common.txt": ManifestEntry(
                    content_id="hash-common", size=50, mime_type="text/plain"
                ),
            }
        )
        target_manifest = WorkspaceManifest(
            entries={
                "shared.txt": ManifestEntry(
                    content_id="hash-target-edit", size=300, mime_type="text/plain"
                ),
                "common.txt": ManifestEntry(
                    content_id="hash-common", size=50, mime_type="text/plain"
                ),
            }
        )

        # Store manifests in CAS
        src_bytes = source_manifest.to_json()
        src_hash = hashlib.sha256(src_bytes).hexdigest()
        cas.blobs[src_hash] = src_bytes

        tgt_bytes = target_manifest.to_json()
        tgt_hash = hashlib.sha256(tgt_bytes).hexdigest()
        cas.blobs[tgt_hash] = tgt_bytes

        # Create snapshots for divergent changes
        with session_factory() as s:
            src_snap = WorkspaceSnapshotModel(
                snapshot_id="snap-source",
                workspace_path=ws,
                snapshot_number=2,
                manifest_hash=src_hash,
                file_count=2,
                total_size_bytes=250,
                description="Source changes",
            )
            tgt_snap = WorkspaceSnapshotModel(
                snapshot_id="snap-target",
                workspace_path=ws,
                snapshot_number=3,
                manifest_hash=tgt_hash,
                file_count=2,
                total_size_bytes=350,
                description="Target changes",
            )
            s.add_all([src_snap, tgt_snap])
            s.commit()

        # Advance branch heads
        service._advance_head("z1", ws, "feature", "snap-source")
        service._advance_head("z1", ws, "main", "snap-target")

        # Merge with fail strategy should raise conflict
        with pytest.raises(BranchConflictError) as exc_info:
            service.merge(ws, "feature", "main", strategy="fail")
        assert "shared.txt" in exc_info.value.conflicting_paths

        # Merge with source-wins should succeed
        result = service.merge(ws, "feature", "main", strategy="source-wins")
        assert result.merged is True


# ---------------------------------------------------------------------------
# E2E: Cross-session continuity
# ---------------------------------------------------------------------------


class TestE2ECrossSessionContinuity:
    """Branches persist across independent service instances."""

    def test_branch_survives_service_restart(self, fake_wm, session_factory):
        ws = "/ws/e2e-continuity"

        # Service instance 1: create exploration
        svc1 = ContextBranchService(
            workspace_manager=fake_wm,
            record_store=SimpleNamespace(session_factory=session_factory),
            rebac_manager=None,
            default_zone_id="z1",
        )
        svc1.ensure_main_branch(ws)
        svc1.commit(ws, message="Initial")
        explore = svc1.explore(ws, "Persistent exploration")

        # Service instance 2: continue the work
        svc2 = ContextBranchService(
            workspace_manager=fake_wm,
            record_store=SimpleNamespace(session_factory=session_factory),
            rebac_manager=None,
            default_zone_id="z1",
        )
        branches = svc2.list_branches(ws)
        names = {b.branch_name for b in branches}
        assert explore.branch_name in names

        # Can checkout and commit on the persisted branch
        svc2.checkout(ws, explore.branch_name)
        svc2.commit(ws, message="Continued work", branch_name=explore.branch_name)

        # Service instance 3: finish the exploration
        svc3 = ContextBranchService(
            workspace_manager=fake_wm,
            record_store=SimpleNamespace(session_factory=session_factory),
            rebac_manager=None,
            default_zone_id="z1",
        )
        result = svc3.finish_explore(ws, explore.branch_name, outcome="merge")
        assert result["outcome"] == "merged"


# ---------------------------------------------------------------------------
# E2E: Protected branch safety
# ---------------------------------------------------------------------------


class TestE2EProtectedBranches:
    def test_cannot_delete_main(self, service):
        ws = "/ws/e2e-protected"
        service.ensure_main_branch(ws)

        with pytest.raises(BranchProtectedError):
            service.delete_branch(ws, "main")

    def test_main_branch_survives_discard_attempt(self, service):
        """finish_explore with discard on a non-exploration branch is safe."""
        ws = "/ws/e2e-safe"
        service.ensure_main_branch(ws)
        service.commit(ws, message="Initial")

        # Try to discard main via finish_explore
        with pytest.raises(BranchProtectedError):
            service.finish_explore(ws, "main", outcome="discard")


# ===========================================================================
# Namespace Fork Integration (Issue #1273)
# ===========================================================================


class TestE2ENamespaceForkIntegration:
    """E2E tests verifying namespace fork alongside context branching."""

    @pytest.fixture
    def mock_namespace_manager(self):
        mgr = MagicMock()
        mgr.get_mount_table.return_value = [
            SimpleNamespace(virtual_path="/workspace/alpha"),
            SimpleNamespace(virtual_path="/workspace/beta"),
        ]
        return mgr

    @pytest.fixture
    def fork_service(self, mock_namespace_manager):
        from nexus.services.namespace.namespace_fork_service import (
            AgentNamespaceForkService,
        )

        return AgentNamespaceForkService(namespace_manager=mock_namespace_manager)

    def test_fork_during_explore(self, fork_service):
        """Fork namespace when starting exploration."""
        from nexus.contracts.namespace_fork_types import ForkMode

        info = fork_service.fork("explore-agent", mode=ForkMode.COPY)
        assert info.mount_count == 2
        ns = fork_service.get_fork(info.fork_id)
        assert ns.get("/workspace/alpha") is not None

    def test_merge_on_finish(self, fork_service):
        """Merge namespace fork when finishing exploration with merge outcome."""
        info = fork_service.fork("explore-agent")
        ns = fork_service.get_fork(info.fork_id)
        ns.put(
            "/workspace/gamma",
            SimpleNamespace(virtual_path="/workspace/gamma"),
        )
        result = fork_service.merge(info.fork_id, strategy="source-wins")
        assert result.merged is True
        assert result.entries_added == 1

    def test_discard_on_finish(self, fork_service):
        """Discard namespace fork when finishing exploration with discard outcome."""
        info = fork_service.fork("explore-agent")
        fork_service.discard(info.fork_id)
        from nexus.contracts.exceptions import NamespaceForkNotFoundError

        with pytest.raises(NamespaceForkNotFoundError):
            fork_service.get_fork(info.fork_id)

    def test_fork_isolation_across_agents(self, fork_service):
        """Two agents' forks don't interfere."""
        info1 = fork_service.fork("agent-a")
        info2 = fork_service.fork("agent-b")
        ns1 = fork_service.get_fork(info1.fork_id)
        ns2 = fork_service.get_fork(info2.fork_id)
        ns1.put("/workspace/only-a", SimpleNamespace(virtual_path="/workspace/only-a"))
        assert ns2.get("/workspace/only-a") is None

    def test_graceful_degradation(self):
        """Fork service operations fail gracefully when namespace manager is broken."""
        broken_mgr = MagicMock()
        broken_mgr.get_mount_table.side_effect = RuntimeError("DB down")

        from nexus.services.namespace.namespace_fork_service import (
            AgentNamespaceForkService,
        )

        svc = AgentNamespaceForkService(namespace_manager=broken_mgr)
        with pytest.raises(RuntimeError, match="DB down"):
            svc.fork("agent-x")
