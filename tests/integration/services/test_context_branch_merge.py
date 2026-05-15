"""Unit tests for ContextBranchService merge algorithm (Issue #1315, T2-B).

8 example-based scenarios covering all branches of the three-way merge:
1. Fast-forward merge
2. Clean merge (different files changed)
3. Conflict detected (fail strategy)
4. Conflict with source-wins strategy
5. Empty branch merge (no changes)
6. Branch with deletions
7. Both branches delete same file
8. One deletes, other modifies (conflict)
"""

import hashlib
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from nexus.contracts.exceptions import BranchConflictError, BranchStateError
from nexus.contracts.workspace_manifest import ManifestEntry, WorkspaceManifest
from nexus.services.workspace.context_branch import ContextBranchService
from nexus.storage.models._base import Base
from nexus.storage.models.context_branch import ContextBranchModel
from nexus.storage.models.filesystem import WorkspaceSnapshotModel


@pytest.fixture
def engine():
    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    return eng


@pytest.fixture
def session_factory(engine):
    return sessionmaker(bind=engine)


def _make_manifest(*files: tuple[str, str, int]) -> WorkspaceManifest:
    """Create a manifest from (path, hash, size) tuples."""
    entries = {path: ManifestEntry(content_id=h, size=s, mime_type=None) for path, h, s in files}
    return WorkspaceManifest(entries=entries)


def _store_snapshot(
    session_factory, workspace: str, snap_id: str, number: int, manifest: WorkspaceManifest
) -> str:
    """Store a snapshot with a real manifest hash."""
    manifest_bytes = manifest.to_json()
    manifest_hash = hashlib.sha256(manifest_bytes).hexdigest()
    with session_factory() as session:
        snap = WorkspaceSnapshotModel(
            snapshot_id=snap_id,
            workspace_path=workspace,
            snapshot_number=number,
            manifest_hash=manifest_hash,
            file_count=manifest.file_count,
            total_size_bytes=manifest.total_size,
        )
        session.add(snap)
        session.commit()
    return manifest_hash


def _make_service(session_factory, manifest_store: dict[str, bytes]) -> ContextBranchService:
    """Create service with mocked backend that serves manifests from a dict."""
    wm = MagicMock()
    wm.metadata = MagicMock()

    def read_content(hash_val, context=None):
        return manifest_store[hash_val]

    def write_content(data, content_id="", *, offset: int = 0, context=None):
        h = hashlib.sha256(data).hexdigest()
        manifest_store[h] = data
        return SimpleNamespace(content_id=h)

    wm.backend.read_content = read_content
    wm.backend.write_content = write_content

    return ContextBranchService(
        workspace_manager=wm,
        record_store=SimpleNamespace(session_factory=session_factory),
        rebac_manager=None,
        default_zone_id="z1",
    )


def _setup_branches(
    session_factory,
    manifest_store: dict[str, bytes],
    workspace: str,
    fork_manifest: WorkspaceManifest,
    source_manifest: WorkspaceManifest,
    target_manifest: WorkspaceManifest,
):
    """Set up fork_point → source branch + target branch with specific manifests."""
    # Store manifests in CAS
    fork_hash = _store_snapshot(session_factory, workspace, "fork", 1, fork_manifest)
    source_hash = _store_snapshot(session_factory, workspace, "src-head", 2, source_manifest)
    target_hash = _store_snapshot(session_factory, workspace, "tgt-head", 3, target_manifest)

    manifest_store[fork_hash] = fork_manifest.to_json()
    manifest_store[source_hash] = source_manifest.to_json()
    manifest_store[target_hash] = target_manifest.to_json()

    # Create branches
    with session_factory() as session:
        main = ContextBranchModel(
            zone_id="z1",
            workspace_path=workspace,
            branch_name="main",
            head_snapshot_id="tgt-head",
            is_current=True,
            status="active",
        )
        feature = ContextBranchModel(
            zone_id="z1",
            workspace_path=workspace,
            branch_name="feature",
            head_snapshot_id="src-head",
            parent_branch="main",
            fork_point_id="fork",
            is_current=False,
            status="active",
        )
        session.add_all([main, feature])
        session.commit()


# ==================================================================
# Scenario 1: Fast-forward merge
# ==================================================================


