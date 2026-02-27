"""Shared dependencies for API v2 endpoints.

Provides FastAPI dependency injection for ACE components with
proper authentication context. All routers should import deps
from here instead of duplicating inline helpers.

Issue #2138: Protocol return types replace ``Any`` for static type safety.
"""

import logging
from typing import TYPE_CHECKING, Any, NamedTuple, cast

from fastapi import Depends, HTTPException, Request

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.core.protocols.vfs_core import VFSCoreProtocol
from nexus.server.dependencies import get_operation_context, require_auth
from nexus.services.protocols.llm_provider import LLMProviderProtocol
from nexus.services.protocols.write_back import WriteBackProtocol

if TYPE_CHECKING:
    from nexus.contracts.types import OperationContext

logger = logging.getLogger(__name__)

# =============================================================================
# Router-facing re-exports (#2138)
# =============================================================================
# Routers import these names; the implementation now delegates directly
# to server/dependencies instead of using lazy imports.


def _get_require_auth() -> Any:
    """Return the ``require_auth`` dependency for ``Depends()``."""
    return require_auth


def _get_operation_context(auth_result: dict[str, Any]) -> "OperationContext":
    """Build an OperationContext from *auth_result*."""
    return cast("OperationContext", get_operation_context(auth_result))


# =============================================================================
# DRY helper for ACE manager context extraction (#2138)
# =============================================================================


class ACEContext(NamedTuple):
    """Common context for ACE manager construction."""

    session: Any
    backend: Any
    user_id: str
    agent_id: str | None
    zone_id: str
    context: "OperationContext"


def _get_ace_context(nexus_fs: Any, auth_result: dict[str, Any]) -> ACEContext:
    """Extract common ACE context from NexusFS and auth result.

    Centralizes the repeated pattern of:
    - Building OperationContext from auth_result
    - Extracting session/backend from nexus_fs.memory
    - Deriving user_id/agent_id/zone_id

    Args:
        nexus_fs: NexusFS instance
        auth_result: Authenticated user context dict

    Returns:
        ACEContext named tuple with all common fields.
    """
    context = get_operation_context(auth_result)
    return ACEContext(
        session=nexus_fs.memory.session,
        backend=nexus_fs.memory.backend,
        user_id=context.user_id or "anonymous",
        agent_id=getattr(context, "agent_id", None),
        zone_id=context.zone_id or ROOT_ZONE_ID,
        context=context,
    )


# =============================================================================
# Core dependencies
# =============================================================================


async def get_nexus_fs(request: Request) -> VFSCoreProtocol:
    """Get NexusFS instance, raising 503 if not initialized.

    All deps that need NexusFS should accept this via Depends()
    rather than repeating the guard inline.
    """
    if not request.app.state.nexus_fs:
        raise HTTPException(status_code=503, detail="NexusFS not initialized")
    return cast(VFSCoreProtocol, request.app.state.nexus_fs)


async def get_record_store(request: Request) -> Any:
    """Get RecordStoreABC instance from app state (Issue #2200).

    Raises 503 if record_store is not initialized. Prefer this over
    get_session_factory() for new endpoints.
    """
    record_store = getattr(request.app.state, "record_store", None)
    if record_store is None:
        raise HTTPException(status_code=503, detail="RecordStore not initialized")
    return record_store


async def get_auth_result(
    auth_result: dict[str, Any] | None = Depends(require_auth),
) -> dict[str, Any]:
    """Get authenticated user context."""
    if auth_result is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    return auth_result


async def get_memory_api(
    nexus_fs: Any = Depends(get_nexus_fs),
) -> Any:
    """Get Memory API instance from NexusFS."""
    return nexus_fs.memory


async def get_db_session(
    nexus_fs: Any = Depends(get_nexus_fs),
) -> Any:
    """Get database session from NexusFS."""
    return nexus_fs.memory.session


async def get_backend(
    nexus_fs: Any = Depends(get_nexus_fs),
) -> Any:
    """Get storage backend from NexusFS."""
    return nexus_fs.memory.backend


async def get_llm_provider(
    nexus_fs: Any = Depends(get_nexus_fs),
) -> LLMProviderProtocol | None:
    """Get LLM provider from NexusFS (may be None)."""
    provider = nexus_fs.llm_provider
    return cast(LLMProviderProtocol, provider) if provider is not None else None


