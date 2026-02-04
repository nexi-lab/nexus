"""Consolidation REST API endpoints.

Provides 4 endpoints for memory consolidation:
- POST /api/v2/consolidate              - Consolidate by affinity
- POST /api/v2/consolidate/hierarchy    - Build memory hierarchy
- GET  /api/v2/consolidate/hierarchy/{id} - Get hierarchy for memory
- POST /api/v2/consolidate/decay        - Apply importance decay
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from nexus.server.api.v2.models import (
    ConsolidateRequest,
    ConsolidationResponse,
    DecayRequest,
    DecayResponse,
    HierarchyBuildRequest,
    HierarchyResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v2/consolidate", tags=["consolidation"])


def _get_require_auth() -> Any:
    """Lazy import to avoid circular imports."""
    from nexus.server.fastapi_server import require_auth

    return require_auth


def _get_app_state() -> Any:
    """Lazy import to avoid circular imports."""
    from nexus.server.fastapi_server import _app_state

    return _app_state


def _get_operation_context(auth_result: dict[str, Any]) -> Any:
    """Get operation context from auth result."""
    from nexus.server.fastapi_server import get_operation_context

    return get_operation_context(auth_result)


def _get_consolidation_engine(auth_result: dict[str, Any]) -> Any:
    """Create ConsolidationEngine with user context."""
    from nexus.core.ace.consolidation import ConsolidationEngine

    app_state = _get_app_state()
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
        tenant_id=context.tenant_id,
    )


def _get_hierarchy_manager(auth_result: dict[str, Any]) -> Any:
    """Create HierarchicalMemoryManager with user context."""
    from nexus.core.ace.consolidation import ConsolidationEngine
    from nexus.core.ace.memory_hierarchy import HierarchicalMemoryManager

    app_state = _get_app_state()
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
        tenant_id=context.tenant_id,
    )

    return HierarchicalMemoryManager(
        consolidation_engine=consolidation_engine,
        session=session,
        tenant_id=context.tenant_id or "default",
    )


# =============================================================================
# Endpoints
# =============================================================================


@router.post("", response_model=ConsolidationResponse)
async def consolidate_by_affinity(
    request: ConsolidateRequest,
    auth_result: dict[str, Any] = Depends(_get_require_auth()),
) -> ConsolidationResponse:
    """Consolidate memories using affinity-based clustering.

    Uses SimpleMem-inspired affinity scoring combining semantic similarity
    and temporal proximity for intelligent memory consolidation.

    The affinity formula is:
        affinity = beta * cos(vi, vj) + (1-beta) * exp(-lambda * |ti - tj|)

    Where:
    - beta: Weight for semantic similarity (default 0.7)
    - lambda_decay: Temporal decay rate (default 0.1)
    - cos(vi, vj): Cosine similarity of embeddings
    - |ti - tj|: Time difference in specified units
    """
    app_state = _get_app_state()
    if not app_state.nexus_fs:
        raise HTTPException(status_code=503, detail="NexusFS not initialized")

    try:
        engine = _get_consolidation_engine(auth_result)

        result = await engine.consolidate_by_affinity_async(
            memory_ids=request.memory_ids,
            beta=request.beta,
            lambda_decay=request.lambda_decay,
            affinity_threshold=request.affinity_threshold,
            importance_max=request.importance_max,
            memory_type=request.memory_type,
            namespace=request.namespace,
            limit=request.limit,
        )

        # Extract results summary
        clusters = result.get("clusters", [])
        total_consolidated = sum(len(c.get("memory_ids", [])) for c in clusters)

        return ConsolidationResponse(
            clusters_formed=len(clusters),
            total_consolidated=total_consolidated,
            archived_count=result.get("archived_count", 0),
            results=clusters,
        )

    except Exception as e:
        logger.error(f"Consolidation error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Consolidation error: {e}") from e


@router.post("/hierarchy", response_model=HierarchyResponse)
async def build_hierarchy(
    request: HierarchyBuildRequest,
    auth_result: dict[str, Any] = Depends(_get_require_auth()),
) -> HierarchyResponse:
    """Build a memory hierarchy.

    Creates a multi-level abstraction hierarchy from atomic memories:
    - Level 0: Atomic memories (original)
    - Level 1: Clusters of related memories
    - Level 2: Abstract summaries of clusters
    - Level 3+: Meta-abstractions

    This enables efficient retrieval by searching higher-level abstracts
    first and drilling down to atomic memories when needed.
    """
    app_state = _get_app_state()
    if not app_state.nexus_fs:
        raise HTTPException(status_code=503, detail="NexusFS not initialized")

    # Check if LLM provider is available (needed for abstracts)
    llm_provider = getattr(app_state.nexus_fs, "_llm_provider", None)
    if llm_provider is None:
        raise HTTPException(
            status_code=503,
            detail="LLM provider not configured. Hierarchy building requires an LLM.",
        )

    try:
        hierarchy_manager = _get_hierarchy_manager(auth_result)

        result = await hierarchy_manager.build_hierarchy_async(
            memory_ids=request.memory_ids,
            max_levels=request.max_levels,
            cluster_threshold=request.cluster_threshold,
            beta=request.beta,
            lambda_decay=request.lambda_decay,
            time_unit_hours=request.time_unit_hours,
        )

        # Convert HierarchyResult to response format
        levels_dict = {}
        if hasattr(result, "levels"):
            for level_num, level_data in result.levels.items():
                levels_dict[str(level_num)] = {
                    "memory_count": len(getattr(level_data, "memories", [])),
                    "memory_ids": [
                        getattr(m, "memory_id", str(m)) for m in getattr(level_data, "memories", [])
                    ],
                }

        return HierarchyResponse(
            total_memories=getattr(result, "total_memories", 0),
            total_abstracts_created=getattr(result, "total_abstracts_created", 0),
            max_level_reached=getattr(result, "max_level_reached", 0),
            levels=levels_dict,
            statistics=getattr(result, "statistics", None),
        )

    except Exception as e:
        logger.error(f"Hierarchy build error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Hierarchy build error: {e}") from e


@router.get("/hierarchy/{memory_id}")
async def get_hierarchy_for_memory(
    memory_id: str,
    include_children: bool = True,
    auth_result: dict[str, Any] = Depends(_get_require_auth()),
) -> dict[str, Any]:
    """Get hierarchy information for a memory.

    Returns the memory's position in the hierarchy including:
    - Parent abstract (if any)
    - Child memories (if this is an abstract)
    - Abstraction level
    """
    app_state = _get_app_state()
    if not app_state.nexus_fs:
        raise HTTPException(status_code=503, detail="NexusFS not initialized")

    try:
        hierarchy_manager = _get_hierarchy_manager(auth_result)
        result = hierarchy_manager.get_hierarchy_for_memory(
            memory_id=memory_id,
            include_children=include_children,
        )

        return {"hierarchy": result}

    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except Exception as e:
        logger.error(f"Hierarchy get error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Hierarchy get error: {e}") from e


@router.post("/decay", response_model=DecayResponse)
async def apply_decay(
    request: DecayRequest,
    _auth_result: dict[str, Any] = Depends(_get_require_auth()),
) -> DecayResponse:
    """Apply importance decay to memories.

    Batch updates memory importance scores based on time-based decay.
    Run periodically (e.g., daily cron) to maintain importance freshness.

    The decay formula is:
        importance_new = max(min_importance, importance_current * decay_factor ^ days_since_access)

    Where:
    - decay_factor: Decay rate per period (default 0.95)
    - min_importance: Floor value for importance (default 0.1)
    """
    app_state = _get_app_state()
    if not app_state.nexus_fs:
        raise HTTPException(status_code=503, detail="NexusFS not initialized")

    try:
        result = app_state.nexus_fs.memory.apply_decay_batch(
            decay_factor=request.decay_factor,
            min_importance=request.min_importance,
            batch_size=request.batch_size,
        )

        return DecayResponse(
            success=result.get("success", True),
            updated=result.get("updated", 0),
            skipped=result.get("skipped", 0),
            processed=result.get("processed", 0),
            error=result.get("error"),
        )

    except Exception as e:
        logger.error(f"Decay error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Decay error: {e}") from e
