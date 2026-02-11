"""Curation REST API endpoints.

Provides 2 endpoints for curation:
- POST /api/v2/curate      - Curate memories into playbook
- POST /api/v2/curate/bulk - Bulk curation from trajectories
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from nexus.server.api.v2.dependencies import get_curator
from nexus.server.api.v2.models import (
    CurateBulkRequest,
    CurateRequest,
    CurationResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["curation"])


# =============================================================================
# Endpoints
# =============================================================================


@router.post("/api/v2/curate", response_model=CurationResponse)
async def curate_memories(
    request: CurateRequest,
    curator: Any = Depends(get_curator),
) -> CurationResponse:
    """Curate reflection memories into a playbook.

    Takes reflection memories and extracts strategies to add to
    or merge with existing strategies in the target playbook.
    """
    try:
        result = curator.curate_playbook(
            playbook_id=request.playbook_id,
            reflection_memory_ids=request.reflection_memory_ids,
            merge_threshold=request.merge_threshold,
        )

        return CurationResponse(
            playbook_id=result.get("playbook_id", request.playbook_id),
            strategies_added=result.get("strategies_added", 0),
            strategies_merged=result.get("strategies_merged", 0),
            strategies_total=result.get("strategies_total", 0),
        )

    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except Exception as e:
        logger.error(f"Curation error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to curate memories") from e


@router.post("/api/v2/curate/bulk")
async def curate_bulk(
    request: CurateBulkRequest,
    curator: Any = Depends(get_curator),
) -> dict[str, Any]:
    """Bulk curation from trajectories.

    Processes multiple trajectories and curates their reflections
    into the target playbook.

    Note: trajectories are processed sequentially because the underlying
    SQLAlchemy session is not thread-safe for concurrent writes.
    """
    try:
        results = []
        errors = []

        # Sequential iteration — SQLAlchemy session is not thread-safe
        for trajectory_id in request.trajectory_ids:
            try:
                result = curator.curate_from_trajectory(
                    playbook_id=request.playbook_id,
                    trajectory_id=trajectory_id,
                )
                if result:
                    results.append(
                        {
                            "trajectory_id": trajectory_id,
                            **result,
                        }
                    )
            except Exception as e:
                errors.append(
                    {
                        "trajectory_id": trajectory_id,
                        "error": str(e),
                    }
                )

        return {
            "playbook_id": request.playbook_id,
            "processed": len(results),
            "failed": len(errors),
            "results": results,
            "errors": errors if errors else None,
        }

    except Exception as e:
        logger.error(f"Bulk curation error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to bulk curate") from e
