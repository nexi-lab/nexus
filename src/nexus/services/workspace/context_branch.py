"""Context branch service for workspace branching (Issue #1315).

Provides git-like named branches on top of existing workspace snapshots.
Branches are metadata-only pointers — no data duplication (zero-copy branching).

Architecture decisions (from plan review):
    A1-A: Separate service composing WorkspaceManager (not extending it)
    A2-A: Three-way merge with fail-on-conflict + source-wins strategy
    A3-B: Optimistic concurrency via pointer_version counter
    A4-A: Explicit two-call pattern (explore + finish_explore)
    P2-A: Eager-load with SQL JOIN to avoid N+1
    P3-A: Retry 3x with exponential backoff on stale pointer
    P4-B: Skip auto-commit if workspace unchanged
"""

import logging
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.contracts.exceptions import (
    BranchConflictError,
    BranchExistsError,
    BranchNotFoundError,
    BranchProtectedError,
    BranchStateError,
    NexusFileNotFoundError,
    StalePointerError,
)
from nexus.contracts.workspace_manifest import WorkspaceManifest
from nexus.storage.models import ContextBranchModel, WorkspaceSnapshotModel

from .workspace_permissions import check_workspace_permission

# Valid merge strategies (H2: validate upfront)
_VALID_STRATEGIES = frozenset({"fail", "source-wins"})

if TYPE_CHECKING:
    from nexus.contracts.protocols.rebac import ReBACBrickProtocol
    from nexus.storage.record_store import RecordStoreABC

    from .workspace_manager import WorkspaceManager

logger = logging.getLogger(__name__)

# Protected branch names that cannot be deleted/discarded
PROTECTED_BRANCHES = frozenset({"main"})

# Default branch name
DEFAULT_BRANCH = "main"

# Optimistic concurrency retry config (P3-A)
_MAX_RETRIES = 3
_BASE_BACKOFF_MS = 10


@dataclass(frozen=True)
class BranchInfo:
    """Immutable branch metadata returned by service methods."""

    id: str
    branch_name: str
    workspace_path: str
    zone_id: str
    head_snapshot_id: str | None
    parent_branch: str | None
    fork_point_id: str | None
    status: str
    is_current: bool
    pointer_version: int
    merged_into_branch: str | None
    merge_snapshot_id: str | None
    created_by: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class MergeResult:
    """Immutable result of a merge operation."""

    merged: bool
    merge_snapshot_id: str | None
    files_added: int
    files_removed: int
    files_modified: int
    fast_forward: bool
    strategy: str


@dataclass(frozen=True)
class ExploreResult:
    """Immutable result of an explore() operation."""

    branch_name: str
    branch_id: str
    fork_point_snapshot_id: str | None
    skipped_commit: bool
    message: str


def _slugify(text: str) -> str:
    """Convert text to a valid branch name slug."""
    slug = text.lower().strip()
    slug = re.sub(r"[^a-z0-9\s-]", "", slug)
    slug = re.sub(r"[\s_]+", "-", slug)
    slug = re.sub(r"-+", "-", slug)
    slug = slug.strip("-")
    return slug[:64] or "unnamed-branch"


