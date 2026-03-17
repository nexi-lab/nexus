"""Agent Delegation API endpoints (Issue #1271, #1618).

Provides endpoints for coordinator-initiated agent delegation:
- POST   /api/v2/agents/delegate              — Delegate agent identity
- DELETE /api/v2/agents/delegate/{id}          — Revoke delegation
- GET    /api/v2/agents/delegate               — List coordinator's delegations
- GET    /api/v2/agents/delegate/{id}/chain    — Trace delegation chain
- POST   /api/v2/agents/delegate/{id}/complete — Complete delegation with feedback
- GET    /api/v2/agents/delegate/{id}/namespace — Namespace detail
- PATCH  /api/v2/agents/delegate/{id}/namespace — Update namespace config
- GET    /api/v2/agents/delegate/{id}          — Single delegation detail
"""

import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from nexus.bricks.delegation.service import MAX_TTL_SECONDS as _MAX_TTL

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v2/agents/delegate", tags=["delegation"])

# =============================================================================
# Lazy imports (avoid circular imports with fastapi_server)
# =============================================================================


def _get_require_auth() -> Any:
    from nexus.server.dependencies import require_auth

    return require_auth


def _get_delegation_service(request: Request) -> Any:
    """Lazily construct DelegationService from app state.

    All dependencies come from ``app.state`` which is populated during
    server startup (``fastapi_server.py`` + lifespan helpers).  No
    NexusFS private attribute access (Issue #701).
    """
    state = request.app.state
    cached = getattr(state, "_delegation_service", None)
    if cached is not None:
        return cached

    record_store = getattr(state, "record_store", None)
    if record_store is None:
        raise HTTPException(status_code=503, detail="RecordStore not available")

    rebac_manager = getattr(state, "rebac_manager", None)
    if rebac_manager is None:
        raise HTTPException(status_code=503, detail="ReBAC manager not available")

    from nexus.bricks.delegation.service import DelegationService

    service = DelegationService(
        record_store=record_store,
        rebac_manager=rebac_manager,
        namespace_manager=getattr(state, "namespace_manager", None),
        entity_registry=getattr(state, "entity_registry", None),
        process_table=getattr(state, "process_table", None),
    )
    state._delegation_service = service
    return service


# =============================================================================
# Pydantic models
# =============================================================================


class DelegationScopeModel(BaseModel):
    """Fine-grained scope constraints for a delegation."""

    allowed_operations: list[str] = Field(default_factory=list, description="Permitted operations")
    resource_patterns: list[str] = Field(
        default_factory=list, description="Glob patterns for resources"
    )
    budget_limit: str | None = Field(
        default=None, description="Max spend in credits (decimal string)"
    )
    max_depth: int = Field(
        default=0, description="Max sub-delegation depth (0 = no sub-delegation)"
    )


class DelegateRequest(BaseModel):
    """Request to create a delegated worker agent."""

    worker_id: str = Field(..., description="Unique ID for the worker agent")
    worker_name: str = Field(..., description="Human-readable name for the worker")
    namespace_mode: str = Field(..., description="Delegation mode: 'copy', 'clean', or 'shared'")
    remove_grants: list[str] | None = Field(
        default=None, description="Paths to exclude (copy mode)"
    )
    add_grants: list[str] | None = Field(
        default=None, description="Paths to include (clean mode, must be subset of parent)"
    )
    readonly_paths: list[str] | None = Field(
        default=None, description="Paths to downgrade to read-only (copy mode)"
    )
    scope_prefix: str | None = Field(default=None, description="Path prefix filter for grants")
    ttl_seconds: int | None = Field(
        default=None,
        gt=0,
        le=_MAX_TTL,
        description="Delegation TTL in seconds (max 86400 = 24h)",
    )
    intent: str = Field(default="", description="Immutable purpose description for audit")
    can_sub_delegate: bool = Field(
        default=False, description="Allow worker to create sub-delegations"
    )
    scope: DelegationScopeModel | None = Field(
        default=None, description="Fine-grained scope constraints"
    )


class DelegateResponse(BaseModel):
    """Response after creating a delegation."""

    delegation_id: str
    worker_agent_id: str
    api_key: str
    mount_table: list[str]
    expires_at: datetime | None
    delegation_mode: str


class DelegationListItem(BaseModel):
    """Summary of a delegation for list responses."""

    delegation_id: str
    agent_id: str
    parent_agent_id: str
    delegation_mode: str
    status: str
    scope_prefix: str | None
    lease_expires_at: datetime | None
    zone_id: str | None
    intent: str
    depth: int
    can_sub_delegate: bool
    created_at: datetime


