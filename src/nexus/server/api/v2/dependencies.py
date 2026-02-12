"""Shared dependencies for API v2 endpoints.

Provides FastAPI dependency injection for ACE components with
proper authentication context. All routers should import deps
from here instead of duplicating inline helpers.

Note: This module intentionally does NOT use `from __future__ import annotations`
because FastAPI uses `eval_str=True` on dependency signatures at import time,
which fails for TYPE_CHECKING-only imports.
"""

import logging
from typing import Any

from fastapi import Depends, HTTPException

logger = logging.getLogger(__name__)


# =============================================================================
# Internal lazy-import helpers (avoid circular imports with fastapi_server)
# =============================================================================


def _get_app_state() -> Any:
    """Get the global app state (lazy import)."""
    from nexus.server.fastapi_server import _app_state

    return _app_state


def _get_require_auth() -> Any:
    """Get the require_auth dependency (lazy import)."""
    from nexus.server.fastapi_server import require_auth

    return require_auth


def _get_operation_context(auth_result: dict[str, Any]) -> Any:
    """Get operation context from auth result (lazy import)."""
    from nexus.server.fastapi_server import get_operation_context

    return get_operation_context(auth_result)


# =============================================================================
# Core dependencies
# =============================================================================


async def get_nexus_fs() -> Any:
    """Get NexusFS instance, raising 503 if not initialized.

    All deps that need NexusFS should accept this via Depends()
    rather than repeating the guard inline.
    """
    app_state = _get_app_state()
    if not app_state.nexus_fs:
        raise HTTPException(status_code=503, detail="NexusFS not initialized")
    return app_state.nexus_fs


async def get_auth_result(
    auth_result: dict[str, Any] | None = Depends(lambda: _get_require_auth()),
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
) -> Any:
    """Get LLM provider from NexusFS (may be None)."""
    return getattr(nexus_fs, "_llm_provider", None)


# =============================================================================
# ACE manager dependencies
# =============================================================================


async def get_conflict_log_store() -> Any:
    """Get ConflictLogStore instance from app state."""
    app_state = _get_app_state()
    store = getattr(app_state, "conflict_log_store", None)
    if store is None:
        raise HTTPException(status_code=503, detail="Conflict log store not initialized")
    return store


async def get_trajectory_manager(
    nexus_fs: Any = Depends(get_nexus_fs),
    auth_result: dict[str, Any] = Depends(_get_require_auth()),
) -> Any:
    """Get TrajectoryManager with current user context."""
    from nexus.core.ace.trajectory import TrajectoryManager

    context = _get_operation_context(auth_result)
    session = nexus_fs.memory.session
    backend = nexus_fs.memory.backend

    return TrajectoryManager(
        session=session,
        backend=backend,
        user_id=context.user_id or context.user or "anonymous",
        agent_id=getattr(context, "agent_id", None),
        zone_id=context.zone_id,
        context=context,
    )


async def get_feedback_manager(
    nexus_fs: Any = Depends(get_nexus_fs),
) -> Any:
    """Get FeedbackManager instance."""
    from nexus.core.ace.feedback import FeedbackManager

    session = nexus_fs.memory.session
    return FeedbackManager(session=session)


async def get_playbook_manager(
    nexus_fs: Any = Depends(get_nexus_fs),
    auth_result: dict[str, Any] = Depends(_get_require_auth()),
) -> Any:
    """Get PlaybookManager with current user context."""
    from nexus.core.ace.playbook import PlaybookManager

    context = _get_operation_context(auth_result)
    session = nexus_fs.memory.session
    backend = nexus_fs.memory.backend

    return PlaybookManager(
        session=session,
        backend=backend,
        user_id=context.user_id or context.user or "anonymous",
        agent_id=getattr(context, "agent_id", None),
        zone_id=context.zone_id,
        context=context,
    )


async def get_reflector(
    nexus_fs: Any = Depends(get_nexus_fs),
    auth_result: dict[str, Any] = Depends(_get_require_auth()),
) -> Any:
    """Get Reflector with current user context.

    Requires LLM provider for reflection analysis.
    """
    from nexus.core.ace.reflection import Reflector
    from nexus.core.ace.trajectory import TrajectoryManager

    context = _get_operation_context(auth_result)
    session = nexus_fs.memory.session
    backend = nexus_fs.memory.backend
    llm_provider = getattr(nexus_fs, "_llm_provider", None)

    traj_manager = TrajectoryManager(
        session=session,
        backend=backend,
        user_id=context.user_id or context.user or "anonymous",
        agent_id=getattr(context, "agent_id", None),
        zone_id=context.zone_id,
        context=context,
    )

    return Reflector(
        session=session,
        backend=backend,
        llm_provider=llm_provider,  # type: ignore[arg-type]
        trajectory_manager=traj_manager,
        user_id=context.user_id or context.user or "anonymous",
        agent_id=getattr(context, "agent_id", None),
        zone_id=context.zone_id,
    )