# =============================================================================
# ACE manager dependencies
# =============================================================================


async def get_conflict_log_store(request: Request) -> Any:
    """Get ConflictLogStore instance from app state."""
    store = getattr(request.app.state, "conflict_log_store", None)
    if store is None:
        raise HTTPException(status_code=503, detail="Conflict log store not initialized")
    return store


async def get_write_back_service(request: Request) -> WriteBackProtocol:
    """Get WriteBackService instance from app state."""
    service = getattr(request.app.state, "write_back_service", None)
    if service is None:
        raise HTTPException(
            status_code=503,
            detail="Write-back service not initialized (set NEXUS_WRITE_BACK=true)",
        )
    return cast(WriteBackProtocol, service)


async def get_trajectory_manager(
    nexus_fs: Any = Depends(get_nexus_fs),
    auth_result: dict[str, Any] = Depends(require_auth),
) -> Any:
    """Get TrajectoryManager with current user context."""
    from nexus.services.ace.trajectory import TrajectoryManager

    ace = _get_ace_context(nexus_fs, auth_result)
    return TrajectoryManager(
        session=ace.session,
        backend=ace.backend,
        user_id=ace.user_id,
        agent_id=ace.agent_id,
        zone_id=ace.zone_id,
        context=ace.context,
    )


async def get_feedback_manager(
    nexus_fs: Any = Depends(get_nexus_fs),
) -> Any:
    """Get FeedbackManager instance."""
    from nexus.services.ace.feedback import FeedbackManager

    session = nexus_fs.memory.session
    return FeedbackManager(session=session)


async def get_playbook_manager(
    nexus_fs: Any = Depends(get_nexus_fs),
    auth_result: dict[str, Any] = Depends(require_auth),
) -> Any:
    """Get PlaybookManager with current user context."""
    from nexus.services.ace.playbook import PlaybookManager

    ace = _get_ace_context(nexus_fs, auth_result)
    return PlaybookManager(
        session=ace.session,
        backend=ace.backend,
        user_id=ace.user_id,
        agent_id=ace.agent_id,
        zone_id=ace.zone_id,
        context=ace.context,
    )


async def get_reflector(
    nexus_fs: Any = Depends(get_nexus_fs),
    auth_result: dict[str, Any] = Depends(require_auth),
) -> Any:
    """Get Reflector with current user context.

    Requires LLM provider for reflection analysis.
    """
    from nexus.services.ace.reflection import Reflector
    from nexus.services.ace.trajectory import TrajectoryManager

    ace = _get_ace_context(nexus_fs, auth_result)
    llm_provider = nexus_fs.llm_provider

    traj_manager = TrajectoryManager(
        session=ace.session,
        backend=ace.backend,
        user_id=ace.user_id,
        agent_id=ace.agent_id,
        zone_id=ace.zone_id,
        context=ace.context,
    )

    return Reflector(
        session=ace.session,
        backend=ace.backend,
        llm_provider=llm_provider,
        trajectory_manager=traj_manager,
        user_id=ace.user_id,
        agent_id=ace.agent_id,
        zone_id=ace.zone_id,
    )


async def get_curator(
    nexus_fs: Any = Depends(get_nexus_fs),
    auth_result: dict[str, Any] = Depends(require_auth),
) -> Any:
    """Get Curator with current user context."""
    from nexus.services.ace.curation import Curator
    from nexus.services.ace.playbook import PlaybookManager

    ace = _get_ace_context(nexus_fs, auth_result)

    playbook_manager = PlaybookManager(
        session=ace.session,
        backend=ace.backend,
        user_id=ace.user_id,
        agent_id=ace.agent_id,
        zone_id=ace.zone_id,
        context=ace.context,
    )

    return Curator(
        session=ace.session,
        backend=ace.backend,
        playbook_manager=playbook_manager,
    )


