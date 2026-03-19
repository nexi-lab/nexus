"""Delegation service — core business logic (Issue #1271, #1618).

Orchestrates agent identity delegation: coordinator agents can
provision worker agents with narrower permissions.

Uses ordered steps for safety without requiring cross-system transactions:
    1. Register worker agent (harmless, no permissions)
    2. Create ReBAC grants (permissions materialized)
    3. Create API key (activation — only after grants exist)
    4. Persist DelegationRecord (audit trail)

On failure at step 2+: unregister agent (no key exists, safe to retry).

#1618 additions:
    - DelegationStatus lifecycle (ACTIVE -> REVOKED/EXPIRED/COMPLETED)
    - Soft-delete revocation (status=REVOKED first, then cleanup)
    - Fail-loud on grant deletion during revocation
    - Session context manager (DRY)
    - Pagination for list_delegations
    - Intent, depth, can_sub_delegate, parent_delegation_id fields
    - DelegationScope serialization
"""

import json
import logging
import uuid
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from nexus.bricks.delegation.derivation import MAX_DELEGATABLE_GRANTS, GrantSpec, derive_grants
from nexus.bricks.delegation.errors import (
    DelegationChainError,
    DelegationError,
    DelegationNotFoundError,
    DepthExceededError,
)
from nexus.bricks.delegation.models import (
    DelegationMode,
    DelegationOutcome,
    DelegationRecord,
    DelegationResult,
    DelegationScope,
    DelegationStatus,
)
from nexus.contracts.constants import ROOT_ZONE_ID

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from nexus.contracts.protocols.entity_registry import EntityRegistryProtocol
    from nexus.storage.record_store import RecordStoreABC

logger = logging.getLogger(__name__)

# Maximum TTL for delegations
MAX_TTL_SECONDS = 86400  # 24 hours

# Maximum sub-delegation depth
MAX_CHAIN_DEPTH = 5


