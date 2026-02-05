"""Shared dependencies for API v2 endpoints.

Provides FastAPI dependency injection for ACE components with
proper authentication context.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from fastapi import Depends, HTTPException

if TYPE_CHECKING:
    from nexus.core.ace.consolidation import ConsolidationEngine
    from nexus.core.ace.curation import Curator
    from nexus.core.ace.feedback import FeedbackManager
    from nexus.core.ace.memory_hierarchy import HierarchicalMemoryManager
    from nexus.core.ace.playbook import PlaybookManager
    from nexus.core.ace.reflection import Reflector
    from nexus.core.ace.trajectory import TrajectoryManager
    from nexus.core.memory_api import Memory

logger = logging.getLogger(__name__)


def _get_app_state() -> Any:
    """Get the global app state.

    This is imported lazily to avoid circular imports.
    """
    from nexus.server.fastapi_server import _app_state

    return _app_state


def _get_require_auth() -> Any:
    """Get the require_auth dependency.

    This is imported lazily to avoid circular imports.
    """
    from nexus.server.fastapi_server import require_auth

    return require_auth


def _get_operation_context(auth_result: dict[str, Any]) -> Any:
    """Get operation context from auth result.

    This is imported lazily to avoid circular imports.
    """
    from nexus.server.fastapi_server import get_operation_context

    return get_operation_context(auth_result)


async def get_auth_result(
    auth_result: dict[str, Any] | None = Depends(lambda: _get_require_auth()),
) -> dict[str, Any]:
    """Get authenticated user context.

    This dependency ensures the request is authenticated and returns
    the auth result dict containing zone_id, user_id, etc.
    """
    # The actual require_auth is called via Depends
    # This is a passthrough for type hints
    if auth_result is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    return auth_result


async def get_memory_api() -> Memory:
    """Get Memory API instance.

    Returns the Memory API from NexusFS. The Memory class already
    handles permission checks internally.
    """
    app_state = _get_app_state()
    if not app_state.nexus_fs:
        raise HTTPException(status_code=503, detail="NexusFS not initialized")

    return app_state.nexus_fs.memory  # type: ignore[no-any-return]


async def get_db_session() -> Any:
    """Get database session from NexusFS.

    Uses the session from the Memory API (which creates one via SessionLocal).
    """
    app_state = _get_app_state()
    if not app_state.nexus_fs:
        raise HTTPException(status_code=503, detail="NexusFS not initialized")

    # Access memory property to ensure session is created
    return app_state.nexus_fs.memory.session


async def get_backend() -> Any:
    """Get storage backend from NexusFS."""
    app_state = _get_app_state()
    if not app_state.nexus_fs:
        raise HTTPException(status_code=503, detail="NexusFS not initialized")

    return app_state.nexus_fs.memory.backend


async def get_llm_provider() -> Any:
    """Get LLM provider from NexusFS (may be None)."""
    app_state = _get_app_state()
    if not app_state.nexus_fs:
        raise HTTPException(status_code=503, detail="NexusFS not initialized")

    return getattr(app_state.nexus_fs, "_llm_provider", None)


async def get_trajectory_manager(
    auth_result: dict[str, Any] = Depends(_get_require_auth()),
) -> TrajectoryManager:
    """Get TrajectoryManager with current user context.

    Creates a new TrajectoryManager instance configured for the
    authenticated user/agent/zone.
    """
    app_state = _get_app_state()
    if not app_state.nexus_fs:
        raise HTTPException(status_code=503, detail="NexusFS not initialized")

    from nexus.core.ace.trajectory import TrajectoryManager

    context = _get_operation_context(auth_result)
    session = app_state.nexus_fs.memory.session
    backend = app_state.nexus_fs.memory.backend

    return TrajectoryManager(
        session=session,
        backend=backend,
        user_id=context.user_id or context.user or "anonymous",
        agent_id=getattr(context, "agent_id", None),
        tenant_id=context.zone_id,
        context=context,
    )


async def get_feedback_manager() -> FeedbackManager:
    """Get FeedbackManager instance.

    FeedbackManager only requires a database session.
    """
    app_state = _get_app_state()
    if not app_state.nexus_fs:
        raise HTTPException(status_code=503, detail="NexusFS not initialized")

    from nexus.core.ace.feedback import FeedbackManager

    session = app_state.nexus_fs.memory.session
    return FeedbackManager(session=session)


async def get_playbook_manager(
    auth_result: dict[str, Any] = Depends(_get_require_auth()),
) -> PlaybookManager:
    """Get PlaybookManager with current user context.

    Creates a new PlaybookManager instance configured for the
    authenticated user/agent/zone.
    """
    app_state = _get_app_state()
    if not app_state.nexus_fs:
        raise HTTPException(status_code=503, detail="NexusFS not initialized")

    from nexus.core.ace.playbook import PlaybookManager

    context = _get_operation_context(auth_result)
    session = app_state.nexus_fs.memory.session
    backend = app_state.nexus_fs.memory.backend

    return PlaybookManager(
        session=session,
        backend=backend,
        user_id=context.user_id or context.user or "anonymous",
        agent_id=getattr(context, "agent_id", None),
        tenant_id=context.zone_id,
        context=context,
    )


async def get_reflector(
    auth_result: dict[str, Any] = Depends(_get_require_auth()),
) -> Reflector:
    """Get Reflector with current user context.

    Creates a new Reflector instance. Requires LLM provider for
    reflection analysis.
    """
    app_state = _get_app_state()
    if not app_state.nexus_fs:
        raise HTTPException(status_code=503, detail="NexusFS not initialized")

    from nexus.core.ace.reflection import Reflector
    from nexus.core.ace.trajectory import TrajectoryManager

    context = _get_operation_context(auth_result)
    session = app_state.nexus_fs.memory.session
    backend = app_state.nexus_fs.memory.backend
    llm_provider = getattr(app_state.nexus_fs, "_llm_provider", None)

    # Create trajectory manager for reflector
    traj_manager = TrajectoryManager(
        session=session,
        backend=backend,
        user_id=context.user_id or context.user or "anonymous",
        agent_id=getattr(context, "agent_id", None),
        tenant_id=context.zone_id,
        context=context,
    )

    return Reflector(
        session=session,
        backend=backend,
        llm_provider=llm_provider,  # type: ignore[arg-type]
        trajectory_manager=traj_manager,
        user_id=context.user_id or context.user or "anonymous",
        agent_id=getattr(context, "agent_id", None),
        tenant_id=context.zone_id,
    )


async def get_curator(
    auth_result: dict[str, Any] = Depends(_get_require_auth()),
) -> Curator:
    """Get Curator with current user context.

    Creates a new Curator instance configured for the
    authenticated user/agent/zone.
    """
    app_state = _get_app_state()
    if not app_state.nexus_fs:
        raise HTTPException(status_code=503, detail="NexusFS not initialized")

    from nexus.core.ace.curation import Curator
    from nexus.core.ace.playbook import PlaybookManager

    context = _get_operation_context(auth_result)
    session = app_state.nexus_fs.memory.session
    backend = app_state.nexus_fs.memory.backend

    # Create playbook manager for curator
    playbook_manager = PlaybookManager(
        session=session,
        backend=backend,
        user_id=context.user_id or context.user or "anonymous",
        agent_id=getattr(context, "agent_id", None),
        tenant_id=context.zone_id,
        context=context,
    )

    return Curator(
        session=session,
        backend=backend,
        playbook_manager=playbook_manager,
    )


async def get_consolidation_engine(
    auth_result: dict[str, Any] = Depends(_get_require_auth()),
) -> ConsolidationEngine:
    """Get ConsolidationEngine with current user context.

    Creates a new ConsolidationEngine instance. Requires LLM provider
    for consolidation summaries.
    """
    app_state = _get_app_state()
    if not app_state.nexus_fs:
        raise HTTPException(status_code=503, detail="NexusFS not initialized")

    from nexus.core.ace.consolidation import ConsolidationEngine

    context = _get_operation_context(auth_result)
    session = app_state.nexus_fs.memory.session
    backend = app_state.nexus_fs.memory.backend
    llm_provider = getattr(app_state.nexus_fs, "_llm_provider", None)

    return ConsolidationEngine(
        session=session,
        backend=backend,
        llm_provider=llm_provider,  # type: ignore[arg-type]
        user_id=context.user_id or context.user or "anonymous",
        agent_id=getattr(context, "agent_id", None),
        tenant_id=context.zone_id,
    )


async def get_hierarchy_manager(
    auth_result: dict[str, Any] = Depends(_get_require_auth()),
) -> HierarchicalMemoryManager:
    """Get HierarchicalMemoryManager with current user context.

    Creates a new HierarchicalMemoryManager instance configured for
    the authenticated zone.
    """
    app_state = _get_app_state()
    if not app_state.nexus_fs:
        raise HTTPException(status_code=503, detail="NexusFS not initialized")

    from nexus.core.ace.consolidation import ConsolidationEngine
    from nexus.core.ace.memory_hierarchy import HierarchicalMemoryManager

    context = _get_operation_context(auth_result)
    session = app_state.nexus_fs.memory.session
    backend = app_state.nexus_fs.memory.backend
    llm_provider = getattr(app_state.nexus_fs, "_llm_provider", None)

    # Create consolidation engine for hierarchy manager
    consolidation_engine = ConsolidationEngine(
        session=session,
        backend=backend,
        llm_provider=llm_provider,  # type: ignore[arg-type]
        user_id=context.user_id or context.user or "anonymous",
        agent_id=getattr(context, "agent_id", None),
        tenant_id=context.zone_id,
    )

    return HierarchicalMemoryManager(
        consolidation_engine=consolidation_engine,
        session=session,
        tenant_id=context.zone_id or "default",
    )