async def get_curator(
    nexus_fs: Any = Depends(get_nexus_fs),
    auth_result: dict[str, Any] = Depends(_get_require_auth()),
) -> Any:
    """Get Curator with current user context."""
    from nexus.core.ace.curation import Curator
    from nexus.core.ace.playbook import PlaybookManager

    context = _get_operation_context(auth_result)
    session = nexus_fs.memory.session
    backend = nexus_fs.memory.backend

    playbook_manager = PlaybookManager(
        session=session,
        backend=backend,
        user_id=context.user_id or context.user or "anonymous",
        agent_id=getattr(context, "agent_id", None),
        zone_id=context.zone_id,
        context=context,
    )

    return Curator(
        session=session,
        backend=backend,
        playbook_manager=playbook_manager,
    )


async def get_consolidation_engine(
    nexus_fs: Any = Depends(get_nexus_fs),
    auth_result: dict[str, Any] = Depends(_get_require_auth()),
) -> Any:
    """Get ConsolidationEngine with current user context.

    Requires LLM provider for consolidation summaries.
    """
    from nexus.core.ace.consolidation import ConsolidationEngine

    context = _get_operation_context(auth_result)
    session = nexus_fs.memory.session
    backend = nexus_fs.memory.backend
    llm_provider = getattr(nexus_fs, "_llm_provider", None)

    return ConsolidationEngine(
        session=session,
        backend=backend,
        llm_provider=llm_provider,  # type: ignore[arg-type]
        user_id=context.user_id or context.user or "anonymous",
        agent_id=getattr(context, "agent_id", None),
        zone_id=context.zone_id,
    )


async def get_operation_logger(
    nexus_fs: Any = Depends(get_nexus_fs),
    auth_result: dict[str, Any] = Depends(_get_require_auth()),
) -> Any:
    """Get OperationLogger scoped to the authenticated user's zone.

    Returns a tuple of (OperationLogger, zone_id) for zone-scoped queries.
    """
    from nexus.storage.operation_logger import OperationLogger

    context = _get_operation_context(auth_result)
    session = nexus_fs.SessionLocal()
    zone_id = context.zone_id or "default"

    return OperationLogger(session=session), zone_id


async def get_hierarchy_manager(
    nexus_fs: Any = Depends(get_nexus_fs),
    auth_result: dict[str, Any] = Depends(_get_require_auth()),
) -> Any:
    """Get HierarchicalMemoryManager with current user context."""
    from nexus.core.ace.consolidation import ConsolidationEngine
    from nexus.core.ace.memory_hierarchy import HierarchicalMemoryManager

    context = _get_operation_context(auth_result)
    session = nexus_fs.memory.session
    backend = nexus_fs.memory.backend
    llm_provider = getattr(nexus_fs, "_llm_provider", None)

    consolidation_engine = ConsolidationEngine(
        session=session,
        backend=backend,
        llm_provider=llm_provider,  # type: ignore[arg-type]
        user_id=context.user_id or context.user or "anonymous",
        agent_id=getattr(context, "agent_id", None),
        zone_id=context.zone_id,
    )

    return HierarchicalMemoryManager(
        consolidation_engine=consolidation_engine,
        session=session,
        zone_id=context.zone_id or "default",
    )


async def get_exchange_audit_logger(
    nexus_fs: Any = Depends(get_nexus_fs),
    auth_result: dict[str, Any] = Depends(_get_require_auth()),
) -> Any:
    """Get ExchangeAuditLogger scoped to the authenticated user's zone.

    Returns a tuple of (ExchangeAuditLogger, zone_id) for zone-scoped queries.
    Issue #1360.
    """
    from nexus.storage.exchange_audit_logger import ExchangeAuditLogger

    context = _get_operation_context(auth_result)
    zone_id = context.zone_id or "default"

    session_factory = nexus_fs.SessionLocal
    return ExchangeAuditLogger(session_factory=session_factory), zone_id


# =============================================================================
# Reputation & Trust dependencies (Issue #1356)
# =============================================================================


async def get_reputation_context(
    nexus_fs: Any = Depends(get_nexus_fs),
    auth_result: dict[str, Any] = Depends(_get_require_auth()),
) -> tuple[Any, Any, dict[str, Any]]:
    """Get ReputationService + DisputeService + auth context.

    Returns:
        Tuple of (ReputationService, DisputeService, auth_context dict).
    """
    from nexus.core.dispute_service import DisputeService
    from nexus.core.reputation_service import ReputationService

    session_factory = nexus_fs.SessionLocal
    reputation_service = ReputationService(
        session_factory=session_factory,
        cache_maxsize=10_000,
        cache_ttl=60,
    )
    dispute_service = DisputeService(session_factory=session_factory)

    context = _get_operation_context(auth_result)
    auth_ctx = {
        "user_id": context.user_id or context.user or "",
        "subject_id": getattr(context, "subject_id", ""),
        "subject_type": getattr(context, "subject_type", ""),
        "is_admin": getattr(context, "is_admin", False),
        "zone_id": context.zone_id,
    }

    return reputation_service, dispute_service, auth_ctx