class DelegationService:
    """Service for managing agent identity delegation.

    Coordinates between ReBAC, entity registry, namespace manager,
    and API key auth to provision delegated worker agents.
    """

    def __init__(
        self,
        record_store: "RecordStoreABC",
        rebac_manager: Any,
        namespace_manager: Any = None,
        entity_registry: "EntityRegistryProtocol | None" = None,
        process_table: Any = None,
    ) -> None:
        self._session_factory = record_store.session_factory
        self._rebac_manager = rebac_manager
        self._namespace_manager = namespace_manager
        self._entity_registry = entity_registry
        self._process_table: Any = process_table
        logger.info("[DelegationService] Initialized")

    @contextmanager
    def _session(self, *, commit: bool = True) -> Generator[Any, None, None]:
        """Context manager for session lifecycle.

        Eliminates duplicated try/except/rollback/finally across methods.
        Set commit=False for read-only operations.
        """
        session: Session = self._session_factory()
        try:
            yield session
            if commit:
                session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

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
        intent: str = "",
        can_sub_delegate: bool = False,
        scope: DelegationScope | None = None,
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
            intent: Immutable purpose description for audit trail.
            can_sub_delegate: Whether worker can create further delegations.
            scope: Fine-grained operation/resource/budget constraints.

        Returns:
            DelegationResult with worker agent ID, API key, and mount table.

        Raises:
            DelegationChainError: If coordinator cannot sub-delegate.
            DepthExceededError: If chain depth exceeds max_depth.
            DelegationError: If coordinator is not registered.
            EscalationError: If grants would exceed parent's permissions.
            TooManyGrantsError: If too many grants derived.
        """
        # 1. Validate coordinator is registered and can delegate
        parent_delegation = self._validate_coordinator(coordinator_agent_id)

        # 2. Compute chain depth
        depth = 0
        parent_delegation_id: str | None = None
        if parent_delegation is not None:
            depth = parent_delegation.depth + 1
            parent_delegation_id = parent_delegation.delegation_id
            # Enforce max depth from parent's scope
            parent_max_depth = MAX_CHAIN_DEPTH
            if parent_delegation.scope is not None:
                parent_max_depth = parent_delegation.scope.max_depth
            if depth > parent_max_depth:
                raise DepthExceededError(
                    f"Delegation depth {depth} exceeds max_depth {parent_max_depth}"
                )

        # 3. Compute lease expiry
        lease_expires_at = self._compute_lease_expiry(ttl_seconds)

        # 4. Enumerate coordinator's grants (single query, Issue 13A)
        parent_grants = self._enumerate_parent_grants(coordinator_agent_id, zone_id)

        # 5. Derive child grants (pure function)
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

        # 6. Register worker agent via ProcessTable
        if self._process_table is None:
            raise DelegationError("process_table is required for DelegationService")
        self._process_table.register_external(
            worker_name,
            coordinator_owner_id,
            zone_id or ROOT_ZONE_ID,
            connection_id=worker_id,
            labels={"delegated_by": coordinator_agent_id},
        )

        try:
            # 7. Create ReBAC tuples for child grants
            self._create_grant_tuples(
                worker_id=worker_id,
                grants=child_grants,
                zone_id=zone_id,
                coordinator_agent_id=coordinator_agent_id,
                expires_at=lease_expires_at,
            )

            # 8. Persist delegation record with status=ACTIVE
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
                intent=intent,
                parent_delegation_id=parent_delegation_id,
                depth=depth,
                can_sub_delegate=can_sub_delegate,
                scope=scope,
            )

            # 9. Create API key (activation step — only after grants exist)
            raw_key = self._create_worker_api_key(
                worker_id=worker_id,
                worker_name=worker_name,
                owner_id=coordinator_owner_id,
                zone_id=zone_id,
                expires_at=lease_expires_at,
            )

            # 10. Get mount table for response
            mount_table = self._get_worker_mount_table(worker_id, zone_id)

        except Exception:
            # Cleanup: unregister agent on failure (no key exists yet)
            self._process_table.unregister_external(worker_id)
            raise

        logger.info(
            "[Delegation] Created delegation=%s worker=%s coordinator=%s mode=%s grants=%d depth=%d",
            delegation_id,
            worker_id,
            coordinator_agent_id,
            delegation_mode.value,
            len(child_grants),
            depth,
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
        """Revoke a delegation using soft-delete-first pattern (Issue 8A).

        Steps:
            0. Set status=REVOKED (contract with callers -- immediate)
            1. Delete ReBAC tuples (fail-loud, Issue 7A)
            2. Revoke API key
            3. Unregister agent
        The record persists with status=REVOKED for audit trail.

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

        if record.status != DelegationStatus.ACTIVE:
            raise DelegationError(
                f"Delegation {delegation_id} is not active (status={record.status.value})"
            )

        # Step 0: Soft-delete -- mark as REVOKED immediately
        self._update_delegation_status(delegation_id, DelegationStatus.REVOKED)

        # Step 1: Delete ReBAC tuples -- fail-loud (Issue 7A)
        self._delete_worker_tuples(record.agent_id, record.zone_id)

        # Step 2: Revoke API key
        self._revoke_worker_api_key(record.agent_id)

        # Step 3: Unregister agent entity
        if self._process_table is None:
            raise DelegationError("process_table is required for DelegationService")
        self._process_table.unregister_external(record.agent_id)

        logger.info(
            "[Delegation] Revoked delegation=%s worker=%s",
            delegation_id,
            record.agent_id,
        )
        return True

    def list_delegations(
        self,
        parent_agent_id: str,
        *,
        limit: int = 50,
        offset: int = 0,
        status_filter: DelegationStatus | None = None,
    ) -> tuple[list[DelegationRecord], int]:
        """List delegations created by a coordinator agent with pagination.

        Args:
            parent_agent_id: The coordinator agent ID.
            limit: Maximum records to return (default 50).
            offset: Number of records to skip (default 0).
            status_filter: Optional filter by status (default: all statuses).

        Returns:
            Tuple of (records, total_count).
        """
        from sqlalchemy import func, select

        from nexus.storage.models.agents import DelegationRecordModel

        with self._session(commit=False) as session:
            stmt = select(DelegationRecordModel).where(
                DelegationRecordModel.parent_agent_id == parent_agent_id
            )
            if status_filter is not None:
                stmt = stmt.where(DelegationRecordModel.status == status_filter.value)

            count_stmt = select(func.count()).select_from(stmt.subquery())
            total = session.execute(count_stmt).scalar() or 0
            rows = (
                session.execute(
                    stmt.order_by(DelegationRecordModel.created_at.desc())
                    .offset(offset)
                    .limit(limit)
                )
                .scalars()
                .all()
            )
            return [self._model_to_record(row) for row in rows], total

    def get_delegation_by_id(self, delegation_id: str) -> DelegationRecord | None:
        """Get delegation record by delegation_id."""
        from sqlalchemy import select

        from nexus.storage.models.agents import DelegationRecordModel

        with self._session(commit=False) as session:
            row = (
                session.execute(
                    select(DelegationRecordModel).where(
                        DelegationRecordModel.delegation_id == delegation_id
                    )
                )
                .scalars()
                .first()
            )
            if row is None:
                return None
            return self._model_to_record(row)

    def get_delegation(self, agent_id: str) -> DelegationRecord | None:
        """Get active delegation record for a worker agent."""
        from sqlalchemy import select

        from nexus.storage.models.agents import DelegationRecordModel

        with self._session(commit=False) as session:
            row = (
                session.execute(
                    select(DelegationRecordModel).where(
                        DelegationRecordModel.agent_id == agent_id,
                        DelegationRecordModel.status == DelegationStatus.ACTIVE.value,
                    )
                )
                .scalars()
                .first()
            )
            if row is None:
                return None
            return self._model_to_record(row)

    def get_delegation_chain(self, delegation_id: str) -> list[DelegationRecord]:
        """Trace delegation chain from child to root.

        Args:
            delegation_id: Starting delegation ID.

        Returns:
            List of DelegationRecord from child to root.
        """
        chain: list[DelegationRecord] = []
        current_id: str | None = delegation_id
        seen: set[str] = set()

        while current_id is not None and current_id not in seen:
            seen.add(current_id)
            record = self.get_delegation_by_id(current_id)
            if record is None:
                break
            chain.append(record)
            current_id = record.parent_delegation_id

        return chain

    def complete_delegation(
        self,
        delegation_id: str,
        outcome: DelegationOutcome,  # noqa: ARG002 — API contract (callers pass this)
        quality_score: float | None = None,
    ) -> DelegationRecord:
        """Complete a delegation (#1619).

        Args:
            delegation_id: The delegation to complete.
            outcome: How the delegation ended (COMPLETED/FAILED/TIMEOUT).
            quality_score: Optional quality rating (0.0-1.0) for COMPLETED outcome.

        Returns:
            Updated DelegationRecord.

        Raises:
            DelegationNotFoundError: If delegation_id not found.
            DelegationError: If delegation is not ACTIVE or quality_score out of range.
        """
        if quality_score is not None and not (0.0 <= quality_score <= 1.0):
            raise DelegationError(f"quality_score must be between 0.0 and 1.0, got {quality_score}")

        record = self._load_delegation_record(delegation_id)
        if record is None:
            raise DelegationNotFoundError(f"Delegation {delegation_id} not found")

        if record.status != DelegationStatus.ACTIVE:
            raise DelegationError(
                f"Delegation {delegation_id} is not active (status={record.status.value})"
            )

        # Update status to COMPLETED
        self._update_delegation_status(delegation_id, DelegationStatus.COMPLETED)

        # Return updated record
        updated = self._load_delegation_record(delegation_id)
        if updated is None:
            raise DelegationNotFoundError(f"Delegation {delegation_id} not found after update")
        return updated

    # Sentinel for "clear this field" vs "leave unchanged"
    _CLEAR: str = "__CLEAR__"

    def update_namespace_config(
        self,
        delegation_id: str,
        *,
        scope_prefix: str | None = None,
        clear_scope_prefix: bool = False,
        remove_grants: list[str] | None = None,
        add_grants: list[str] | None = None,
        readonly_paths: list[str] | None = None,
    ) -> DelegationRecord:
        """Update namespace config and re-materialize effective access.

        Updates the record, re-derives grants from the parent's current
        permissions, replaces the worker's ReBAC tuples, and invalidates
        the namespace cache so the mount_table reflects the new state.

        Args:
            delegation_id: The delegation to update.
            scope_prefix: New scope prefix (None = leave unchanged).
            clear_scope_prefix: Set True to clear scope_prefix to None.
            remove_grants: New removed grants list (None = leave unchanged).
            add_grants: New added grants list (None = leave unchanged).
            readonly_paths: New readonly paths list (None = leave unchanged).

        Returns:
            Updated DelegationRecord.

        Raises:
            DelegationNotFoundError: If delegation_id not found.
            DelegationError: If delegation is not ACTIVE.
            InvalidPrefixError: If scope_prefix is empty or malformed.
            EscalationError: If add_grants exceed parent's permissions.
        """
        from nexus.bricks.delegation.derivation import validate_scope_prefix

        record = self._load_delegation_record(delegation_id)
        if record is None:
            raise DelegationNotFoundError(f"Delegation {delegation_id} not found")

        if record.status != DelegationStatus.ACTIVE:
            raise DelegationError(
                f"Delegation {delegation_id} is not active (status={record.status.value})"
            )

        # Compute effective new values (merge unchanged fields from record)
        new_scope_prefix: str | None
        if clear_scope_prefix:
            new_scope_prefix = None
        elif scope_prefix is not None:
            new_scope_prefix = scope_prefix
        else:
            new_scope_prefix = record.scope_prefix

        new_remove = (
            list(remove_grants) if remove_grants is not None else list(record.removed_grants)
        )
        new_add = list(add_grants) if add_grants is not None else list(record.added_grants)
        new_readonly = (
            list(readonly_paths) if readonly_paths is not None else list(record.readonly_paths)
        )

        # Validate scope_prefix at system boundary
        validate_scope_prefix(new_scope_prefix)

        # 1. Re-derive grants from parent's current permissions (pure, no side effects)
        parent_grants = self._enumerate_parent_grants(record.parent_agent_id, record.zone_id)
        child_grants = derive_grants(
            parent_grants=parent_grants,
            mode=record.delegation_mode,
            remove_grants=new_remove if new_remove else None,
            add_grants=new_add if new_add else None,
            readonly_paths=new_readonly if new_readonly else None,
            scope_prefix=new_scope_prefix,
        )

        # 2. Snapshot existing tuples for compensation on failure
        old_tuples = self._rebac_manager.list_tuples(
            subject=("agent", record.agent_id),
        )

        # 3. Update DB record first (auto-rollback via session on exception)
        from sqlalchemy import update as sa_update

        from nexus.storage.models.agents import DelegationRecordModel

        old_record_values = {
            "scope_prefix": record.scope_prefix,
            "removed_grants": json.dumps(list(record.removed_grants)),
            "added_grants": json.dumps(list(record.added_grants)),
            "readonly_paths": json.dumps(list(record.readonly_paths)),
        }

        with self._session() as session:
            session.execute(
                sa_update(DelegationRecordModel)
                .where(DelegationRecordModel.delegation_id == delegation_id)
                .values(
                    scope_prefix=new_scope_prefix,
                    removed_grants=json.dumps(new_remove),
                    added_grants=json.dumps(new_add),
                    readonly_paths=json.dumps(new_readonly),
                )
            )

        # 4. Replace tuples with compensation on failure
        try:
            self._delete_worker_tuples(record.agent_id, record.zone_id)
            self._create_grant_tuples(
                worker_id=record.agent_id,
                grants=child_grants,
                zone_id=record.zone_id,
                coordinator_agent_id=record.parent_agent_id,
                expires_at=record.lease_expires_at,
            )
        except Exception:
            logger.error(
                "[Delegation] Tuple replacement failed for delegation=%s, "
                "compensating: restoring DB record + old tuples",
                delegation_id,
            )
            # Compensate: rollback DB record to previous values
            with self._session() as session:
                session.execute(
                    sa_update(DelegationRecordModel)
                    .where(DelegationRecordModel.delegation_id == delegation_id)
                    .values(**old_record_values)
                )
            # Compensate: restore old tuples from snapshot
            self._restore_tuples_from_snapshot(old_tuples)
            raise

        # 5. Invalidate namespace cache so mount_table reflects new state
        if self._namespace_manager is not None:
            self._namespace_manager.invalidate(("agent", record.agent_id))

        logger.info(
            "[Delegation] Updated namespace config for delegation=%s grants=%d",
            delegation_id,
            len(child_grants),
        )

        updated = self._load_delegation_record(delegation_id)
        if updated is None:
            raise DelegationNotFoundError(f"Delegation {delegation_id} not found after update")
        return updated

    # -------------------------------------------------------------------------
    # Private helpers
    # -------------------------------------------------------------------------

    def _validate_coordinator(self, coordinator_agent_id: str) -> DelegationRecord | None:
        """Validate that coordinator is registered and can delegate.

        Returns the coordinator's own delegation record if it is a delegated
        agent (for chain depth tracking), or None if it's a root agent.

        Raises:
            DelegationError: If coordinator is not registered.
            DelegationChainError: If coordinator is delegated but cannot sub-delegate.
        """
        # Check if coordinator is registered
        if self._entity_registry is not None:
            entity = self._entity_registry.get_entity("agent", coordinator_agent_id)
            if entity is None:
                raise DelegationError(f"Coordinator agent {coordinator_agent_id} is not registered")

        # Check if coordinator is a delegated agent
        existing = self.get_delegation(coordinator_agent_id)
        if existing is not None:
            # #1618: allow sub-delegation if can_sub_delegate is True
            if not existing.can_sub_delegate:
                raise DelegationChainError(
                    f"Agent {coordinator_agent_id} is a delegated agent and "
                    "cannot sub-delegate (can_sub_delegate=False)."
                )
            return existing

        return None

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

        Issue 13A: Uses single read query + write query to classify.
        """
        subject = ("agent", agent_id)
        fetch_limit = MAX_DELEGATABLE_GRANTS + 1

        # Get write-accessible objects (editor/owner relations)
        write_objects = self._rebac_manager.rebac_list_objects(
            subject=subject,
            permission="write",
            object_type="file",
            zone_id=zone_id,
            limit=fetch_limit,
        )
        write_ids = {obj_id for _, obj_id in write_objects}

        # Get read-accessible objects (includes write + read-only)
        read_objects = self._rebac_manager.rebac_list_objects(
            subject=subject,
            permission="read",
            object_type="file",
            zone_id=zone_id,
            limit=fetch_limit,
        )

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
                    "zone_id": zone_id or ROOT_ZONE_ID,
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
        intent: str = "",
        parent_delegation_id: str | None = None,
        depth: int = 0,
        can_sub_delegate: bool = False,
        scope: DelegationScope | None = None,
    ) -> None:
        """Persist delegation record to database."""
        from nexus.storage.models.agents import DelegationRecordModel

        scope_json = None
        if scope is not None:
            scope_json = json.dumps(
                {
                    "allowed_operations": sorted(scope.allowed_operations),
                    "resource_patterns": sorted(scope.resource_patterns),
                    "budget_limit": str(scope.budget_limit)
                    if scope.budget_limit is not None
                    else None,
                    "max_depth": scope.max_depth,
                }
            )

        with self._session() as session:
            model = DelegationRecordModel(
                delegation_id=delegation_id,
                agent_id=agent_id,
                parent_agent_id=parent_agent_id,
                delegation_mode=delegation_mode.value,
                status=DelegationStatus.ACTIVE.value,
                scope_prefix=scope_prefix,
                scope=scope_json,
                lease_expires_at=lease_expires_at,
                removed_grants=json.dumps(removed_grants or []),
                added_grants=json.dumps(added_grants or []),
                readonly_paths=json.dumps(readonly_paths or []),
                zone_id=zone_id,
                intent=intent,
                parent_delegation_id=parent_delegation_id,
                depth=depth,
                can_sub_delegate=can_sub_delegate,
            )
            session.add(model)

    def _create_worker_api_key(
        self,
        worker_id: str,
        worker_name: str,
        owner_id: str,
        zone_id: str | None,
        expires_at: datetime | None,
    ) -> str:
        """Create API key for the worker agent."""
        from nexus.storage.api_key_ops import create_api_key

        with self._session() as session:
            _key_id, raw_key = create_api_key(
                session,
                user_id=owner_id,
                name=f"delegation:{worker_name}",
                subject_type="agent",
                subject_id=worker_id,
                zone_id=zone_id,
                expires_at=expires_at,
            )
            return str(raw_key)

    def _get_worker_mount_table(
        self,
        worker_id: str,
        zone_id: str | None,
    ) -> list[str]:
        """Get mount table for the worker agent (fail-soft -- informational)."""
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

        Fail-loud (Issue 7A): propagates exceptions on revocation path.
        """
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

    @staticmethod
    def _normalize_expires_at(raw: Any) -> datetime | None:
        """Normalize expires_at from DB row to datetime.

        list_tuples() returns the raw DB value which may be a datetime
        object or an ISO-format string depending on the driver.
        write_batch() calls .isoformat() unconditionally, so we must
        ensure a datetime is returned.
        """
        if raw is None:
            return None
        if isinstance(raw, datetime):
            return raw
        # String-backed timestamp from SQLite/Postgres text column
        if isinstance(raw, str):
            return datetime.fromisoformat(raw)
        return None

    @staticmethod
    def _normalize_conditions(raw: Any) -> Any:
        """Normalize conditions from DB row for write_batch().

        list_tuples() returns the raw DB value (a JSON string).
        write_batch() calls json.dumps() on it, so we must parse
        the string back to a Python object to avoid double-encoding.
        """
        if raw is None:
            return None
        if isinstance(raw, str):
            return json.loads(raw)
        # Already a dict/list — pass through
        return raw

    def _restore_tuples_from_snapshot(self, snapshot: list[dict[str, Any]]) -> None:
        """Restore ReBAC tuples from a previous snapshot (compensating transaction).

        Converts list_tuples() output back to write_batch() input format,
        preserving all fields (subject_relation, conditions, zone IDs,
        expires_at) so the restored tuples are identical to the originals.
        Best-effort: logs errors but does not raise, since this is itself
        a compensation path.
        """
        if not snapshot:
            return

        batch: list[dict[str, Any]] = []
        for t in snapshot:
            # Reconstruct subject tuple, including subject_relation if present
            subject_relation = t.get("subject_relation")
            if subject_relation:
                subject: tuple[str, ...] = (
                    t["subject_type"],
                    t["subject_id"],
                    subject_relation,
                )
            else:
                subject = (t["subject_type"], t["subject_id"])

            batch.append(
                {
                    "subject": subject,
                    "relation": t["relation"],
                    "object": (t["object_type"], t["object_id"]),
                    "zone_id": t.get("zone_id"),
                    "expires_at": self._normalize_expires_at(t.get("expires_at")),
                    "conditions": self._normalize_conditions(t.get("conditions")),
                    "subject_zone_id": t.get("subject_zone_id"),
                    "object_zone_id": t.get("object_zone_id"),
                }
            )

        try:
            restored = self._rebac_manager.rebac_write_batch(batch)
            logger.info(
                "[Delegation] Compensating restore: recreated %d/%d tuples",
                restored,
                len(batch),
            )
        except Exception:
            logger.exception(
                "[Delegation] CRITICAL: Failed to restore %d tuples during compensation",
                len(batch),
            )

    def _revoke_worker_api_key(self, worker_id: str) -> None:
        """Revoke all API keys for the worker agent."""
        from sqlalchemy import select

        from nexus.storage.api_key_ops import revoke_api_key
        from nexus.storage.models.auth import APIKeyModel

        with self._session() as session:
            keys = (
                session.execute(
                    select(APIKeyModel).where(
                        APIKeyModel.subject_type == "agent",
                        APIKeyModel.subject_id == worker_id,
                    )
                )
                .scalars()
                .all()
            )
            for key in keys:
                revoke_api_key(session, key.key_id)

    def _update_delegation_status(self, delegation_id: str, status: DelegationStatus) -> None:
        """Update delegation record status (soft-delete pattern, Issue 8A)."""
        from sqlalchemy import update

        from nexus.storage.models.agents import DelegationRecordModel

        with self._session() as session:
            result = session.execute(
                update(DelegationRecordModel)
                .where(DelegationRecordModel.delegation_id == delegation_id)
                .values(status=status.value)
            )
            rows_updated = result.rowcount
            if rows_updated == 0:
                raise DelegationNotFoundError(f"Delegation {delegation_id} not found")

    def _load_delegation_record(self, delegation_id: str) -> DelegationRecord | None:
        """Load a delegation record by ID."""
        from sqlalchemy import select

        from nexus.storage.models.agents import DelegationRecordModel

        with self._session(commit=False) as session:
            row = (
                session.execute(
                    select(DelegationRecordModel).where(
                        DelegationRecordModel.delegation_id == delegation_id
                    )
                )
                .scalars()
                .first()
            )
            if row is None:
                return None
            return self._model_to_record(row)

    @staticmethod
    def _model_to_record(model: Any) -> DelegationRecord:
        """Convert SQLAlchemy model to frozen domain object."""
        from decimal import Decimal

        # Deserialize scope JSON if present
        scope = None
        scope_raw = getattr(model, "scope", None)
        if scope_raw:
            scope_data = json.loads(scope_raw)
            budget_str = scope_data.get("budget_limit")
            scope = DelegationScope(
                allowed_operations=frozenset(scope_data.get("allowed_operations", [])),
                resource_patterns=frozenset(scope_data.get("resource_patterns", [])),
                budget_limit=Decimal(budget_str) if budget_str is not None else None,
                max_depth=scope_data.get("max_depth", 0),
            )

        # Handle status with backward compat
        status_val = getattr(model, "status", "active")
        try:
            status = DelegationStatus(status_val)
        except ValueError:
            status = DelegationStatus.ACTIVE

        return DelegationRecord(
            delegation_id=model.delegation_id,
            agent_id=model.agent_id,
            parent_agent_id=model.parent_agent_id,
            delegation_mode=DelegationMode(model.delegation_mode),
            status=status,
            scope_prefix=model.scope_prefix,
            scope=scope,
            lease_expires_at=model.lease_expires_at,
            removed_grants=tuple(json.loads(model.removed_grants or "[]")),
            added_grants=tuple(json.loads(model.added_grants or "[]")),
            readonly_paths=tuple(json.loads(model.readonly_paths or "[]")),
            zone_id=model.zone_id,
            intent=getattr(model, "intent", ""),
            parent_delegation_id=getattr(model, "parent_delegation_id", None),
            depth=getattr(model, "depth", 0),
            can_sub_delegate=bool(getattr(model, "can_sub_delegate", False)),
            created_at=model.created_at,
        )
