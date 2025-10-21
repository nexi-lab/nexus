"""Storage layer for permission policies.

This module provides database operations for storing and retrieving
permission policies.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.orm import Session

from nexus.core.permission_policy import PermissionPolicy

if TYPE_CHECKING:
    from nexus.storage.models import PermissionPolicyModel


class PolicyStore:
    """Storage interface for permission policies.

    Handles database operations for permission policies including
    CRUD operations and policy lookup.
    """

    def __init__(self, session: Session):
        """Initialize policy store.

        Args:
            session: SQLAlchemy session
        """
        self.session = session

    def create_policy(self, policy: PermissionPolicy) -> None:
        """Create a new permission policy.

        Args:
            policy: Policy to create
        """
        from nexus.storage.models import PermissionPolicyModel

        # Validate policy
        self._validate_policy(policy)

        # Create model
        model = PermissionPolicyModel(
            policy_id=policy.policy_id,
            namespace_pattern=policy.namespace_pattern,
            tenant_id=policy.tenant_id,
            default_owner=policy.default_owner,
            default_group=policy.default_group,
            default_mode=policy.default_mode,
            priority=policy.priority,
        )

        self.session.add(model)
        self.session.commit()

    def get_policy(self, policy_id: str) -> PermissionPolicy | None:
        """Get a policy by ID.

        Args:
            policy_id: Policy ID

        Returns:
            Policy if found, None otherwise
        """
        from nexus.storage.models import PermissionPolicyModel

        stmt = select(PermissionPolicyModel).where(PermissionPolicyModel.policy_id == policy_id)
        model = self.session.scalar(stmt)

        if model is None:
            return None

        return self._model_to_policy(model)

    def list_policies(self, tenant_id: str | None = None) -> list[PermissionPolicy]:
        """List all policies, optionally filtered by tenant.

        Args:
            tenant_id: Tenant ID to filter by (None = all policies)

        Returns:
            List of policies
        """
        from nexus.storage.models import PermissionPolicyModel

        stmt = select(PermissionPolicyModel)

        if tenant_id is not None:
            stmt = stmt.where(
                (PermissionPolicyModel.tenant_id == tenant_id)
                | (PermissionPolicyModel.tenant_id.is_(None))
            )

        # Order by priority descending
        stmt = stmt.order_by(PermissionPolicyModel.priority.desc())

        models = self.session.scalars(stmt).all()
        return [self._model_to_policy(m) for m in models]

    def update_policy(self, policy: PermissionPolicy) -> None:
        """Update an existing policy.

        Args:
            policy: Policy with updated fields
        """
        from nexus.storage.models import PermissionPolicyModel

        # Validate policy
        self._validate_policy(policy)

        # Find existing model
        stmt = select(PermissionPolicyModel).where(
            PermissionPolicyModel.policy_id == policy.policy_id
        )
        model = self.session.scalar(stmt)

        if model is None:
            raise ValueError(f"Policy not found: {policy.policy_id}")

        # Update fields
        model.namespace_pattern = policy.namespace_pattern
        model.tenant_id = policy.tenant_id
        model.default_owner = policy.default_owner
        model.default_group = policy.default_group
        model.default_mode = policy.default_mode
        model.priority = policy.priority

        self.session.commit()

    def delete_policy(self, policy_id: str) -> None:
        """Delete a policy.

        Args:
            policy_id: Policy ID
        """
        from nexus.storage.models import PermissionPolicyModel

        stmt = select(PermissionPolicyModel).where(PermissionPolicyModel.policy_id == policy_id)
        model = self.session.scalar(stmt)

        if model is not None:
            self.session.delete(model)
            self.session.commit()

    def find_policies_for_path(
        self, path: str, tenant_id: str | None = None
    ) -> list[PermissionPolicy]:
        """Find all policies that could match a path.

        This returns all potentially matching policies. The caller should
        use PolicyMatcher to select the best match.

        Args:
            path: Virtual path
            tenant_id: Tenant ID (None = system-wide only)

        Returns:
            List of potentially matching policies
        """
        # Get all policies for the tenant (including system-wide)
        policies = self.list_policies(tenant_id)

        # Filter to only matching policies
        return [p for p in policies if p.matches(path)]

    def _model_to_policy(self, model: PermissionPolicyModel) -> PermissionPolicy:
        """Convert database model to policy object.

        Args:
            model: Database model

        Returns:
            PermissionPolicy object
        """
        return PermissionPolicy(
            policy_id=model.policy_id,
            namespace_pattern=model.namespace_pattern,
            tenant_id=model.tenant_id,
            default_owner=model.default_owner,
            default_group=model.default_group,
            default_mode=model.default_mode,
            priority=model.priority,
        )

    def _validate_policy(self, policy: PermissionPolicy) -> None:
        """Validate policy before database operations.

        Args:
            policy: Policy to validate

        Raises:
            ValueError: If policy is invalid
        """
        if not policy.policy_id:
            raise ValueError("policy_id is required")

        if not policy.namespace_pattern:
            raise ValueError("namespace_pattern is required")

        if not policy.default_owner:
            raise ValueError("default_owner is required")

        if not policy.default_group:
            raise ValueError("default_group is required")

        if not 0 <= policy.default_mode <= 0o777:
            raise ValueError(
                f"default_mode must be between 0o000 and 0o777, got {oct(policy.default_mode)}"
            )

        if policy.priority < 0:
            raise ValueError(f"priority must be non-negative, got {policy.priority}")