def _branch_from_model(model: ContextBranchModel) -> BranchInfo:
    """Convert SQLAlchemy model to immutable BranchInfo."""
    return BranchInfo(
        id=model.id,
        branch_name=model.branch_name,
        workspace_path=model.workspace_path,
        zone_id=model.zone_id,
        head_snapshot_id=model.head_snapshot_id,
        parent_branch=model.parent_branch,
        fork_point_id=model.fork_point_id,
        status=model.status,
        is_current=model.is_current,
        pointer_version=model.pointer_version,
        merged_into_branch=model.merged_into_branch,
        merge_snapshot_id=model.merge_snapshot_id,
        created_by=model.created_by,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


class ContextBranchService:
    """Manages git-like named branches for workspace context versioning.

    Composes WorkspaceManager for snapshot operations (create, restore, diff).
    All state is tracked in the context_branches SQL table.
    Branch pointers are the only mutable state; everything else is CAS-immutable.
    """

    def __init__(
        self,
        workspace_manager: "WorkspaceManager",
        record_store: "RecordStoreABC",
        rebac_manager: "ReBACBrickProtocol | None" = None,
        default_zone_id: str | None = None,
        default_agent_id: str | None = None,
    ):
        self._wm = workspace_manager
        self._session_factory = record_store.session_factory
        self._rebac_manager = rebac_manager
        self._default_zone_id = default_zone_id or ROOT_ZONE_ID
        self._default_agent_id = default_agent_id

    def _check_permission(
        self,
        workspace_path: str,
        permission: str,
        user_id: str | None = None,
        agent_id: str | None = None,
        zone_id: str | None = None,
    ) -> None:
        """Delegate permission checking to shared utility (C2-B)."""
        check_workspace_permission(
            rebac_manager=self._rebac_manager,
            workspace_path=workspace_path,
            permission=permission,
            user_id=user_id,
            agent_id=agent_id,
            zone_id=zone_id,
            default_agent_id=self._default_agent_id,
            default_zone_id=self._default_zone_id,
        )

    def _resolve_zone(self, zone_id: str | None) -> str:
        return zone_id or self._default_zone_id

    # ------------------------------------------------------------------
    # Branch CRUD
    # ------------------------------------------------------------------

    def ensure_main_branch(
        self,
        workspace_path: str,
        zone_id: str | None = None,
        created_by: str | None = None,
    ) -> BranchInfo:
        """Ensure a 'main' branch exists for the workspace. Creates it if missing.

        This is called lazily on first branch operation for a workspace.
        The main branch's head_snapshot_id points to the latest existing snapshot
        (or None if no snapshots exist yet).
        """
        z = self._resolve_zone(zone_id)
        with self._session_factory() as session:
            existing = session.execute(
                select(ContextBranchModel).where(
                    ContextBranchModel.zone_id == z,
                    ContextBranchModel.workspace_path == workspace_path,
                    ContextBranchModel.branch_name == DEFAULT_BRANCH,
                )
            ).scalar_one_or_none()

            if existing:
                return _branch_from_model(existing)

            # Find latest snapshot for initial head pointer
            latest_snap = session.execute(
                select(WorkspaceSnapshotModel.snapshot_id)
                .where(WorkspaceSnapshotModel.workspace_path == workspace_path)
                .order_by(WorkspaceSnapshotModel.created_at.desc())
                .limit(1)
            ).scalar_one_or_none()

            branch = ContextBranchModel(
                zone_id=z,
                workspace_path=workspace_path,
                branch_name=DEFAULT_BRANCH,
                head_snapshot_id=latest_snap,
                parent_branch=None,
                fork_point_id=None,
                status="active",
                is_current=True,
                pointer_version=0,
                created_by=created_by,
            )
            session.add(branch)
            try:
                session.commit()
            except IntegrityError:
                # C2: Another concurrent call created it first — re-read
                session.rollback()
                existing = session.execute(
                    select(ContextBranchModel).where(
                        ContextBranchModel.zone_id == z,
                        ContextBranchModel.workspace_path == workspace_path,
                        ContextBranchModel.branch_name == DEFAULT_BRANCH,
                    )
                ).scalar_one_or_none()
                if existing:
                    return _branch_from_model(existing)
                raise  # Genuinely unexpected
            session.refresh(branch)
            logger.info(
                "Created main branch for workspace %s (zone=%s, head=%s)",
                workspace_path,
                z,
                latest_snap,
            )
            return _branch_from_model(branch)

    def create_branch(
        self,
        workspace_path: str,
        branch_name: str,
        from_branch: str | None = None,
        from_snapshot_id: str | None = None,
        zone_id: str | None = None,
        user_id: str | None = None,
        agent_id: str | None = None,
        created_by: str | None = None,
    ) -> BranchInfo:
        """Create a new named branch.

        Args:
            workspace_path: Workspace to branch
            branch_name: Name for the new branch
            from_branch: Branch to fork from (default: current branch)
            from_snapshot_id: Specific snapshot to fork from (overrides from_branch's HEAD)
            zone_id: Zone for isolation
            user_id: User ID for permission check
            agent_id: Agent ID for permission check
            created_by: Who is creating the branch

        Returns:
            BranchInfo for the newly created branch

        Raises:
            BranchExistsError: If branch_name already exists
            BranchNotFoundError: If from_branch doesn't exist
            NexusPermissionError: If permission denied
        """
        self._check_permission(workspace_path, "branch:create", user_id, agent_id, zone_id)
        z = self._resolve_zone(zone_id)

        # Ensure main branch exists
        self.ensure_main_branch(workspace_path, zone_id=z, created_by=created_by)

        with self._session_factory() as session:
            # Check for name collision
            existing = session.execute(
                select(ContextBranchModel).where(
                    ContextBranchModel.zone_id == z,
                    ContextBranchModel.workspace_path == workspace_path,
                    ContextBranchModel.branch_name == branch_name,
                )
            ).scalar_one_or_none()

            if existing:
                raise BranchExistsError(branch_name, workspace_path)

            # Resolve fork point
            fork_snapshot_id: str | None = None
            if from_snapshot_id:
                # Explicit snapshot — verify it exists
                snap = session.get(WorkspaceSnapshotModel, from_snapshot_id)
                if not snap:
                    raise NexusFileNotFoundError(
                        path=f"snapshot:{from_snapshot_id}",
                        message="Fork point snapshot not found",
                    )
                fork_snapshot_id = from_snapshot_id
                parent = from_branch or DEFAULT_BRANCH
            else:
                # Fork from branch HEAD
                source_name = from_branch or self._get_current_branch_name(
                    session, z, workspace_path
                )
                source = self._get_branch_model(session, z, workspace_path, source_name)
                if not source:
                    raise BranchNotFoundError(source_name, workspace_path)
                fork_snapshot_id = source.head_snapshot_id
                parent = source.branch_name

            branch = ContextBranchModel(
                zone_id=z,
                workspace_path=workspace_path,
                branch_name=branch_name,
                head_snapshot_id=fork_snapshot_id,
                parent_branch=parent,
                fork_point_id=fork_snapshot_id,
                status="active",
                is_current=False,
                pointer_version=0,
                created_by=created_by,
            )
            session.add(branch)
            session.commit()
            session.refresh(branch)

            logger.info(
                "Created branch '%s' from %s (snapshot=%s) for workspace %s",
                branch_name,
                parent,
                fork_snapshot_id,
                workspace_path,
            )
            return _branch_from_model(branch)

    def list_branches(
        self,
        workspace_path: str,
        zone_id: str | None = None,
        user_id: str | None = None,
        agent_id: str | None = None,
        include_inactive: bool = False,
    ) -> list[BranchInfo]:
        """List all branches for a workspace (P2-A: eager-loaded, no N+1)."""
        self._check_permission(workspace_path, "branch:list", user_id, agent_id, zone_id)
        z = self._resolve_zone(zone_id)

        with self._session_factory() as session:
            stmt = select(ContextBranchModel).where(
                ContextBranchModel.zone_id == z,
                ContextBranchModel.workspace_path == workspace_path,
            )
            if not include_inactive:
                stmt = stmt.where(ContextBranchModel.status == "active")
            stmt = stmt.order_by(ContextBranchModel.created_at)

            branches = session.execute(stmt).scalars().all()
            return [_branch_from_model(b) for b in branches]

    def get_branch(
        self,
        workspace_path: str,
        branch_name: str,
        zone_id: str | None = None,
        user_id: str | None = None,
        agent_id: str | None = None,
    ) -> BranchInfo:
        """Get a single branch by name."""
        self._check_permission(workspace_path, "branch:read", user_id, agent_id, zone_id)
        z = self._resolve_zone(zone_id)

        with self._session_factory() as session:
            model = self._get_branch_model(session, z, workspace_path, branch_name)
            if not model:
                raise BranchNotFoundError(branch_name, workspace_path)
            return _branch_from_model(model)

    def get_current_branch(
        self,
        workspace_path: str,
        zone_id: str | None = None,
        user_id: str | None = None,
        agent_id: str | None = None,
    ) -> BranchInfo:
        """Get the currently checked-out branch for a workspace."""
        self._check_permission(workspace_path, "branch:read", user_id, agent_id, zone_id)
        z = self._resolve_zone(zone_id)

        self.ensure_main_branch(workspace_path, zone_id=z)

        with self._session_factory() as session:
            name = self._get_current_branch_name(session, z, workspace_path)
            model = self._get_branch_model(session, z, workspace_path, name)
            if not model:
                raise BranchNotFoundError(name, workspace_path)
            return _branch_from_model(model)

    def delete_branch(
        self,
        workspace_path: str,
        branch_name: str,
        zone_id: str | None = None,
        user_id: str | None = None,
        agent_id: str | None = None,
    ) -> BranchInfo:
        """Discard a branch (mark as discarded). Protected branches cannot be deleted.

        Raises:
            BranchProtectedError: If branch is in PROTECTED_BRANCHES
            BranchNotFoundError: If branch doesn't exist
            BranchStateError: If branch is not active
        """
        if branch_name in PROTECTED_BRANCHES:
            raise BranchProtectedError(branch_name)

        self._check_permission(workspace_path, "branch:delete", user_id, agent_id, zone_id)
        z = self._resolve_zone(zone_id)

        with self._session_factory() as session:
            model = self._get_branch_model(session, z, workspace_path, branch_name)
            if not model:
                raise BranchNotFoundError(branch_name, workspace_path)
            if model.status != "active":
                raise BranchStateError(
                    branch_name,
                    f"Cannot delete branch '{branch_name}' with status '{model.status}'",
                )

            model.status = "discarded"
            model.is_current = False
            model.updated_at = datetime.now(UTC)
            session.commit()
            session.refresh(model)

            logger.info("Discarded branch '%s' for workspace %s", branch_name, workspace_path)
            return _branch_from_model(model)

    # ------------------------------------------------------------------
    # Commit / Checkout / Log / Diff
    # ------------------------------------------------------------------

    def commit(
        self,
        workspace_path: str,
        message: str | None = None,
        branch_name: str | None = None,
        zone_id: str | None = None,
        user_id: str | None = None,
        agent_id: str | None = None,
        created_by: str | None = None,
    ) -> dict[str, Any]:
        """Create a snapshot and advance the branch HEAD.

        Args:
            workspace_path: Workspace to snapshot
            message: Commit message (used as snapshot description)
            branch_name: Branch to commit to (default: current branch)
            zone_id: Zone for isolation
            user_id: User ID for permission check
            agent_id: Agent ID for permission check
            created_by: Who is committing

        Returns:
            Dict with snapshot info and branch info
        """
        z = self._resolve_zone(zone_id)
        self.ensure_main_branch(workspace_path, zone_id=z, created_by=created_by)

        with self._session_factory() as session:
            target_name = branch_name or self._get_current_branch_name(session, z, workspace_path)
            target = self._get_branch_model(session, z, workspace_path, target_name)
            if not target:
                raise BranchNotFoundError(target_name, workspace_path)
            if target.status != "active":
                raise BranchStateError(
                    target_name,
                    f"Cannot commit to branch '{target_name}' with status '{target.status}'",
                )

        # Create snapshot via WorkspaceManager (handles permission check)
        snapshot_info = self._wm.create_snapshot(
            workspace_path=workspace_path,
            description=message,
            created_by=created_by,
            user_id=user_id,
            agent_id=agent_id,
            zone_id=zone_id,
        )

        # Advance branch HEAD with optimistic concurrency
        new_snap_id = snapshot_info["snapshot_id"]
        self._advance_head_with_retry(z, workspace_path, target_name, new_snap_id)

        return {
            "snapshot": snapshot_info,
            "branch": target_name,
            "pointer_advanced": True,
        }

    def checkout(
        self,
        workspace_path: str,
        target: str,
        zone_id: str | None = None,
        user_id: str | None = None,
        agent_id: str | None = None,
    ) -> dict[str, Any]:
        """Switch to a different branch and restore its HEAD snapshot.

        Args:
            target: Branch name to check out

        Returns:
            Dict with restore info and branch info
        """
        self._check_permission(workspace_path, "branch:checkout", user_id, agent_id, zone_id)
        z = self._resolve_zone(zone_id)
        self.ensure_main_branch(workspace_path, zone_id=z)

        with self._session_factory() as session:
            branch = self._get_branch_model(session, z, workspace_path, target)
            if not branch:
                raise BranchNotFoundError(target, workspace_path)
            if branch.status != "active":
                raise BranchStateError(
                    target, f"Cannot checkout branch '{target}' with status '{branch.status}'"
                )

            head_snap_id = branch.head_snapshot_id

            # Update is_current flags: unset old, set new
            session.execute(
                update(ContextBranchModel)
                .where(
                    ContextBranchModel.zone_id == z,
                    ContextBranchModel.workspace_path == workspace_path,
                    ContextBranchModel.is_current == True,  # noqa: E712
                )
                .values(is_current=False, updated_at=datetime.now(UTC))
            )
            branch.is_current = True
            branch.updated_at = datetime.now(UTC)
            session.commit()

        # Restore workspace to branch HEAD (if it has a snapshot)
        restore_info = None
        if head_snap_id:
            restore_info = self._wm.restore_snapshot(
                snapshot_id=head_snap_id,
                user_id=user_id,
                agent_id=agent_id,
                zone_id=zone_id,
            )

        logger.info("Checked out branch '%s' for workspace %s", target, workspace_path)
        return {
            "branch": target,
            "head_snapshot_id": head_snap_id,
            "restore_info": restore_info,
        }

    def log(
        self,
        workspace_path: str,
        branch_name: str | None = None,  # noqa: ARG002
        limit: int = 50,
        zone_id: str | None = None,
        user_id: str | None = None,
        agent_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """List snapshot history for a branch (delegates to WorkspaceManager.list_snapshots)."""
        return self._wm.list_snapshots(
            workspace_path=workspace_path,
            limit=limit,
            user_id=user_id,
            agent_id=agent_id,
            zone_id=zone_id,
        )

    def diff(
        self,
        workspace_path: str,  # noqa: ARG002
        from_ref: str,
        to_ref: str,
        zone_id: str | None = None,
        user_id: str | None = None,
        agent_id: str | None = None,
    ) -> dict[str, Any]:
        """Diff two snapshot IDs (delegates to WorkspaceManager.diff_snapshots)."""
        return self._wm.diff_snapshots(
            snapshot_id_1=from_ref,
            snapshot_id_2=to_ref,
            user_id=user_id,
            agent_id=agent_id,
            zone_id=zone_id,
        )

    # ------------------------------------------------------------------
    # Merge
    # ------------------------------------------------------------------

    def merge(
        self,
        workspace_path: str,
        source_branch: str,
        target_branch: str | None = None,
        strategy: str = "fail",
        zone_id: str | None = None,
        user_id: str | None = None,
        agent_id: str | None = None,
        created_by: str | None = None,
    ) -> MergeResult:
        """Three-way merge of source branch into target branch (A2-A).

        Args:
            workspace_path: Workspace containing the branches
            source_branch: Branch to merge FROM
            target_branch: Branch to merge INTO (default: current branch)
            strategy: Conflict strategy — 'fail' (default) or 'source-wins'
            zone_id: Zone for isolation
            user_id: User ID for permission check
            agent_id: Agent ID for permission check
            created_by: Who is performing the merge

        Returns:
            MergeResult with merge details

        Raises:
            BranchConflictError: If conflicts detected and strategy='fail'
            BranchNotFoundError: If source or target branch doesn't exist
            BranchStateError: If source branch is not active or already merged
        """
        # H2: Validate strategy upfront
        if strategy not in _VALID_STRATEGIES:
            raise ValueError(
                f"Invalid merge strategy: '{strategy}'. Must be one of: {sorted(_VALID_STRATEGIES)}"
            )

        self._check_permission(workspace_path, "branch:merge", user_id, agent_id, zone_id)
        z = self._resolve_zone(zone_id)

        with self._session_factory() as session:
            source = self._get_branch_model(session, z, workspace_path, source_branch)
            if not source:
                raise BranchNotFoundError(source_branch, workspace_path)

            # Validate source state
            if source.status == "merged":
                raise BranchStateError(source_branch, f"Branch '{source_branch}' is already merged")
            if source.status != "active":
                raise BranchStateError(
                    source_branch,
                    f"Cannot merge branch '{source_branch}' with status '{source.status}'",
                )

            # Resolve target
            target_name = target_branch or self._get_current_branch_name(session, z, workspace_path)

            # Prevent self-merge
            if source_branch == target_name:
                raise BranchStateError(source_branch, "Cannot merge a branch into itself")

            target = self._get_branch_model(session, z, workspace_path, target_name)
            if not target:
                raise BranchNotFoundError(target_name, workspace_path)

            target_head = target.head_snapshot_id
            fork_point = source.fork_point_id

            # Fast-forward: if target hasn't moved since fork
            if target_head == fork_point:
                return self._fast_forward_merge(
                    session,
                    source,
                    target,
                    workspace_path,
                    z,
                )

            # Three-way merge required
            return self._three_way_merge(
                session,
                source,
                target,
                workspace_path,
                z,
                strategy,
                created_by,
            )

    # ------------------------------------------------------------------
    # Explore / Finish Explore (A4-A)
    # ------------------------------------------------------------------

    def explore(
        self,
        workspace_path: str,
        description: str,
        zone_id: str | None = None,
        user_id: str | None = None,
        agent_id: str | None = None,
        created_by: str | None = None,
    ) -> ExploreResult:
        """Start an exploration: auto-commit (if changed) + create branch.

        P4-B: Skips commit if workspace manifest hash matches latest snapshot.

        Args:
            workspace_path: Workspace to explore
            description: Human-readable description (used for branch name)

        Returns:
            ExploreResult with branch info and fork point
        """
        self._check_permission(workspace_path, "branch:explore", user_id, agent_id, zone_id)
        z = self._resolve_zone(zone_id)

        # Ensure main branch exists
        self.ensure_main_branch(workspace_path, zone_id=z, created_by=created_by)

        # P4-B: Check if workspace has changed since last snapshot
        skipped_commit = False
        fork_snapshot_id: str | None = None

        with self._session_factory() as session:
            current_name = self._get_current_branch_name(session, z, workspace_path)
            current_branch = self._get_branch_model(session, z, workspace_path, current_name)
            if current_branch:
                fork_snapshot_id = current_branch.head_snapshot_id

        if fork_snapshot_id and self._workspace_unchanged(workspace_path, fork_snapshot_id):
            skipped_commit = True
            logger.info("explore(): workspace %s unchanged, skipping auto-commit", workspace_path)
        else:
            # Auto-commit current state
            commit_result = self.commit(
                workspace_path=workspace_path,
                message=f"Before: {description}",
                zone_id=zone_id,
                user_id=user_id,
                agent_id=agent_id,
                created_by=created_by,
            )
            fork_snapshot_id = commit_result["snapshot"]["snapshot_id"]

        # Create branch — H4: collision-avoidance suffix
        branch_name = _slugify(description)
        with self._session_factory() as session:
            if self._get_branch_model(session, z, workspace_path, branch_name):
                import uuid as _uuid

                branch_name = f"{branch_name}-{_uuid.uuid4().hex[:6]}"

        branch = self.create_branch(
            workspace_path=workspace_path,
            branch_name=branch_name,
            from_snapshot_id=fork_snapshot_id,
            zone_id=zone_id,
            user_id=user_id,
            agent_id=agent_id,
            created_by=created_by,
        )

        # Checkout the new branch
        self.checkout(
            workspace_path=workspace_path,
            target=branch_name,
            zone_id=zone_id,
            user_id=user_id,
            agent_id=agent_id,
        )

        return ExploreResult(
            branch_name=branch_name,
            branch_id=branch.id,
            fork_point_snapshot_id=fork_snapshot_id,
            skipped_commit=skipped_commit,
            message=f"Created branch '{branch_name}' from snapshot {fork_snapshot_id}",
        )

    def finish_explore(
        self,
        workspace_path: str,
        branch_name: str,
        outcome: str = "merge",
        strategy: str = "source-wins",
        zone_id: str | None = None,
        user_id: str | None = None,
        agent_id: str | None = None,
        created_by: str | None = None,
    ) -> dict[str, Any]:
        """Finish an exploration: merge or discard the branch (A4-A).

        Args:
            workspace_path: Workspace being explored
            branch_name: Branch to finish
            outcome: 'merge' to merge back, 'discard' to abandon
            strategy: Merge strategy if outcome='merge' (default: 'source-wins')

        Returns:
            Dict with outcome details
        """
        # H1: Permission check at top level (sub-methods have their own, but
        # this ensures early validation before partial operations)
        self._check_permission(workspace_path, "branch:explore", user_id, agent_id, zone_id)
        z = self._resolve_zone(zone_id)

        if outcome == "merge":
            # Merge exploration branch back into its parent
            with self._session_factory() as session:
                branch = self._get_branch_model(session, z, workspace_path, branch_name)
                if not branch:
                    raise BranchNotFoundError(branch_name, workspace_path)
                target = branch.parent_branch or DEFAULT_BRANCH

            merge_result = self.merge(
                workspace_path=workspace_path,
                source_branch=branch_name,
                target_branch=target,
                strategy=strategy,
                zone_id=zone_id,
                user_id=user_id,
                agent_id=agent_id,
                created_by=created_by,
            )

            # Checkout target branch after merge
            self.checkout(
                workspace_path=workspace_path,
                target=target,
                zone_id=zone_id,
                user_id=user_id,
                agent_id=agent_id,
            )

            return {
                "outcome": "merged",
                "branch": branch_name,
                "merged_into": target,
                "merge_result": merge_result,
            }
        elif outcome == "discard":
            # Discard the exploration branch
            branch_info = self.delete_branch(
                workspace_path=workspace_path,
                branch_name=branch_name,
                zone_id=zone_id,
                user_id=user_id,
                agent_id=agent_id,
            )

            # Checkout parent branch
            target = branch_info.parent_branch or DEFAULT_BRANCH
            self.checkout(
                workspace_path=workspace_path,
                target=target,
                zone_id=zone_id,
                user_id=user_id,
                agent_id=agent_id,
            )

            return {
                "outcome": "discarded",
                "branch": branch_name,
                "returned_to": target,
            }
        else:
            raise ValueError(f"Invalid outcome: '{outcome}'. Must be 'merge' or 'discard'")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_branch_model(
        self,
        session: Any,
        zone_id: str,
        workspace_path: str,
        branch_name: str,
    ) -> ContextBranchModel | None:
        """Fetch a branch model by name (within an open session)."""
        result: ContextBranchModel | None = session.execute(
            select(ContextBranchModel).where(
                ContextBranchModel.zone_id == zone_id,
                ContextBranchModel.workspace_path == workspace_path,
                ContextBranchModel.branch_name == branch_name,
            )
        ).scalar_one_or_none()
        return result

    def _get_current_branch_name(self, session: Any, zone_id: str, workspace_path: str) -> str:
        """Get the name of the currently checked-out branch, defaulting to 'main'."""
        result = session.execute(
            select(ContextBranchModel.branch_name).where(
                ContextBranchModel.zone_id == zone_id,
                ContextBranchModel.workspace_path == workspace_path,
                ContextBranchModel.is_current == True,  # noqa: E712
            )
        ).scalar_one_or_none()
        return result or DEFAULT_BRANCH

    def _advance_head_with_retry(
        self,
        zone_id: str,
        workspace_path: str,
        branch_name: str,
        new_snapshot_id: str,
    ) -> None:
        """Advance branch HEAD with optimistic concurrency + retry (P3-A)."""
        for attempt in range(_MAX_RETRIES):
            try:
                self._advance_head(zone_id, workspace_path, branch_name, new_snapshot_id)
                return
            except StalePointerError:
                if attempt == _MAX_RETRIES - 1:
                    raise
                backoff_ms = _BASE_BACKOFF_MS * (2**attempt)
                logger.warning(
                    "Stale pointer on branch '%s' (attempt %d/%d), retrying in %dms",
                    branch_name,
                    attempt + 1,
                    _MAX_RETRIES,
                    backoff_ms,
                )
                time.sleep(backoff_ms / 1000.0)

    def _advance_head(
        self,
        zone_id: str,
        workspace_path: str,
        branch_name: str,
        new_snapshot_id: str,
    ) -> None:
        """Advance branch HEAD atomically with optimistic concurrency (A3-B).

        Uses compare-and-swap on pointer_version to detect concurrent updates.
        """
        with self._session_factory() as session:
            branch = self._get_branch_model(session, zone_id, workspace_path, branch_name)
            if not branch:
                raise BranchNotFoundError(branch_name, workspace_path)
            # H6: Check status to prevent advancing HEAD on discarded/merged branches
            if branch.status != "active":
                raise BranchStateError(
                    branch_name,
                    f"Cannot advance head: branch '{branch_name}' has status '{branch.status}'",
                )

            expected_version = branch.pointer_version

            result = cast(
                CursorResult[Any],
                session.execute(
                    update(ContextBranchModel)
                    .where(
                        ContextBranchModel.id == branch.id,
                        ContextBranchModel.pointer_version == expected_version,
                    )
                    .values(
                        head_snapshot_id=new_snapshot_id,
                        pointer_version=expected_version + 1,
                        updated_at=datetime.now(UTC),
                    )
                ),
            )

            if result.rowcount == 0:
                # Re-read to get current version for error message
                session.expire(branch)
                session.refresh(branch)
                raise StalePointerError(branch_name, expected_version, branch.pointer_version)

            session.commit()

    def _workspace_unchanged(self, workspace_path: str, snapshot_id: str) -> bool:
        """Check if workspace manifest matches a snapshot (P4-B optimization)."""
        try:
            with self._session_factory() as session:
                snap = session.get(WorkspaceSnapshotModel, snapshot_id)
                if not snap:
                    return False
                existing_hash: str = snap.manifest_hash

            # Build current workspace manifest
            workspace_prefix = (
                workspace_path if workspace_path.endswith("/") else workspace_path + "/"
            )
            files = self._wm.metadata.list_iter(prefix=workspace_prefix)
            file_entries: list[tuple[str, str, int, str | None]] = []
            for file_meta in files:
                if file_meta.mime_type == "directory" or not file_meta.content_id:
                    continue
                rel_path = file_meta.path[len(workspace_prefix) :]
                file_entries.append(
                    (rel_path, file_meta.content_id, file_meta.size, file_meta.mime_type)
                )

            manifest = WorkspaceManifest.from_file_list(file_entries)
            import hashlib

            current_hash = hashlib.sha256(manifest.to_json()).hexdigest()
            return current_hash == existing_hash
        except (NexusFileNotFoundError, OSError, KeyError, ValueError) as exc:
            # H3: Narrow exceptions — only catch expected failures
            logger.debug("Could not check workspace unchanged (reason: %s), assuming changed", exc)
            return False

    def _fast_forward_merge(
        self,
        _session: Any,
        source: ContextBranchModel,
        target: ContextBranchModel,
        workspace_path: str,
        zone_id: str,
    ) -> MergeResult:
        """Fast-forward merge: target hasn't diverged, just advance pointer."""
        logger.info(
            "Fast-forward merge: '%s' → '%s' (workspace=%s)",
            source.branch_name,
            target.branch_name,
            workspace_path,
        )

        if not source.head_snapshot_id:
            raise BranchStateError(
                source.branch_name, "Cannot fast-forward merge: source has no commits"
            )

        # Capture values before any session operations
        source_head = source.head_snapshot_id
        source_name = source.branch_name
        target_name = target.branch_name

        # Advance target HEAD to source HEAD
        self._advance_head_with_retry(zone_id, workspace_path, target_name, source_head)

        # C1: Mark source as merged in a FRESH session (avoid stale session mutation)
        with self._session_factory() as s2:
            src = self._get_branch_model(s2, zone_id, workspace_path, source_name)
            if src:
                src.status = "merged"
                src.merged_into_branch = target_name
                src.merge_snapshot_id = source_head
                src.updated_at = datetime.now(UTC)
                s2.commit()

        return MergeResult(
            merged=True,
            merge_snapshot_id=source_head,
            files_added=0,
            files_removed=0,
            files_modified=0,
            fast_forward=True,
            strategy="fast-forward",
        )

    def _three_way_merge(
        self,
        session: Any,
        source: ContextBranchModel,
        target: ContextBranchModel,
        workspace_path: str,
        zone_id: str,
        strategy: str,
        created_by: str | None,
    ) -> MergeResult:
        """Three-way merge using fork point as common ancestor (A2-A)."""
        fork_point_id = source.fork_point_id
        source_head_id = source.head_snapshot_id
        target_head_id = target.head_snapshot_id

        if not fork_point_id or not source_head_id or not target_head_id:
            raise BranchStateError(
                source.branch_name,
                "Cannot perform three-way merge: missing fork point or head snapshots",
            )

        # Load all three manifests
        fork_manifest = self._load_manifest(session, fork_point_id)
        source_manifest = self._load_manifest(session, source_head_id)
        target_manifest = self._load_manifest(session, target_head_id)

        # Compute diffs relative to fork point
        source_changes = self._compute_changes(fork_manifest, source_manifest)
        target_changes = self._compute_changes(fork_manifest, target_manifest)

        # Detect conflicts: same path changed in both branches with different outcomes
        conflicts = []
        source_changed_paths = set(source_changes.keys())
        target_changed_paths = set(target_changes.keys())
        overlap = source_changed_paths & target_changed_paths

        for path in overlap:
            sc = source_changes[path]
            tc = target_changes[path]
            # Same change in both → no conflict
            if sc == tc:
                continue
            conflicts.append(path)

        if conflicts and strategy == "fail":
            raise BranchConflictError(source.branch_name, target.branch_name, sorted(conflicts))

        # Apply merge: start from target manifest, apply source changes
        merged_entries = dict(target_manifest.entries)

        for path, change in source_changes.items():
            if path in conflicts and strategy != "source-wins":
                continue  # Skip conflicts if not source-wins
            action, entry = change
            if action == "delete":
                merged_entries.pop(path, None)
            else:  # 'add' or 'modify'
                merged_entries[path] = entry

        # Build merged manifest
        merged_manifest = WorkspaceManifest(entries=merged_entries)
        merged_bytes = merged_manifest.to_json()
        merged_hash = self._wm.backend.write_content(merged_bytes, context=None).content_id

        # C3: Create merge snapshot with retry on IntegrityError (duplicate snapshot_number)
        merge_snap_id = self._create_merge_snapshot(
            workspace_path=workspace_path,
            manifest_hash=merged_hash,
            file_count=merged_manifest.file_count,
            total_size_bytes=merged_manifest.total_size,
            description=f"Merge '{source.branch_name}' into '{target.branch_name}'",
            created_by=created_by,
        )

        # Advance target HEAD
        self._advance_head_with_retry(zone_id, workspace_path, target.branch_name, merge_snap_id)

        # Mark source as merged
        with self._session_factory() as s2:
            src = self._get_branch_model(s2, zone_id, workspace_path, source.branch_name)
            if src:
                src.status = "merged"
                src.merged_into_branch = target.branch_name
                src.merge_snapshot_id = merge_snap_id
                src.updated_at = datetime.now(UTC)
                s2.commit()

        # Count changes
        files_added = sum(1 for _, (a, _) in source_changes.items() if a == "add")
        files_removed = sum(1 for _, (a, _) in source_changes.items() if a == "delete")
        files_modified = sum(1 for _, (a, _) in source_changes.items() if a == "modify")

        logger.info(
            "Three-way merge: '%s' → '%s' (added=%d, removed=%d, modified=%d, conflicts=%d, strategy=%s)",
            source.branch_name,
            target.branch_name,
            files_added,
            files_removed,
            files_modified,
            len(conflicts),
            strategy,
        )

        return MergeResult(
            merged=True,
            merge_snapshot_id=merge_snap_id,
            files_added=files_added,
            files_removed=files_removed,
            files_modified=files_modified,
            fast_forward=False,
            strategy=strategy,
        )

    def _create_merge_snapshot(
        self,
        workspace_path: str,
        manifest_hash: str,
        file_count: int,
        total_size_bytes: int,
        description: str,
        created_by: str | None,
        max_retries: int = 3,
    ) -> str:
        """Create a merge snapshot with retry on IntegrityError (C3).

        Retries with re-computed snapshot_number if a concurrent insert causes
        a duplicate constraint violation on (workspace_path, snapshot_number).

        Returns:
            The snapshot_id of the created snapshot.
        """
        from sqlalchemy import desc

        for attempt in range(max_retries):
            with self._session_factory() as session:
                next_number = (
                    session.execute(
                        select(WorkspaceSnapshotModel.snapshot_number)
                        .where(WorkspaceSnapshotModel.workspace_path == workspace_path)
                        .order_by(desc(WorkspaceSnapshotModel.snapshot_number))
                        .limit(1)
                    ).scalar_one_or_none()
                    or 0
                ) + 1

                merge_snap = WorkspaceSnapshotModel(
                    workspace_path=workspace_path,
                    snapshot_number=next_number,
                    manifest_hash=manifest_hash,
                    file_count=file_count,
                    total_size_bytes=total_size_bytes,
                    description=description,
                    created_by=created_by,
                )
                session.add(merge_snap)
                try:
                    session.commit()
                    session.refresh(merge_snap)
                    return merge_snap.snapshot_id
                except IntegrityError:
                    session.rollback()
                    if attempt == max_retries - 1:
                        raise
                    logger.warning(
                        "Snapshot number collision (attempt %d/%d), retrying",
                        attempt + 1,
                        max_retries,
                    )

        # Should never reach here, but satisfy type checker
        raise RuntimeError("Unreachable: merge snapshot creation exhausted retries")

    def _load_manifest(self, session: Any, snapshot_id: str) -> WorkspaceManifest:
        """Load a workspace manifest from CAS via snapshot ID."""
        snap = session.get(WorkspaceSnapshotModel, snapshot_id)
        if not snap:
            raise NexusFileNotFoundError(
                path=f"snapshot:{snapshot_id}", message="Snapshot not found"
            )
        manifest_bytes = self._wm.backend.read_content(snap.manifest_hash, context=None)
        return WorkspaceManifest.from_json(manifest_bytes)

    @staticmethod
    def _compute_changes(
        base: WorkspaceManifest, target: WorkspaceManifest
    ) -> dict[str, tuple[str, Any]]:
        """Compute changes between base and target manifests.

        Returns dict mapping path → (action, entry) where:
            action is 'add', 'modify', or 'delete'
            entry is the ManifestEntry (None for deletions)
        """
        changes: dict[str, tuple[str, Any]] = {}
        base_paths = base.paths()
        target_paths = target.paths()

        # Added files
        for path in target_paths - base_paths:
            changes[path] = ("add", target.get(path))

        # Removed files
        for path in base_paths - target_paths:
            changes[path] = ("delete", None)

        # Modified files
        for path in base_paths & target_paths:
            base_entry = base.get(path)
            target_entry = target.get(path)
            if base_entry and target_entry and base_entry.content_id != target_entry.content_id:
                changes[path] = ("modify", target_entry)

        return changes
