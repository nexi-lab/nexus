"""Delegation service — core business logic (Issue #1271).

Orchestrates agent identity delegation: coordinator agents can
provision worker agents with narrower permissions.

Uses ordered steps for safety without requiring cross-system transactions:
    1. Register worker agent (harmless, no permissions)
    2. Create ReBAC grants (permissions materialized)
    3. Create API key (activation — only after grants exist)
    4. Persist DelegationRecord (audit trail)

On failure at step 2+: unregister agent (no key exists, safe to retry).
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from nexus.services.delegation.derivation import MAX_DELEGATABLE_GRANTS, GrantSpec, derive_grants
from nexus.services.delegation.errors import (
    DelegationChainError,
    DelegationError,
    DelegationNotFoundError,
)
from nexus.services.delegation.models import DelegationMode, DelegationRecord, DelegationResult

if TYPE_CHECKING:
    from sqlalchemy.orm import Session, sessionmaker

    from nexus.services.agents.agent_registry import AgentRegistry
    from nexus.services.permissions.entity_registry import EntityRegistry
    from nexus.services.permissions.namespace_manager import NamespaceManager
    from nexus.services.permissions.rebac_manager_enhanced import EnhancedReBACManager

logger = logging.getLogger(__name__)

# v1 constraint: maximum TTL for delegations
MAX_TTL_SECONDS = 86400  # 24 hours


class DelegationService:
    """Service for managing agent identity delegation.

    Coordinates between ReBAC, entity registry, namespace manager,
    and API key auth to provision delegated worker agents.
    """

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        rebac_manager: EnhancedReBACManager,
        namespace_manager: NamespaceManager | None = None,
        entity_registry: EntityRegistry | None = None,
        agent_registry: AgentRegistry | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._rebac_manager = rebac_manager
        self._namespace_manager = namespace_manager
        self._entity_registry = entity_registry
        self._agent_registry: AgentRegistry | None = agent_registry
        logger.info("[DelegationService] Initialized")

    def delegate(
        self,
        coordinator_agent_id: str,
        coordinator_owner_id: str,
        worker_id: str,
        worker_name: str,
        delegation_mode: DelegationMode,
        zone_id: str | None = None,
        scope_prefix: str | None = None,
        remove_grants: list[str] | None = None,
        add_grants: list[str] | None = None,
        readonly_paths: list[str] | None = None,
        ttl_seconds: int | None = None,
    ) -> DelegationResult:
        """Create a delegated worker agent with narrowed permissions.

        Args:
            coordinator_agent_id: The coordinator agent creating the delegation.
            coordinator_owner_id: The user who owns the coordinator agent.
            worker_id: Desired ID for the worker agent.
            worker_name: Human-readable name for the worker.
            delegation_mode: How to derive grants (COPY/CLEAN/SHARED).
            zone_id: Zone isolation scope.
            scope_prefix: Optional path prefix filter.
            remove_grants: Paths to exclude (COPY mode).
            add_grants: Paths to include (CLEAN mode).
            readonly_paths: Paths to downgrade to viewer (COPY mode).
            ttl_seconds: Delegation TTL in seconds (max 86400).

        Returns:
            DelegationResult with worker agent ID, API key, and mount table.

        Raises:
            DelegationChainError: If coordinator is itself a delegated agent.
            DelegationError: If coordinator is not registered.
            EscalationError: If grants would exceed parent's permissions.
            TooManyGrantsError: If too many grants derived.
        """
        # 1. Validate coordinator is registered and not delegated
        self._validate_coordinator(coordinator_agent_id)

        # 2. Compute lease expiry
        lease_expires_at = self._compute_lease_expiry(ttl_seconds)

        # 3. Enumerate coordinator's grants
        parent_grants = self._enumerate_parent_grants(
            coordinator_agent_id,
            zone_id,
        )

        # 4. Derive child grants (pure function)
        child_grants = derive_grants(
            parent_grants=parent_grants,
            mode=delegation_mode,
            remove_grants=remove_grants,
            add_grants=add_grants,
            readonly_paths=readonly_paths,
            scope_prefix=scope_prefix,
        )

        logger.info(
            "[Delegation] Derived %d grants for worker=%s from coordinator=%s mode=%s",
            len(child_grants),
            worker_id,
            coordinator_agent_id,
            delegation_mode.value,
        )

        # 5. Register worker agent (UNKNOWN state, no API key yet)
        if self._agent_registry is None:
            raise DelegationError("agent_registry is required for DelegationService")
        self._agent_registry.register(
            agent_id=worker_id,
            owner_id=coordinator_owner_id,
            zone_id=zone_id,
            name=worker_name,
            metadata={"delegated_by": coordinator_agent_id},
        )

        try:
            # 6. Create ReBAC tuples for child grants
            self._create_grant_tuples(
                worker_id=worker_id,
                grants=child_grants,
                zone_id=zone_id,
                coordinator_agent_id=coordinator_agent_id,
                expires_at=lease_expires_at,
            )

            # 7. Persist delegation record
            delegation_id = str(uuid.uuid4())
            self._persist_delegation_record(
                delegation_id=delegation_id,
                agent_id=worker_id,
                parent_agent_id=coordinator_agent_id,
                delegation_mode=delegation_mode,
                scope_prefix=scope_prefix,
                lease_expires_at=lease_expires_at,
                removed_grants=remove_grants,
                added_grants=add_grants,
                readonly_paths=readonly_paths,
                zone_id=zone_id,
            )

            # 8. Create API key (activation step — only after grants exist)
            raw_key = self._create_worker_api_key(
                worker_id=worker_id,
                worker_name=worker_name,
                owner_id=coordinator_owner_id,
                zone_id=zone_id,
                expires_at=lease_expires_at,
            )

            # 9. Get mount table for response
            mount_table = self._get_worker_mount_table(worker_id, zone_id)

        except Exception:
            # Cleanup: unregister agent on failure (no key exists yet)
            self._agent_registry.unregister(worker_id)
            raise

        logger.info(
            "[Delegation] Created delegation=%s worker=%s coordinator=%s mode=%s grants=%d",
            delegation_id,
            worker_id,
            coordinator_agent_id,
            delegation_mode.value,
            len(child_grants),
        )

        return DelegationResult(
            delegation_id=delegation_id,
            worker_agent_id=worker_id,
            api_key=raw_key,
            mount_table=mount_table,
            expires_at=lease_expires_at,
            delegation_mode=delegation_mode,
        )

    def revoke_delegation(self, delegation_id: str) -> bool:
        """Revoke a delegation: delete grants, revoke API key, remove record.

        Args:
            delegation_id: The delegation to revoke.

        Returns:
            True if revoked successfully.

        Raises:
            DelegationNotFoundError: If delegation_id not found.
        """
        record = self._load_delegation_record(delegation_id)
        if record is None:
            raise DelegationNotFoundError(f"Delegation {delegation_id} not found")

        # 1. Delete ReBAC tuples for the worker agent
        self._delete_worker_tuples(record.agent_id, record.zone_id)

        # 2. Revoke API key
        self._revoke_worker_api_key(record.agent_id)

        # 3. Unregister agent entity
        if self._agent_registry is None:
            raise DelegationError("agent_registry is required for DelegationService")
        self._agent_registry.unregister(record.agent_id)

        # 4. Delete delegation record
        self._delete_delegation_record(delegation_id)

        logger.info(
            "[Delegation] Revoked delegation=%s worker=%s",
            delegation_id,
            record.agent_id,
        )
        return True

    def list_delegations(self, parent_agent_id: str) -> list[DelegationRecord]:
        """List all delegations created by a coordinator agent.

        Args:
            parent_agent_id: The coordinator agent ID.

        Returns:
            List of DelegationRecord objects.
        """
        from nexus.storage.models.agents import DelegationRecordModel

        session = self._session_factory()
        try:
            rows = (
                session.query(DelegationRecordModel)
                .filter(DelegationRecordModel.parent_agent_id == parent_agent_id)
                .order_by(DelegationRecordModel.created_at.desc())
                .all()
            )
            return [self._model_to_record(row) for row in rows]
        finally:
            session.close()

    def get_delegation_by_id(self, delegation_id: str) -> DelegationRecord | None:
        """Get delegation record by delegation_id.

        Args:
            delegation_id: The delegation record ID.

        Returns:
            DelegationRecord or None if not found.
        """
        from nexus.storage.models.agents import DelegationRecordModel

        session = self._session_factory()
        try:
            row = (
                session.query(DelegationRecordModel)
                .filter(DelegationRecordModel.delegation_id == delegation_id)
                .first()
            )
            if row is None:
                return None
            return self._model_to_record(row)
        finally:
            session.close()

    def get_delegation(self, agent_id: str) -> DelegationRecord | None:
        """Get delegation record for a worker agent.

        Args:
            agent_id: The worker agent ID.

        Returns:
            DelegationRecord or None if not a delegated agent.
        """
        from nexus.storage.models.agents import DelegationRecordModel

        session = self._session_factory()
        try:
            row = (
                session.query(DelegationRecordModel)
                .filter(DelegationRecordModel.agent_id == agent_id)
                .first()
            )
            if row is None:
                return None
            return self._model_to_record(row)
        finally:
            session.close()

    # -------------------------------------------------------------------------
    # Private helpers
    # -------------------------------------------------------------------------

    def _validate_coordinator(self, coordinator_agent_id: str) -> None:
        """Validate that coordinator is registered and not itself delegated."""
        # Check if coordinator is registered
        if self._entity_registry is not None:
            entity = self._entity_registry.get_entity("agent", coordinator_agent_id)
            if entity is None:
                raise DelegationError(f"Coordinator agent {coordinator_agent_id} is not registered")

        # Check if coordinator is a delegated agent (no chains in v1)
        existing = self.get_delegation(coordinator_agent_id)
        if existing is not None:
            raise DelegationChainError(
                f"Agent {coordinator_agent_id} is a delegated agent and cannot delegate. "
                "Delegation chains are not supported in v1."
            )

    def _compute_lease_expiry(self, ttl_seconds: int | None) -> datetime | None:
        """Compute lease expiry from TTL, enforcing MAX_TTL_SECONDS."""
        if ttl_seconds is None:
            return None
        if ttl_seconds <= 0:
            raise DelegationError(f"TTL must be positive, got {ttl_seconds}")
        if ttl_seconds > MAX_TTL_SECONDS:
            raise DelegationError(f"TTL {ttl_seconds}s exceeds maximum of {MAX_TTL_SECONDS}s (24h)")
        return datetime.now(UTC) + timedelta(seconds=ttl_seconds)

    def _enumerate_parent_grants(
        self,
        agent_id: str,
        zone_id: str | None,
    ) -> list[tuple[str, str]]:
        """Enumerate parent agent's grants as (relation, object_id) tuples.

        Calls rebac_list_objects for "write" and "read" permissions to
        determine the relation for each grant.
        """
        subject = ("agent", agent_id)

        # Fetch limit: slightly above MAX_DELEGATABLE_GRANTS to detect overflow
        fetch_limit = MAX_DELEGATABLE_GRANTS + 1

        # Get write-accessible objects (these have editor/owner relations)
        write_objects = self._rebac_manager.rebac_list_objects(
            subject=subject,
            permission="write",
            object_type="file",
            zone_id=zone_id,
            limit=fetch_limit,
        )
        write_ids = {obj_id for _, obj_id in write_objects}

        # Get read-accessible objects (includes write objects + read-only)
        read_objects = self._rebac_manager.rebac_list_objects(
            subject=subject,
            permission="read",
            object_type="file",
            zone_id=zone_id,
            limit=fetch_limit,
        )

        # Build (relation, object_id) tuples
        result: list[tuple[str, str]] = []
        for _, obj_id in read_objects:
            if obj_id in write_ids:
                result.append(("direct_editor", obj_id))
            else:
                result.append(("direct_viewer", obj_id))

        return result

    def _create_grant_tuples(
        self,
        worker_id: str,
        grants: list[GrantSpec],
        zone_id: str | None,
        coordinator_agent_id: str,
        expires_at: datetime | None,
    ) -> None:
        """Create ReBAC tuples for derived grants."""
        if not grants:
            return

        tuples: list[dict[str, Any]] = []
        for grant in grants:
            tuples.append(
                {
                    "subject": ("agent", worker_id),
                    "relation": grant.relation,
                    "object": (grant.object_type, grant.object_id),
                    "zone_id": zone_id or "default",
                    "expires_at": expires_at,
                    "conditions": json.dumps({"delegated_by": coordinator_agent_id}),
                }
            )

        created = self._rebac_manager.rebac_write_batch(tuples)
        logger.debug(
            "[Delegation] Created %d ReBAC tuples for worker=%s",
            created,
            worker_id,
        )

    def _persist_delegation_record(
        self,
        delegation_id: str,
        agent_id: str,
        parent_agent_id: str,
        delegation_mode: DelegationMode,
        scope_prefix: str | None,
        lease_expires_at: datetime | None,
        removed_grants: list[str] | None,
        added_grants: list[str] | None,
        readonly_paths: list[str] | None,
        zone_id: str | None,
    ) -> None:
        """Persist delegation record to database."""
        from nexus.storage.models.agents import DelegationRecordModel

        session = self._session_factory()
        try:
            model = DelegationRecordModel(
                delegation_id=delegation_id,
                agent_id=agent_id,
                parent_agent_id=parent_agent_id,
                delegation_mode=delegation_mode.value,
                scope_prefix=scope_prefix,
                lease_expires_at=lease_expires_at,
                removed_grants=json.dumps(removed_grants or []),
                added_grants=json.dumps(added_grants or []),
                readonly_paths=json.dumps(readonly_paths or []),
                zone_id=zone_id,
            )
            session.add(model)
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def _create_worker_api_key(
        self,
        worker_id: str,
        worker_name: str,
        owner_id: str,
        zone_id: str | None,
        expires_at: datetime | None,
    ) -> str:
        """Create API key for the worker agent."""
        from nexus.server.auth.database_key import DatabaseAPIKeyAuth

        session = self._session_factory()
        try:
            _key_id, raw_key = DatabaseAPIKeyAuth.create_key(
                session,
                user_id=owner_id,
                name=f"delegation:{worker_name}",
                subject_type="agent",
                subject_id=worker_id,
                zone_id=zone_id,
                expires_at=expires_at,
            )
            session.commit()
            return raw_key
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def _get_worker_mount_table(
        self,
        worker_id: str,
        zone_id: str | None,
    ) -> list[str]:
        """Get mount table for the worker agent."""
        if self._namespace_manager is None:
            return []
        try:
            entries = self._namespace_manager.get_mount_table(
                subject=("agent", worker_id),
                zone_id=zone_id,
            )
            return [entry.virtual_path for entry in entries]
        except Exception as e:
            logger.warning("[Delegation] Failed to get mount table: %s", e)
            return []

    def _delete_worker_tuples(self, worker_id: str, zone_id: str | None) -> None:
        """Delete all ReBAC tuples for a worker agent.

        Delegates to ReBACManager.rebac_delete_by_subject() public API.
        """
        try:
            deleted = self._rebac_manager.rebac_delete_by_subject(
                subject_type="agent",
                subject_id=worker_id,
                zone_id=zone_id,
            )
            logger.debug(
                "[Delegation] Deleted %d ReBAC tuples for worker=%s",
                deleted,
                worker_id,
            )
        except Exception as e:
            logger.warning("[Delegation] Error deleting worker tuples: %s", e)

    def _revoke_worker_api_key(self, worker_id: str) -> None:
        """Revoke all API keys for the worker agent."""
        from nexus.server.auth.database_key import DatabaseAPIKeyAuth
        from nexus.storage.models.auth import APIKeyModel

        session = self._session_factory()
        try:
            # Find API keys for this agent
            keys = (
                session.query(APIKeyModel)
                .filter(
                    APIKeyModel.subject_type == "agent",
                    APIKeyModel.subject_id == worker_id,
                )
                .all()
            )
            for key in keys:
                DatabaseAPIKeyAuth.revoke_key(session, key.key_id)
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def _load_delegation_record(self, delegation_id: str) -> DelegationRecord | None:
        """Load a delegation record by ID."""
        from nexus.storage.models.agents import DelegationRecordModel

        session = self._session_factory()
        try:
            row = (
                session.query(DelegationRecordModel)
                .filter(DelegationRecordModel.delegation_id == delegation_id)
                .first()
            )
            if row is None:
                return None
            return self._model_to_record(row)
        finally:
            session.close()

    def _delete_delegation_record(self, delegation_id: str) -> None:
        """Delete a delegation record."""
        from nexus.storage.models.agents import DelegationRecordModel

        session = self._session_factory()
        try:
            session.query(DelegationRecordModel).filter(
                DelegationRecordModel.delegation_id == delegation_id
            ).delete()
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    @staticmethod
    def _model_to_record(model: Any) -> DelegationRecord:
        """Convert SQLAlchemy model to frozen domain object."""
        return DelegationRecord(
            delegation_id=model.delegation_id,
            agent_id=model.agent_id,
            parent_agent_id=model.parent_agent_id,
            delegation_mode=DelegationMode(model.delegation_mode),
            scope_prefix=model.scope_prefix,
            lease_expires_at=model.lease_expires_at,
            removed_grants=tuple(json.loads(model.removed_grants or "[]")),
            added_grants=tuple(json.loads(model.added_grants or "[]")),
            readonly_paths=tuple(json.loads(model.readonly_paths or "[]")),
            zone_id=model.zone_id,
            created_at=model.created_at,
        )