async def get_consolidation_engine(
    nexus_fs: Any = Depends(get_nexus_fs),
    auth_result: dict[str, Any] = Depends(require_auth),
) -> Any:
    """Get ConsolidationEngine with current user context.

    Requires LLM provider for consolidation summaries.
    """
    from nexus.services.ace.consolidation import ConsolidationEngine

    ace = _get_ace_context(nexus_fs, auth_result)
    llm_provider = nexus_fs.llm_provider

    return ConsolidationEngine(
        session=ace.session,
        backend=ace.backend,
        llm_provider=llm_provider,
        user_id=ace.user_id,
        agent_id=ace.agent_id,
        zone_id=ace.zone_id,
    )


async def get_operation_logger(
    nexus_fs: Any = Depends(get_nexus_fs),
    auth_result: dict[str, Any] = Depends(require_auth),
) -> Any:
    """Get OperationLogger scoped to the authenticated user's zone.

    Returns a tuple of (OperationLogger, zone_id) for zone-scoped queries.
    """
    from nexus.storage.operation_logger import OperationLogger

    context = get_operation_context(auth_result)
    _record_store = getattr(nexus_fs, "_record_store", None)
    session_factory = (
        _record_store.session_factory if _record_store is not None else nexus_fs.SessionLocal
    )
    session = session_factory()
    zone_id = context.zone_id or ROOT_ZONE_ID

    return OperationLogger(session=session), zone_id


async def get_hierarchy_manager(
    nexus_fs: Any = Depends(get_nexus_fs),
    auth_result: dict[str, Any] = Depends(require_auth),
) -> Any:
    """Get HierarchicalMemoryManager with current user context."""
    from nexus.services.ace.consolidation import ConsolidationEngine
    from nexus.services.ace.memory_hierarchy import HierarchicalMemoryManager

    ace = _get_ace_context(nexus_fs, auth_result)
    llm_provider = nexus_fs.llm_provider

    consolidation_engine = ConsolidationEngine(
        session=ace.session,
        backend=ace.backend,
        llm_provider=llm_provider,
        user_id=ace.user_id,
        agent_id=ace.agent_id,
        zone_id=ace.zone_id,
    )

    return HierarchicalMemoryManager(
        consolidation_engine=consolidation_engine,
        session=ace.session,
        zone_id=ace.zone_id,
    )


async def get_exchange_audit_logger(
    nexus_fs: Any = Depends(get_nexus_fs),
    auth_result: dict[str, Any] = Depends(require_auth),
) -> Any:
    """Get ExchangeAuditLogger scoped to the authenticated user's zone.

    Returns a tuple of (ExchangeAuditLogger, zone_id) for zone-scoped queries.
    Issue #1360.
    """
    from nexus.storage.exchange_audit_logger import ExchangeAuditLogger

    context = _get_operation_context(auth_result)
    zone_id = context.zone_id or ROOT_ZONE_ID

    _record_store = getattr(nexus_fs, "_record_store", None)
    if _record_store is None:
        raise HTTPException(status_code=503, detail="RecordStore not initialized")
    return ExchangeAuditLogger(record_store=_record_store), zone_id


# =============================================================================
# Reputation & Trust dependencies (Issue #1356)
# =============================================================================


async def get_reputation_context(
    nexus_fs: Any = Depends(get_nexus_fs),
    auth_result: dict[str, Any] = Depends(require_auth),
) -> tuple[Any, Any, dict[str, Any]]:
    """Get ReputationService + DisputeService + auth context.

    Prefers the singleton ReputationService on app.state (#1619),
    falling back to per-request instantiation for backward compat.

    Returns:
        Tuple of (ReputationService, DisputeService, auth_context dict).
    """
    from nexus.bricks.reputation.dispute_service import DisputeService
    from nexus.bricks.reputation.reputation_service import ReputationService

    _record_store = getattr(nexus_fs, "_record_store", None)
    if _record_store is None:
        raise HTTPException(status_code=503, detail="RecordStore not initialized")

    # Per-request instantiation (singleton DI via app.state planned in #1619)
    reputation_service = ReputationService(
        record_store=_record_store,
    )
    dispute_service = DisputeService(record_store=_record_store)

    context = get_operation_context(auth_result)
    auth_ctx = {
        "user_id": context.user_id or "",
        "subject_id": getattr(context, "subject_id", ""),
        "subject_type": getattr(context, "subject_type", ""),
        "is_admin": getattr(context, "is_admin", False),
        "zone_id": context.zone_id,
    }

    return reputation_service, dispute_service, auth_ctx
