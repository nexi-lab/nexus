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

from nexus.server.api.v2.dependencies import (
    _get_require_auth,
    get_consolidation_engine,
    get_hierarchy_manager,
    get_llm_provider,
    get_memory_api,
)
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


# =============================================================================
# Endpoints
# =============================================================================


@router.post("", response_model=ConsolidationResponse)
async def consolidate_by_affinity(
    request: ConsolidateRequest,
    engine: Any = Depends(get_consolidation_engine),
) -> ConsolidationResponse:
    """Consolidate memories using affinity-based clustering.

    Uses SimpleMem-inspired affinity scoring combining semantic similarity
    and temporal proximity for intelligent memory consolidation.

    The affinity formula is:
        affinity = beta * cos(vi, vj) + (1-beta) * exp(-lambda * |ti - tj|)
    """
    try:
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
        raise HTTPException(status_code=500, detail="Failed to consolidate memories") from e


@router.post("/hierarchy", response_model=HierarchyResponse)
async def build_hierarchy(
    request: HierarchyBuildRequest,
    hierarchy_manager: Any = Depends(get_hierarchy_manager),
    llm_provider: Any = Depends(get_llm_provider),
) -> HierarchyResponse:
    """Build a memory hierarchy.

    Creates a multi-level abstraction hierarchy from atomic memories.
    Requires an LLM provider for generating abstracts.
    """
    if llm_provider is None:
        raise HTTPException(
            status_code=503,
            detail="LLM provider not configured. Hierarchy building requires an LLM.",
        )

    try:
        result = await hierarchy_manager.build_hierarchy_async(
            memory_ids=request.memory_ids,
            max_levels=request.max_levels,
            cluster_threshold=request.cluster_threshold,
            beta=request.beta,
            lambda_decay=request.lambda_decay,
            time_unit_hours=request.time_unit_hours,
        )

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
        raise HTTPException(status_code=500, detail="Failed to build hierarchy") from e


@router.get("/hierarchy/{memory_id}")
async def get_hierarchy_for_memory(
    memory_id: str,
    include_children: bool = True,
    hierarchy_manager: Any = Depends(get_hierarchy_manager),
) -> dict[str, Any]:
    """Get hierarchy information for a memory."""
    try:
        result = hierarchy_manager.get_hierarchy_for_memory(
            memory_id=memory_id,
            include_children=include_children,
        )

        return {"hierarchy": result}

    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except Exception as e:
        logger.error(f"Hierarchy get error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to retrieve hierarchy") from e


@router.post("/decay", response_model=DecayResponse)
async def apply_decay(
    request: DecayRequest,
    _auth_result: dict[str, Any] = Depends(_get_require_auth()),
    memory_api: Any = Depends(get_memory_api),
) -> DecayResponse:
    """Apply importance decay to memories.

    Batch updates memory importance scores based on time-based decay.
    """
    try:
        result = memory_api.apply_decay_batch(
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
        raise HTTPException(status_code=500, detail="Failed to apply decay") from e
