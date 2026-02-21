"""Skill governance and approval workflows."""

from __future__ import annotations

import dataclasses
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from nexus.bricks.skills.exceptions import SkillPermissionDeniedError, SkillValidationError

if TYPE_CHECKING:
    from nexus.core.cache_store import CacheStoreABC
    from nexus.services.protocols.rebac import ReBACBrickProtocol

logger = logging.getLogger(__name__)


class ApprovalStatus(StrEnum):
    """Status of a skill approval request."""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


@dataclass
class SkillApproval:
    """Skill approval request."""

    approval_id: str
    skill_name: str
    submitted_by: str
    status: ApprovalStatus
    reviewers: list[str] | None = None
    comments: str | None = None
    submitted_at: datetime | None = None
    reviewed_at: datetime | None = None
    reviewed_by: str | None = None

    def validate(self) -> None:
        """Validate approval record.

        Raises:
            SkillValidationError: If validation fails.
        """
        if not self.approval_id:
            raise SkillValidationError("approval_id is required")

        if not self.skill_name:
            raise SkillValidationError("skill_name is required")

        if not self.submitted_by:
            raise SkillValidationError("submitted_by is required")

        if not isinstance(self.status, ApprovalStatus):
            raise SkillValidationError(f"status must be ApprovalStatus, got {type(self.status)}")


class GovernanceError(SkillValidationError):
    """Raised when governance operations fail."""

    pass


# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------

_KEY_PREFIX = "skills:approval"


def _approval_key(approval_id: str) -> str:
    return f"{_KEY_PREFIX}:{approval_id}"


def _json_default(obj: object) -> str:
    if isinstance(obj, datetime):
        return obj.isoformat()
    raise TypeError(f"Not JSON serializable: {type(obj)}")


def _serialize_approval(approval: SkillApproval) -> bytes:
    return json.dumps(dataclasses.asdict(approval), default=_json_default).encode()


def _deserialize_approval(raw: bytes) -> SkillApproval:
    data: dict[str, Any] = json.loads(raw)
    data["status"] = ApprovalStatus(data["status"])
    if data.get("submitted_at"):
        data["submitted_at"] = datetime.fromisoformat(data["submitted_at"])
    if data.get("reviewed_at"):
        data["reviewed_at"] = datetime.fromisoformat(data["reviewed_at"])
    return SkillApproval(**data)