class DelegationListResponse(BaseModel):
    """Response for listing delegations with pagination."""

    delegations: list[DelegationListItem]
    total: int
    limit: int
    offset: int


class DelegationChainItem(BaseModel):
    """Single node in a delegation chain."""

    delegation_id: str
    agent_id: str
    parent_agent_id: str
    delegation_mode: str
    status: str
    depth: int
    intent: str
    created_at: datetime


class DelegationChainResponse(BaseModel):
    """Response for delegation chain tracing."""

    chain: list[DelegationChainItem]
    total_depth: int


class CompleteDelegationRequest(BaseModel):
    """Request to complete a delegation with outcome feedback (#1619)."""

    outcome: str = Field(description="Delegation outcome: 'completed', 'failed', or 'timeout'")
    quality_score: float | None = Field(
        default=None, ge=0.0, le=1.0, description="Optional quality rating (0.0-1.0)"
    )


class DelegationDetailResponse(BaseModel):
    """Full delegation detail response."""

    delegation_id: str
    agent_id: str
    parent_agent_id: str
    delegation_mode: str
    status: str
    scope_prefix: str | None
    lease_expires_at: datetime | None
    zone_id: str | None
    intent: str
    depth: int
    can_sub_delegate: bool
    created_at: datetime
    removed_grants: list[str]
    added_grants: list[str]
    readonly_paths: list[str]


# =============================================================================
# Endpoints
# =============================================================================


@router.post("", response_model=DelegateResponse)
async def create_delegation(
    body: DelegateRequest,
    auth_result: dict[str, Any] = Depends(_get_require_auth()),
    service: Any = Depends(_get_delegation_service),
) -> DelegateResponse:
    """Create a delegated worker agent with narrowed permissions.

    The caller must be an agent (not a user, not a delegated agent unless
    can_sub_delegate=True on its own delegation).
    """
    # Validate caller is an agent
    subject_type = auth_result.get("subject_type", "")
    if subject_type != "agent":
        raise HTTPException(
            status_code=403,
            detail="Only agents can delegate. Caller subject_type must be 'agent'.",
        )

    coordinator_agent_id = auth_result.get("subject_id", "")
    coordinator_owner_id = auth_result.get("user_id") or auth_result.get("metadata", {}).get(
        "user_id", ""
    )
    zone_id = auth_result.get("zone_id")

    # Validate namespace_mode
    from nexus.bricks.delegation.models import DelegationMode, DelegationScope

    try:
        mode = DelegationMode(body.namespace_mode)
    except ValueError as e:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid namespace_mode: {body.namespace_mode!r}. "
            "Must be 'copy', 'clean', or 'shared'.",
        ) from e

    # Convert Pydantic scope model to domain object
    scope: DelegationScope | None = None
    if body.scope is not None:
        from decimal import Decimal

        scope = DelegationScope(
            allowed_operations=frozenset(body.scope.allowed_operations),
            resource_patterns=frozenset(body.scope.resource_patterns),
            budget_limit=Decimal(body.scope.budget_limit) if body.scope.budget_limit else None,
            max_depth=body.scope.max_depth,
        )

    try:
        result = service.delegate(
            coordinator_agent_id=coordinator_agent_id,
            coordinator_owner_id=coordinator_owner_id,
            worker_id=body.worker_id,
            worker_name=body.worker_name,
            delegation_mode=mode,
            zone_id=zone_id,
            scope_prefix=body.scope_prefix,
            remove_grants=body.remove_grants,
            add_grants=body.add_grants,
            readonly_paths=body.readonly_paths,
            ttl_seconds=body.ttl_seconds,
            intent=body.intent,
            can_sub_delegate=body.can_sub_delegate,
            scope=scope,
        )
    except Exception as e:
        _handle_delegation_error(e)
        raise  # unreachable, but satisfies type checker

    return DelegateResponse(
        delegation_id=result.delegation_id,
        worker_agent_id=result.worker_agent_id,
        api_key=result.api_key,
        mount_table=result.mount_table,
        expires_at=result.expires_at,
        delegation_mode=result.delegation_mode.value,
    )