class TestFastForwardMerge:
    """Target hasn't moved since fork → just advance pointer."""

    def test_fast_forward(self, session_factory):
        workspace = "/ws/ff"
        manifest_store: dict[str, bytes] = {}
        service = _make_service(session_factory, manifest_store)

        # Fork manifest = target HEAD (target hasn't moved)
        base = _make_manifest(("a.txt", "hash-a", 100))
        source = _make_manifest(("a.txt", "hash-a", 100), ("b.txt", "hash-b", 200))

        fork_hash = _store_snapshot(session_factory, workspace, "fork", 1, base)
        source_hash = _store_snapshot(session_factory, workspace, "src", 2, source)
        manifest_store[fork_hash] = base.to_json()
        manifest_store[source_hash] = source.to_json()

        with session_factory() as session:
            main = ContextBranchModel(
                zone_id="z1",
                workspace_path=workspace,
                branch_name="main",
                head_snapshot_id="fork",  # Target HEAD == fork point
                is_current=True,
                status="active",
            )
            feature = ContextBranchModel(
                zone_id="z1",
                workspace_path=workspace,
                branch_name="feature",
                head_snapshot_id="src",
                parent_branch="main",
                fork_point_id="fork",
                is_current=False,
                status="active",
            )
            session.add_all([main, feature])
            session.commit()

        result = service.merge(workspace, "feature", "main")
        assert result.fast_forward is True
        assert result.merged is True
        assert result.strategy == "fast-forward"


# ==================================================================
# Scenario 2: Clean merge (different files changed)
# ==================================================================


class TestCleanMerge:
    """Both branches changed different files → auto-merge."""

    def test_clean_merge(self, session_factory):
        workspace = "/ws/clean"
        manifest_store: dict[str, bytes] = {}
        service = _make_service(session_factory, manifest_store)

        base = _make_manifest(("a.txt", "hash-a", 100))
        source = _make_manifest(("a.txt", "hash-a", 100), ("b.txt", "hash-b", 200))
        target = _make_manifest(("a.txt", "hash-a", 100), ("c.txt", "hash-c", 300))

        _setup_branches(session_factory, manifest_store, workspace, base, source, target)
        result = service.merge(workspace, "feature", "main")

        assert result.merged is True
        assert result.fast_forward is False
        assert result.files_added == 1  # b.txt added by source
        assert result.files_removed == 0


# ==================================================================
# Scenario 3: Conflict detected (fail strategy)
# ==================================================================


class TestConflictFail:
    """Same file changed in both branches → fail with conflict error."""

    def test_conflict_raises(self, session_factory):
        workspace = "/ws/conflict"
        manifest_store: dict[str, bytes] = {}
        service = _make_service(session_factory, manifest_store)

        base = _make_manifest(("shared.txt", "hash-v1", 100))
        source = _make_manifest(("shared.txt", "hash-v2-src", 100))
        target = _make_manifest(("shared.txt", "hash-v2-tgt", 100))

        _setup_branches(session_factory, manifest_store, workspace, base, source, target)

        with pytest.raises(BranchConflictError) as exc_info:
            service.merge(workspace, "feature", "main", strategy="fail")

        assert "shared.txt" in exc_info.value.conflicting_paths
        assert exc_info.value.source_branch == "feature"
        assert exc_info.value.target_branch == "main"


# ==================================================================
# Scenario 4: Conflict with source-wins strategy
# ==================================================================


class TestConflictSourceWins:
    """Same file changed in both branches → source version wins."""

    def test_source_wins(self, session_factory):
        workspace = "/ws/srcwins"
        manifest_store: dict[str, bytes] = {}
        service = _make_service(session_factory, manifest_store)

        base = _make_manifest(("shared.txt", "hash-v1", 100))
        source = _make_manifest(("shared.txt", "hash-v2-src", 150))
        target = _make_manifest(("shared.txt", "hash-v2-tgt", 120))

        _setup_branches(session_factory, manifest_store, workspace, base, source, target)
        result = service.merge(workspace, "feature", "main", strategy="source-wins")

        assert result.merged is True
        assert result.strategy == "source-wins"


# ==================================================================
# Scenario 5: Empty branch merge (no changes)
# ==================================================================


class TestEmptyBranchMerge:
    """Branch was created but no changes were made → fast-forward (same HEAD)."""

    def test_empty_branch_fast_forward(self, session_factory):
        workspace = "/ws/empty"
        manifest_store: dict[str, bytes] = {}
        service = _make_service(session_factory, manifest_store)

        base = _make_manifest(("a.txt", "hash-a", 100))
        fork_hash = _store_snapshot(session_factory, workspace, "fork", 1, base)
        manifest_store[fork_hash] = base.to_json()

        with session_factory() as session:
            main = ContextBranchModel(
                zone_id="z1",
                workspace_path=workspace,
                branch_name="main",
                head_snapshot_id="fork",  # Same as fork point
                is_current=True,
                status="active",
            )
            # Feature has same HEAD as fork point (no commits)
            feature = ContextBranchModel(
                zone_id="z1",
                workspace_path=workspace,
                branch_name="feature",
                head_snapshot_id="fork",
                parent_branch="main",
                fork_point_id="fork",
                is_current=False,
                status="active",
            )
            session.add_all([main, feature])
            session.commit()

        result = service.merge(workspace, "feature", "main")
        assert result.fast_forward is True


