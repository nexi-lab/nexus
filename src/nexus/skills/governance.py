"""Skill governance and approval workflows."""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol

from nexus.core.exceptions import PermissionDeniedError, ValidationError

if TYPE_CHECKING:
    from nexus.core.rebac_manager import ReBACManager

logger = logging.getLogger(__name__)


class DatabaseConnection(Protocol):
    """Protocol for database connections."""

    def execute(self, query: str, params: dict[str, Any] | None = None) -> Any:
        """Execute a query."""
        ...

    def fetchall(self, query: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """Fetch all results from a query."""
        ...

    def fetchone(self, query: str, params: dict[str, Any] | None = None) -> dict[str, Any] | None:
        """Fetch one result from a query."""
        ...

    def commit(self) -> None:
        """Commit the transaction."""
        ...


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
            ValidationError: If validation fails.
        """
        if not self.approval_id:
            raise ValidationError("approval_id is required")

        if not self.skill_name:
            raise ValidationError("skill_name is required")

        if not self.submitted_by:
            raise ValidationError("submitted_by is required")

        if not isinstance(self.status, ApprovalStatus):
            raise ValidationError(f"status must be ApprovalStatus, got {type(self.status)}")


class GovernanceError(ValidationError):
    """Raised when governance operations fail."""

    pass


class SkillGovernance:
    """Governance system for skill approvals.

    Features:
    - Approval workflow for org-wide skill publication
    - Review process with multiple reviewers
    - Approval tracking and status management
    - Only approved skills can be published to /shared/

    Example:
        >>> from nexus.skills import SkillGovernance
        >>>
        >>> # Initialize governance
        >>> gov = SkillGovernance(db_connection)
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
        db_connection: DatabaseConnection | None = None,
        rebac_manager: ReBACManager | None = None,
    ):
        """Initialize governance system.

        Args:
            db_connection: Optional database connection (defaults to in-memory)
            rebac_manager: Optional ReBAC manager for permission checks
        """
        self._db = db_connection
        self._rebac = rebac_manager
        self._in_memory_approvals: dict[str, SkillApproval] = {}

    async def submit_for_approval(
        self,
        skill_name: str,
        submitted_by: str,
        reviewers: list[str] | None = None,
        comments: str | None = None,
    ) -> str:
        """Submit a skill for approval to publish to tenant library.

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

        if self._db:
            # Insert into database
            query = """
            INSERT INTO skill_approvals (
                approval_id, skill_name, submitted_by, status,
                reviewers, comments, submitted_at
            ) VALUES (
                :approval_id, :skill_name, :submitted_by, :status,
                :reviewers, :comments, :submitted_at
            )
            """
            import json

            self._db.execute(
                query,
                {
                    "approval_id": approval_id,
                    "skill_name": skill_name,
                    "submitted_by": submitted_by,
                    "status": approval.status.value,
                    "reviewers": json.dumps(reviewers) if reviewers else None,
                    "comments": comments,
                    "submitted_at": submitted_at,
                },
            )
            self._db.commit()
        else:
            # Store in memory
            self._in_memory_approvals[approval_id] = approval

        logger.info(f"Submitted skill '{skill_name}' for approval (ID: {approval_id})")
        return approval_id

    async def approve_skill(
        self,
        approval_id: str,
        reviewed_by: str,
        comments: str | None = None,
        reviewer_type: str = "user",
        tenant_id: str | None = None,
    ) -> None:
        """Approve a skill for publication.

        Args:
            approval_id: ID of the approval request
            reviewed_by: ID of the reviewer
            comments: Optional review comments
            reviewer_type: Type of reviewer (user, agent) - default: user
            tenant_id: Tenant ID for scoping (for ReBAC)

        Raises:
            GovernanceError: If approval fails
            PermissionDeniedError: If reviewer lacks approve permission

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
                    tenant_id=tenant_id,
                )
                if not has_permission:
                    raise PermissionDeniedError(
                        f"No permission to approve skill '{approval.skill_name}'. "
                        f"Reviewer ({reviewer_type}:{reviewed_by}) lacks 'approve' permission."
                    )
            except PermissionDeniedError:
                # Re-raise permission errors
                raise
            except Exception as e:
                logger.warning(
                    f"ReBAC check failed for approval of skill '{approval.skill_name}': {e}"
                )
                # Continue if ReBAC check fails (backward compatibility)

        reviewed_at = datetime.now(UTC)

        if self._db:
            # Update in database
            query = """
            UPDATE skill_approvals
            SET status = :status,
                reviewed_by = :reviewed_by,
                reviewed_at = :reviewed_at,
                comments = :comments
            WHERE approval_id = :approval_id
            """
            self._db.execute(
                query,
                {
                    "status": ApprovalStatus.APPROVED.value,
                    "reviewed_by": reviewed_by,
                    "reviewed_at": reviewed_at,
                    "comments": comments or approval.comments,
                    "approval_id": approval_id,
                },
            )
            self._db.commit()
        else:
            # Update in memory
            approval.status = ApprovalStatus.APPROVED
            approval.reviewed_by = reviewed_by
            approval.reviewed_at = reviewed_at
            if comments:
                approval.comments = comments

        logger.info(f"Approved skill '{approval.skill_name}' (ID: {approval_id}) by {reviewed_by}")

    async def reject_skill(
        self,
        approval_id: str,
        reviewed_by: str,
        comments: str | None = None,
        reviewer_type: str = "user",
        tenant_id: str | None = None,
    ) -> None:
        """Reject a skill approval request.

        Args:
            approval_id: ID of the approval request
            reviewed_by: ID of the reviewer
            comments: Optional rejection reason
            reviewer_type: Type of reviewer (user, agent) - default: user
            tenant_id: Tenant ID for scoping (for ReBAC)

        Raises:
            GovernanceError: If rejection fails
            PermissionDeniedError: If reviewer lacks approve permission

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
                    tenant_id=tenant_id,
                )
                if not has_permission:
                    raise PermissionDeniedError(
                        f"No permission to reject skill '{approval.skill_name}'. "
                        f"Reviewer ({reviewer_type}:{reviewed_by}) lacks 'approve' permission."
                    )
            except PermissionDeniedError:
                # Re-raise permission errors
                raise
            except Exception as e:
                logger.warning(
                    f"ReBAC check failed for rejection of skill '{approval.skill_name}': {e}"
                )
                # Continue if ReBAC check fails (backward compatibility)

        reviewed_at = datetime.now(UTC)

        if self._db:
            # Update in database
            query = """
            UPDATE skill_approvals
            SET status = :status,
                reviewed_by = :reviewed_by,
                reviewed_at = :reviewed_at,
                comments = :comments
            WHERE approval_id = :approval_id
            """
            self._db.execute(
                query,
                {
                    "status": ApprovalStatus.REJECTED.value,
                    "reviewed_by": reviewed_by,
                    "reviewed_at": reviewed_at,
                    "comments": comments or approval.comments,
                    "approval_id": approval_id,
                },
            )
            self._db.commit()
        else:
            # Update in memory
            approval.status = ApprovalStatus.REJECTED
            approval.reviewed_by = reviewed_by
            approval.reviewed_at = reviewed_at
            if comments:
                approval.comments = comments

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
        if self._db:
            # Query database
            query = """
            SELECT status FROM skill_approvals
            WHERE skill_name = :skill_name
            ORDER BY submitted_at DESC
            LIMIT 1
            """
            result = self._db.fetchone(query, {"skill_name": skill_name})

            if not result:
                return False

            return result.get("status") == ApprovalStatus.APPROVED.value

        else:
            # Check in-memory approvals
            approvals = [
                a for a in self._in_memory_approvals.values() if a.skill_name == skill_name
            ]

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
        if self._db:
            # Query database
            query = """
            SELECT * FROM skill_approvals
            WHERE status = :status
            """
            params: dict[str, Any] = {"status": ApprovalStatus.PENDING.value}

            if reviewer:
                # Check if reviewer is in the reviewers list (JSON array)
                # This is database-specific; adjust for your DB
                query += " AND :reviewer IN (SELECT value FROM json_each(reviewers))"
                params["reviewer"] = reviewer

            query += " ORDER BY submitted_at DESC"

            import json

            results = self._db.fetchall(query, params)
            approvals = []
            for row in results:
                # Handle JSON column - PostgreSQL auto-deserializes, SQLite returns string
                reviewers_data = row.get("reviewers")
                if isinstance(reviewers_data, str):
                    reviewers = json.loads(reviewers_data)
                else:
                    reviewers = reviewers_data  # Already deserialized (PostgreSQL)
                approvals.append(
                    SkillApproval(
                        approval_id=row["approval_id"],
                        skill_name=row["skill_name"],
                        submitted_by=row["submitted_by"],
                        status=ApprovalStatus(row["status"]),
                        reviewers=reviewers,
                        comments=row.get("comments"),
                        submitted_at=row.get("submitted_at"),
                        reviewed_at=row.get("reviewed_at"),
                        reviewed_by=row.get("reviewed_by"),
                    )
                )
            return approvals

        else:
            # Filter in-memory approvals
            approvals = [
                a for a in self._in_memory_approvals.values() if a.status == ApprovalStatus.PENDING
            ]

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
        if self._db:
            # Query database
            query = """
            SELECT * FROM skill_approvals
            WHERE skill_name = :skill_name
            ORDER BY submitted_at DESC
            """

            import json

            results = self._db.fetchall(query, {"skill_name": skill_name})
            approvals = []
            for row in results:
                # Handle JSON column - PostgreSQL auto-deserializes, SQLite returns string
                reviewers_data = row.get("reviewers")
                if isinstance(reviewers_data, str):
                    reviewers = json.loads(reviewers_data)
                else:
                    reviewers = reviewers_data  # Already deserialized (PostgreSQL)
                approvals.append(
                    SkillApproval(
                        approval_id=row["approval_id"],
                        skill_name=row["skill_name"],
                        submitted_by=row["submitted_by"],
                        status=ApprovalStatus(row["status"]),
                        reviewers=reviewers,
                        comments=row.get("comments"),
                        submitted_at=row.get("submitted_at"),
                        reviewed_at=row.get("reviewed_at"),
                        reviewed_by=row.get("reviewed_by"),
                    )
                )
            return approvals

        else:
            # Filter in-memory approvals
            approvals = [
                a for a in self._in_memory_approvals.values() if a.skill_name == skill_name
            ]
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
        if self._db:
            # Build query with filters
            query = "SELECT * FROM skill_approvals WHERE 1=1"
            params: dict[str, Any] = {}

            if status:
                query += " AND status = :status"
                params["status"] = status

            if skill_name:
                query += " AND skill_name = :skill_name"
                params["skill_name"] = skill_name

            query += " ORDER BY submitted_at DESC"

            import json

            results = self._db.fetchall(query, params)
            approvals = []
            for row in results:
                # Handle JSON column - PostgreSQL auto-deserializes, SQLite returns string
                reviewers_data = row.get("reviewers")
                if isinstance(reviewers_data, str):
                    reviewers = json.loads(reviewers_data)
                else:
                    reviewers = reviewers_data  # Already deserialized (PostgreSQL)
                approvals.append(
                    SkillApproval(
                        approval_id=row["approval_id"],
                        skill_name=row["skill_name"],
                        submitted_by=row["submitted_by"],
                        status=ApprovalStatus(row["status"]),
                        reviewers=reviewers,
                        comments=row.get("comments"),
                        submitted_at=row.get("submitted_at"),
                        reviewed_at=row.get("reviewed_at"),
                        reviewed_by=row.get("reviewed_by"),
                    )
                )
            return approvals

        else:
            # Filter in-memory approvals
            approvals = list(self._in_memory_approvals.values())

            if status:
                status_enum = ApprovalStatus(status)
                approvals = [a for a in approvals if a.status == status_enum]

            if skill_name:
                approvals = [a for a in approvals if a.skill_name == skill_name]

            return sorted(approvals, key=lambda a: a.submitted_at or datetime.min, reverse=True)

    async def _get_approval(self, approval_id: str) -> SkillApproval | None:
        """Get approval by ID (internal helper)."""
        if self._db:
            query = "SELECT * FROM skill_approvals WHERE approval_id = :approval_id"

            import json

            result = self._db.fetchone(query, {"approval_id": approval_id})
            if not result:
                return None

            # Handle JSON column - PostgreSQL auto-deserializes, SQLite returns string
            reviewers_data = result.get("reviewers")
            if isinstance(reviewers_data, str):
                reviewers = json.loads(reviewers_data)
            else:
                reviewers = reviewers_data  # Already deserialized (PostgreSQL)
            return SkillApproval(
                approval_id=result["approval_id"],
                skill_name=result["skill_name"],
                submitted_by=result["submitted_by"],
                status=ApprovalStatus(result["status"]),
                reviewers=reviewers,
                comments=result.get("comments"),
                submitted_at=result.get("submitted_at"),
                reviewed_at=result.get("reviewed_at"),
                reviewed_by=result.get("reviewed_by"),
            )
        else:
            return self._in_memory_approvals.get(approval_id)

    async def _get_pending_approval(self, skill_name: str) -> SkillApproval | None:
        """Get pending approval for a skill (internal helper)."""
        if self._db:
            query = """
            SELECT * FROM skill_approvals
            WHERE skill_name = :skill_name AND status = :status
            ORDER BY submitted_at DESC
            LIMIT 1
            """

            import json

            result = self._db.fetchone(
                query, {"skill_name": skill_name, "status": ApprovalStatus.PENDING.value}
            )

            if not result:
                return None

            # Handle JSON column - PostgreSQL auto-deserializes, SQLite returns string
            reviewers_data = result.get("reviewers")
            if isinstance(reviewers_data, str):
                reviewers = json.loads(reviewers_data)
            else:
                reviewers = reviewers_data  # Already deserialized (PostgreSQL)
            return SkillApproval(
                approval_id=result["approval_id"],
                skill_name=result["skill_name"],
                submitted_by=result["submitted_by"],
                status=ApprovalStatus(result["status"]),
                reviewers=reviewers,
                comments=result.get("comments"),
                submitted_at=result.get("submitted_at"),
                reviewed_at=result.get("reviewed_at"),
                reviewed_by=result.get("reviewed_by"),
            )
        else:
            # Find in memory
            pending = [
                a
                for a in self._in_memory_approvals.values()
                if a.skill_name == skill_name and a.status == ApprovalStatus.PENDING
            ]

            if not pending:
                return None

            # Return most recent
            return max(pending, key=lambda a: a.submitted_at or datetime.min)