@router.delete("/{delegation_id}")
async def revoke_delegation(
    delegation_id: str,
    auth_result: dict[str, Any] = Depends(_get_require_auth()),
    service: Any = Depends(_get_delegation_service),
) -> dict[str, Any]:
    """Revoke a delegation, removing all worker grants and API keys."""
    subject_type = auth_result.get("subject_type", "")
    if subject_type != "agent":
        raise HTTPException(
            status_code=403,
            detail="Only agents can revoke delegations.",
        )

    # Ownership check: only the parent agent can revoke its delegation
    record = service.get_delegation_by_id(delegation_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"Delegation {delegation_id} not found.")

    agent_id = auth_result.get("subject_id", "")
    if record.parent_agent_id != agent_id:
        raise HTTPException(
            status_code=403,
            detail="Only the parent agent can revoke a delegation.",
        )

    try:
        service.revoke_delegation(delegation_id)
    except Exception as e:
        _handle_delegation_error(e)
        raise  # unreachable, but satisfies type checker

    return {"status": "revoked", "delegation_id": delegation_id}


@router.get("", response_model=DelegationListResponse)
async def list_delegations(
    auth_result: dict[str, Any] = Depends(_get_require_auth()),
    service: Any = Depends(_get_delegation_service),
    limit: int = Query(default=50, ge=1, le=200, description="Max records to return"),
    offset: int = Query(default=0, ge=0, description="Records to skip"),
    status: str | None = Query(
        default=None, description="Filter by status (active/revoked/expired/completed)"
    ),
) -> DelegationListResponse:
    """List delegations created by the calling agent with pagination."""
    subject_type = auth_result.get("subject_type", "")
    if subject_type != "agent":
        raise HTTPException(
            status_code=403,
            detail="Only agents can list delegations.",
        )

    coordinator_agent_id = auth_result.get("subject_id", "")

    # Parse optional status filter
    from nexus.bricks.delegation.models import DelegationStatus

    status_filter: DelegationStatus | None = None
    if status is not None:
        try:
            status_filter = DelegationStatus(status)
        except ValueError as e:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid status: {status!r}. Must be one of: active, revoked, expired, completed.",
            ) from e

    records, total = service.list_delegations(
        coordinator_agent_id,
        limit=limit,
        offset=offset,
        status_filter=status_filter,
    )
    items = [
        DelegationListItem(
            delegation_id=r.delegation_id,
            agent_id=r.agent_id,
            parent_agent_id=r.parent_agent_id,
            delegation_mode=r.delegation_mode.value,
            status=r.status.value,
            scope_prefix=r.scope_prefix,
            lease_expires_at=r.lease_expires_at,
            zone_id=r.zone_id,
            intent=r.intent,
            depth=r.depth,
            can_sub_delegate=r.can_sub_delegate,
            created_at=r.created_at,
        )
        for r in records
    ]

    return DelegationListResponse(delegations=items, total=total, limit=limit, offset=offset)


@router.get("/{delegation_id}/chain", response_model=DelegationChainResponse)
async def get_delegation_chain(
    delegation_id: str,
    auth_result: dict[str, Any] = Depends(_get_require_auth()),
    service: Any = Depends(_get_delegation_service),
) -> DelegationChainResponse:
    """Trace delegation chain from a delegation back to the root."""
    subject_type = auth_result.get("subject_type", "")
    if subject_type != "agent":
        raise HTTPException(
            status_code=403,
            detail="Only agents can trace delegation chains.",
        )

    chain = service.get_delegation_chain(delegation_id)

    if not chain:
        raise HTTPException(status_code=404, detail=f"Delegation {delegation_id} not found.")

    items = [
        DelegationChainItem(
            delegation_id=r.delegation_id,
            agent_id=r.agent_id,
            parent_agent_id=r.parent_agent_id,
            delegation_mode=r.delegation_mode.value,
            status=r.status.value,
            depth=r.depth,
            intent=r.intent,
            created_at=r.created_at,
        )
        for r in chain
    ]

    return DelegationChainResponse(chain=items, total_depth=len(chain) - 1)


class NamespaceDetailResponse(BaseModel):
    """Namespace configuration for a delegation."""

    delegation_id: str
    agent_id: str
    delegation_mode: str
    scope_prefix: str | None
    removed_grants: list[str]
    added_grants: list[str]
    readonly_paths: list[str]
    mount_table: list[str]
    zone_id: str | None