# ==================================================================
# Scenario 6: Branch with deletions
# ==================================================================


class TestBranchWithDeletions:
    """Source branch deleted files → deletions applied to target."""

    def test_deletions_applied(self, session_factory):
        workspace = "/ws/delete"
        manifest_store: dict[str, bytes] = {}
        service = _make_service(session_factory, manifest_store)

        base = _make_manifest(
            ("a.txt", "hash-a", 100),
            ("b.txt", "hash-b", 200),
            ("c.txt", "hash-c", 300),
        )
        source = _make_manifest(("a.txt", "hash-a", 100))  # Deleted b.txt and c.txt
        target = _make_manifest(
            ("a.txt", "hash-a", 100),
            ("b.txt", "hash-b", 200),
            ("c.txt", "hash-c", 300),
            ("d.txt", "hash-d", 400),  # Target added d.txt
        )

        _setup_branches(session_factory, manifest_store, workspace, base, source, target)
        result = service.merge(workspace, "feature", "main")

        assert result.merged is True
        assert result.files_removed == 2  # b.txt and c.txt


# ==================================================================
# Scenario 7: Both branches delete same file
# ==================================================================


class TestBothDeleteSameFile:
    """Both branches deleted the same file → no conflict (same outcome)."""

    def test_both_delete_no_conflict(self, session_factory):
        workspace = "/ws/both-del"
        manifest_store: dict[str, bytes] = {}
        service = _make_service(session_factory, manifest_store)

        base = _make_manifest(
            ("a.txt", "hash-a", 100),
            ("shared.txt", "hash-shared", 200),
        )
        source = _make_manifest(("a.txt", "hash-a", 100))  # Deleted shared.txt
        target = _make_manifest(("a.txt", "hash-a", 100))  # Also deleted shared.txt

        _setup_branches(session_factory, manifest_store, workspace, base, source, target)
        result = service.merge(workspace, "feature", "main", strategy="fail")

        # Both made same change → no conflict
        assert result.merged is True


# ==================================================================
# Scenario 8: One deletes, other modifies (conflict)
# ==================================================================


class TestDeleteVsModifyConflict:
    """One branch deleted a file, the other modified it → conflict."""

    def test_delete_vs_modify_conflicts(self, session_factory):
        workspace = "/ws/del-mod"
        manifest_store: dict[str, bytes] = {}
        service = _make_service(session_factory, manifest_store)

        base = _make_manifest(("shared.txt", "hash-v1", 100))
        source = _make_manifest()  # Source deleted shared.txt
        target = _make_manifest(("shared.txt", "hash-v2", 150))  # Target modified it

        _setup_branches(session_factory, manifest_store, workspace, base, source, target)

        with pytest.raises(BranchConflictError) as exc_info:
            service.merge(workspace, "feature", "main", strategy="fail")

        assert "shared.txt" in exc_info.value.conflicting_paths


# ==================================================================
# Merge validation edge cases
# ==================================================================


class TestMergeValidation:
    def test_self_merge_raises(self, session_factory):
        workspace = "/ws/self"
        manifest_store: dict[str, bytes] = {}
        service = _make_service(session_factory, manifest_store)

        with session_factory() as session:
            main = ContextBranchModel(
                zone_id="z1",
                workspace_path=workspace,
                branch_name="main",
                head_snapshot_id=None,
                is_current=True,
                status="active",
            )
            session.add(main)
            session.commit()

        with pytest.raises(BranchStateError, match="Cannot merge a branch into itself"):
            service.merge(workspace, "main", "main")

    def test_merge_already_merged_raises(self, session_factory):
        workspace = "/ws/merged"
        manifest_store: dict[str, bytes] = {}
        service = _make_service(session_factory, manifest_store)

        with session_factory() as session:
            main = ContextBranchModel(
                zone_id="z1",
                workspace_path=workspace,
                branch_name="main",
                is_current=True,
                status="active",
            )
            feature = ContextBranchModel(
                zone_id="z1",
                workspace_path=workspace,
                branch_name="feature",
                parent_branch="main",
                status="merged",
            )
            session.add_all([main, feature])
            session.commit()

        with pytest.raises(BranchStateError, match="already merged"):
            service.merge(workspace, "feature", "main")