class SkillGovernance:
    """Governance system for skill approvals.

    Features:
    - Approval workflow for org-wide skill publication
    - Review process with multiple reviewers
    - Approval tracking and status management
    - Only approved skills can be published to /shared/

    Example:
        >>> from nexus.bricks.skills.governance import SkillGovernance
        >>>
        >>> # Initialize governance
        >>> gov = SkillGovernance()
        >>>
        >>> # Submit skill for approval
        >>> approval_id = await gov.submit_for_approval(
        ...     "my-analyzer",
        ...     submitted_by="alice",
        ...     reviewers=["bob", "charlie"]
        ... )
        >>>
        >>> # Review and approve
        >>> await gov.approve_skill(
        ...     approval_id,
        ...     reviewed_by="bob",
        ...     comments="Looks great!"
        ... )
        >>>
        >>> # Check if skill is approved
        >>> is_approved = await gov.is_approved("my-analyzer")
    """

    def __init__(
        self,
        rebac_manager: ReBACBrickProtocol | None = None,
        cache_store: CacheStoreABC | None = None,
    ):
        """Initialize governance system.

        Args:
            rebac_manager: Optional ReBAC manager for permission checks
            cache_store: Optional CacheStoreABC for ephemeral approval storage.
                         Defaults to InMemoryCacheStore when *None*.
        """
        self._rebac = rebac_manager
        if cache_store is not None:
            self._cache: CacheStoreABC = cache_store
        else:
            from nexus.core.cache_store import InMemoryCacheStore

            self._cache = InMemoryCacheStore()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _save_approval(self, approval: SkillApproval) -> None:
        """Persist an approval record to the cache."""
        await self._cache.set(
            _approval_key(approval.approval_id),
            _serialize_approval(approval),
        )

    async def _get_approval(self, approval_id: str) -> SkillApproval | None:
        """Get approval by ID."""
        raw = await self._cache.get(_approval_key(approval_id))
        if raw is None:
            return None
        return _deserialize_approval(raw)

    async def _all_approvals(self) -> list[SkillApproval]:
        """Load all approval records from the cache."""
        keys = await self._cache.keys_by_pattern(f"{_KEY_PREFIX}:*")
        if not keys:
            return []
        values = await self._cache.get_many(keys)
        return [_deserialize_approval(v) for v in values.values() if v is not None]

    async def _get_pending_approval(self, skill_name: str) -> SkillApproval | None:
        """Get pending approval for a skill."""
        all_approvals = await self._all_approvals()
        pending = [
            a
            for a in all_approvals
            if a.skill_name == skill_name and a.status == ApprovalStatus.PENDING
        ]
        if not pending:
            return None
        return max(pending, key=lambda a: a.submitted_at or datetime.min)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def submit_for_approval(
        self,
        skill_name: str,
        submitted_by: str,
        reviewers: list[str] | None = None,
        comments: str | None = None,
    ) -> str:
        """Submit a skill for approval to publish to zone library.

        Args:
            skill_name: Name of the skill
            submitted_by: ID of the submitter
            reviewers: Optional list of reviewer IDs
            comments: Optional submission comments

        Returns:
            Approval ID

        Raises:
            GovernanceError: If submission fails

        Example:
            >>> approval_id = await gov.submit_for_approval(
            ...     "analyze-code",
            ...     submitted_by="alice",
            ...     reviewers=["bob", "charlie"],
            ...     comments="Ready for org-wide use"
            ... )
        """
        # Check if there's already a pending approval
        existing = await self._get_pending_approval(skill_name)
        if existing:
            raise GovernanceError(
                f"Skill '{skill_name}' already has a pending approval (ID: {existing.approval_id})"
            )

        approval_id = str(uuid.uuid4())
        submitted_at = datetime.now(UTC)

        approval = SkillApproval(
            approval_id=approval_id,
            skill_name=skill_name,
            submitted_by=submitted_by,
            status=ApprovalStatus.PENDING,
            reviewers=reviewers,
            comments=comments,
            submitted_at=submitted_at,
        )

        approval.validate()

        await self._save_approval(approval)

        logger.info(f"Submitted skill '{skill_name}' for approval (ID: {approval_id})")
        return approval_id

    async def approve_skill(
        self,
        approval_id: str,
        reviewed_by: str,
        comments: str | None = None,
        reviewer_type: str = "user",
        zone_id: str | None = None,
    ) -> None:
        """Approve a skill for publication.

        Args:
            approval_id: ID of the approval request
            reviewed_by: ID of the reviewer
            comments: Optional review comments
            reviewer_type: Type of reviewer (user, agent) - default: user
            zone_id: Zone ID for scoping (for ReBAC)

        Raises:
            GovernanceError: If approval fails
            SkillPermissionDeniedError: If reviewer lacks approve permission

        Example:
            >>> await gov.approve_skill(
            ...     approval_id,
            ...     reviewed_by="bob",
            ...     comments="Code quality is excellent!"
            ... )
        """
        approval = await self._get_approval(approval_id)
        if not approval:
            raise GovernanceError(f"Approval not found: {approval_id}")

        if approval.status != ApprovalStatus.PENDING:
            raise GovernanceError(
                f"Approval {approval_id} is already {approval.status.value}, cannot approve"
            )

        # Check approve permission
        if self._rebac:
            try:
                has_permission = self._rebac.rebac_check(
                    subject=(reviewer_type, reviewed_by),
                    permission="approve",
                    object=("skill", approval.skill_name),
                    zone_id=zone_id,
                )
                if not has_permission:
                    raise SkillPermissionDeniedError(
                        f"No permission to approve skill '{approval.skill_name}'. "
                        f"Reviewer ({reviewer_type}:{reviewed_by}) lacks 'approve' permission."
                    )
            except SkillPermissionDeniedError:
                # Re-raise permission errors
                raise
            except Exception as e:
                logger.warning(
                    f"ReBAC check failed for approval of skill '{approval.skill_name}': {e}"
                )

        reviewed_at = datetime.now(UTC)

        approval.status = ApprovalStatus.APPROVED
        approval.reviewed_by = reviewed_by
        approval.reviewed_at = reviewed_at
        if comments:
            approval.comments = comments

        await self._save_approval(approval)

        logger.info(f"Approved skill '{approval.skill_name}' (ID: {approval_id}) by {reviewed_by}")

    async def reject_skill(
        self,
        approval_id: str,
        reviewed_by: str,
        comments: str | None = None,
        reviewer_type: str = "user",
        zone_id: str | None = None,
    ) -> None:
        """Reject a skill approval request.

        Args:
            approval_id: ID of the approval request
            reviewed_by: ID of the reviewer
            comments: Optional rejection reason
            reviewer_type: Type of reviewer (user, agent) - default: user
            zone_id: Zone ID for scoping (for ReBAC)

        Raises:
            GovernanceError: If rejection fails
            SkillPermissionDeniedError: If reviewer lacks approve permission

        Example:
            >>> await gov.reject_skill(
            ...     approval_id,
            ...     reviewed_by="bob",
            ...     comments="Needs more documentation"
            ... )
        """
        approval = await self._get_approval(approval_id)
        if not approval:
            raise GovernanceError(f"Approval not found: {approval_id}")

        if approval.status != ApprovalStatus.PENDING:
            raise GovernanceError(
                f"Approval {approval_id} is already {approval.status.value}, cannot reject"
            )

        # Check approve permission (same as approve - reviewer can approve or reject)
        if self._rebac:
            try:
                has_permission = self._rebac.rebac_check(
                    subject=(reviewer_type, reviewed_by),
                    permission="approve",
                    object=("skill", approval.skill_name),
                    zone_id=zone_id,
                )
                if not has_permission:
                    raise SkillPermissionDeniedError(
                        f"No permission to reject skill '{approval.skill_name}'. "
                        f"Reviewer ({reviewer_type}:{reviewed_by}) lacks 'approve' permission."
                    )
            except SkillPermissionDeniedError:
                # Re-raise permission errors
                raise
            except Exception as e:
                logger.warning(
                    f"ReBAC check failed for rejection of skill '{approval.skill_name}': {e}"
                )

        reviewed_at = datetime.now(UTC)

        approval.status = ApprovalStatus.REJECTED
        approval.reviewed_by = reviewed_by
        approval.reviewed_at = reviewed_at
        if comments:
            approval.comments = comments

        await self._save_approval(approval)

        logger.info(f"Rejected skill '{approval.skill_name}' (ID: {approval_id}) by {reviewed_by}")

    async def is_approved(self, skill_name: str) -> bool:
        """Check if a skill is approved for org-wide use.

        Args:
            skill_name: Name of the skill

        Returns:
            True if approved, False otherwise

        Example:
            >>> if await gov.is_approved("analyze-code"):
            ...     print("Skill is approved!")
        """
        all_approvals = await self._all_approvals()
        approvals = [a for a in all_approvals if a.skill_name == skill_name]

        if not approvals:
            return False

        # Get most recent approval
        latest = max(approvals, key=lambda a: a.submitted_at or datetime.min)
        return latest.status == ApprovalStatus.APPROVED

    async def get_pending_approvals(self, reviewer: str | None = None) -> list[SkillApproval]:
        """Get all pending approval requests.

        Args:
            reviewer: Optional reviewer ID to filter by

        Returns:
            List of pending approvals

        Example:
            >>> pending = await gov.get_pending_approvals()
            >>> for approval in pending:
            ...     print(f"{approval.skill_name} by {approval.submitted_by}")
            >>>
            >>> # Get approvals assigned to specific reviewer
            >>> my_approvals = await gov.get_pending_approvals(reviewer="bob")
        """
        all_approvals = await self._all_approvals()
        approvals = [a for a in all_approvals if a.status == ApprovalStatus.PENDING]

        if reviewer:
            approvals = [a for a in approvals if a.reviewers and reviewer in a.reviewers]

        return sorted(approvals, key=lambda a: a.submitted_at or datetime.min, reverse=True)

    async def get_approval_history(self, skill_name: str) -> list[SkillApproval]:
        """Get approval history for a skill.

        Args:
            skill_name: Name of the skill

        Returns:
            List of approval records, sorted by submission date (newest first)

        Example:
            >>> history = await gov.get_approval_history("analyze-code")
            >>> for approval in history:
            ...     print(f"{approval.status.value} by {approval.reviewed_by} at {approval.reviewed_at}")
        """
        all_approvals = await self._all_approvals()
        approvals = [a for a in all_approvals if a.skill_name == skill_name]
        return sorted(approvals, key=lambda a: a.submitted_at or datetime.min, reverse=True)

    async def list_approvals(
        self, status: str | None = None, skill_name: str | None = None
    ) -> list[SkillApproval]:
        """List approval requests with optional filters.

        Args:
            status: Optional status filter (pending, approved, rejected)
            skill_name: Optional skill name filter

        Returns:
            List of approval records matching filters

        Example:
            >>> # List all approvals
            >>> all_approvals = await gov.list_approvals()
            >>>
            >>> # List pending approvals
            >>> pending = await gov.list_approvals(status="pending")
            >>>
            >>> # List approvals for a specific skill
            >>> skill_approvals = await gov.list_approvals(skill_name="my-analyzer")
        """
        approvals = await self._all_approvals()

        if status:
            status_enum = ApprovalStatus(status)
            approvals = [a for a in approvals if a.status == status_enum]

        if skill_name:
            approvals = [a for a in approvals if a.skill_name == skill_name]

        return sorted(approvals, key=lambda a: a.submitted_at or datetime.min, reverse=True)