@router.get("/{delegation_id}/namespace", response_model=NamespaceDetailResponse)
async def get_delegation_namespace(
    delegation_id: str,
    auth_result: dict[str, Any] = Depends(_get_require_auth()),
    service: Any = Depends(_get_delegation_service),
) -> NamespaceDetailResponse:
    """Get the namespace configuration for a delegation.

    Returns the delegation's namespace mode, scope constraints, grant
    modifications, and the current mount table (visible paths).
    """
    subject_type = auth_result.get("subject_type", "")
    if subject_type != "agent":
        raise HTTPException(
            status_code=403,
            detail="Only agents can view delegation namespace details.",
        )

    record = service.get_delegation_by_id(delegation_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"Delegation {delegation_id} not found.")

    # Ownership check: only the parent agent can inspect namespace config
    agent_id = auth_result.get("subject_id", "")
    if record.parent_agent_id != agent_id:
        raise HTTPException(
            status_code=403,
            detail="Only the parent agent can view a delegation's namespace config.",
        )

    # Get current mount table for the worker agent
    mount_table: list[str] = []
    ns_manager = getattr(service, "_namespace_manager", None)
    if ns_manager is not None:
        try:
            entries = ns_manager.get_mount_table(
                subject=("agent", record.agent_id),
                zone_id=record.zone_id,
            )
            mount_table = [entry.virtual_path for entry in entries]
        except Exception:
            logger.warning(
                "[Delegation] Failed to get mount table for namespace detail: %s",
                delegation_id,
            )

    return NamespaceDetailResponse(
        delegation_id=record.delegation_id,
        agent_id=record.agent_id,
        delegation_mode=record.delegation_mode.value,
        scope_prefix=record.scope_prefix,
        removed_grants=list(record.removed_grants),
        added_grants=list(record.added_grants),
        readonly_paths=list(record.readonly_paths),
        mount_table=mount_table,
        zone_id=record.zone_id,
    )


class UpdateNamespaceRequest(BaseModel):
    """Request to update mutable namespace config fields.

    All fields default to None (leave unchanged). To clear scope_prefix,
    send empty string "". To set a new prefix, send a non-empty string.
    """

    scope_prefix: str | None = Field(
        default=None, description="New scope prefix (null = leave unchanged, empty = clear)"
    )
    remove_grants: list[str] | None = Field(
        default=None, description="New removed grants list (null = leave unchanged)"
    )
    add_grants: list[str] | None = Field(
        default=None, description="New added grants list (null = leave unchanged)"
    )
    readonly_paths: list[str] | None = Field(
        default=None, description="New readonly paths list (null = leave unchanged)"
    )


@router.patch("/{delegation_id}/namespace", response_model=NamespaceDetailResponse)
async def update_delegation_namespace(
    delegation_id: str,
    body: UpdateNamespaceRequest,
    auth_result: dict[str, Any] = Depends(_get_require_auth()),
    service: Any = Depends(_get_delegation_service),
) -> NamespaceDetailResponse:
    """Update mutable namespace config fields on an active delegation.

    Only scope_prefix, removed_grants, added_grants, and readonly_paths
    are editable. delegation_mode is immutable (set at creation).
    """
    subject_type = auth_result.get("subject_type", "")
    if subject_type != "agent":
        raise HTTPException(
            status_code=403,
            detail="Only agents can update delegation namespace config.",
        )

    record = service.get_delegation_by_id(delegation_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"Delegation {delegation_id} not found.")

    # Ownership check: only the parent agent can update namespace config
    agent_id = auth_result.get("subject_id", "")
    if record.parent_agent_id != agent_id:
        raise HTTPException(
            status_code=403,
            detail="Only the parent agent can update a delegation's namespace config.",
        )

    # Interpret scope_prefix: None = leave unchanged, "" = clear, else = set
    clear_prefix = body.scope_prefix is not None and body.scope_prefix == ""
    effective_prefix = None if (body.scope_prefix is None or clear_prefix) else body.scope_prefix

    try:
        updated = service.update_namespace_config(
            delegation_id,
            scope_prefix=effective_prefix,
            clear_scope_prefix=clear_prefix,
            remove_grants=body.remove_grants,
            add_grants=body.add_grants,
            readonly_paths=body.readonly_paths,
        )
    except Exception as e:
        _handle_delegation_error(e)
        raise  # unreachable

    # Get current mount table
    mount_table: list[str] = []
    ns_manager = getattr(service, "_namespace_manager", None)
    if ns_manager is not None:
        try:
            entries = ns_manager.get_mount_table(
                subject=("agent", updated.agent_id),
                zone_id=updated.zone_id,
            )
            mount_table = [entry.virtual_path for entry in entries]
        except Exception:
            logger.warning(
                "[Delegation] Failed to get mount table after namespace update: %s",
                delegation_id,
            )

    return NamespaceDetailResponse(
        delegation_id=updated.delegation_id,
        agent_id=updated.agent_id,
        delegation_mode=updated.delegation_mode.value,
        scope_prefix=updated.scope_prefix,
        removed_grants=list(updated.removed_grants),
        added_grants=list(updated.added_grants),
        readonly_paths=list(updated.readonly_paths),
        mount_table=mount_table,
        zone_id=updated.zone_id,
    )


@router.post("/{delegation_id}/complete")
async def complete_delegation(
    delegation_id: str,
    request: CompleteDelegationRequest,
    http_request: Request,
    auth_result: dict[str, Any] = Depends(_get_require_auth()),
) -> dict[str, Any]:
    """Complete a delegation and submit outcome feedback (#1619)."""
    subject_type = auth_result.get("subject_type", "")
    if subject_type != "agent":
        raise HTTPException(
            status_code=403,
            detail="Only agents can complete delegations.",
        )

    service = _get_delegation_service(http_request)

    # Ownership check: only the parent agent can complete its delegation
    record = service.get_delegation_by_id(delegation_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"Delegation {delegation_id} not found.")

    agent_id = auth_result.get("subject_id", "")
    if record.parent_agent_id != agent_id:
        raise HTTPException(
            status_code=403,
            detail="Only the parent agent can complete a delegation.",
        )

    # Parse outcome
    from nexus.bricks.delegation.models import DelegationOutcome

    try:
        outcome = DelegationOutcome(request.outcome)
    except ValueError as e:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid outcome: {request.outcome!r}. "
            "Must be 'completed', 'failed', or 'timeout'.",
        ) from e

    try:
        updated = service.complete_delegation(
            delegation_id=delegation_id,
            outcome=outcome,
            quality_score=request.quality_score,
        )
    except Exception as e:
        _handle_delegation_error(e)
        raise  # unreachable, but satisfies type checker

    return {
        "status": updated.status.value,
        "delegation_id": delegation_id,
        "outcome": request.outcome,
    }


@router.get("/{delegation_id}", response_model=DelegationDetailResponse)
async def get_delegation_detail(
    delegation_id: str,
    auth_result: dict[str, Any] = Depends(_get_require_auth()),
    service: Any = Depends(_get_delegation_service),
) -> DelegationDetailResponse:
    """Get a single delegation by ID."""
    subject_type = auth_result.get("subject_type", "")
    if subject_type != "agent":
        raise HTTPException(status_code=403, detail="Only agents can view delegations.")

    record = service.get_delegation_by_id(delegation_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"Delegation {delegation_id} not found.")

    return DelegationDetailResponse(
        delegation_id=record.delegation_id,
        agent_id=record.agent_id,
        parent_agent_id=record.parent_agent_id,
        delegation_mode=record.delegation_mode.value,
        status=record.status.value,
        scope_prefix=record.scope_prefix,
        lease_expires_at=record.lease_expires_at,
        zone_id=record.zone_id,
        intent=record.intent,
        depth=record.depth,
        can_sub_delegate=record.can_sub_delegate,
        created_at=record.created_at,
        removed_grants=list(record.removed_grants),
        added_grants=list(record.added_grants),
        readonly_paths=list(record.readonly_paths),
    )


# =============================================================================
# Error handling
# =============================================================================


def _handle_delegation_error(e: Exception) -> None:
    """Map domain errors to HTTP responses. Always raises."""
    from nexus.bricks.delegation.errors import (
        DelegationChainError,
        DelegationNotFoundError,
        DepthExceededError,
        EscalationError,
        InvalidPrefixError,
        TooManyGrantsError,
    )

    if isinstance(e, EscalationError):
        raise HTTPException(status_code=403, detail=str(e)) from e
    if isinstance(e, TooManyGrantsError):
        raise HTTPException(status_code=400, detail=str(e)) from e
    if isinstance(e, DelegationChainError):
        raise HTTPException(status_code=403, detail=str(e)) from e
    if isinstance(e, DepthExceededError):
        raise HTTPException(status_code=403, detail=str(e)) from e
    if isinstance(e, InvalidPrefixError):
        raise HTTPException(status_code=400, detail=str(e)) from e
    if isinstance(e, DelegationNotFoundError):
        raise HTTPException(status_code=404, detail=str(e)) from e
    if isinstance(e, HTTPException):
        raise
    logger.error("[Delegation] Unexpected error: %s", e, exc_info=True)
    raise HTTPException(status_code=500, detail=f"Delegation failed: {e}") from e
