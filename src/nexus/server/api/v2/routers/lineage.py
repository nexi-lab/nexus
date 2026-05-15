"""Lineage REST API endpoints (Issue #3417).

Provides endpoints for querying and managing agent lineage:
- GET /api/v2/lineage/{urn}             -- Get upstream lineage for an entity
- GET /api/v2/lineage/downstream/query  -- Find downstream dependents (impact analysis)
- GET /api/v2/lineage/stale/query       -- Find stale downstream entities
- PUT /api/v2/lineage/{urn}             -- Explicitly declare lineage
- DELETE /api/v2/lineage/{urn}          -- Delete lineage for an entity
- POST /api/v2/lineage/scope/begin      -- Begin a named lineage scope
- POST /api/v2/lineage/scope/end        -- End a scope (consume + close)
- GET /api/v2/lineage/scope/active      -- Get the active scope for a session
"""

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query

from nexus.server.api.v2.models.lineage import (
    DownstreamEntry,
    DownstreamResponse,
    LineageResponse,
    PutLineageRequest,
    ScopeRequest,
    ScopeResponse,
    StaleEntry,
    StaleResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v2/lineage", tags=["lineage"])


def _verify_urn_zone(urn: str, zone_id: str) -> None:
    """Verify URN belongs to caller's zone (prevent cross-zone data exposure)."""
    if zone_id in ("root", "default"):
        return
    if f":{zone_id}:" not in urn:
        raise HTTPException(status_code=403, detail="Access denied: URN is outside your zone")


async def _get_lineage_service(
    nexus_fs: Any = Depends(),
    auth_result: dict[str, Any] = Depends(),
) -> Any:
    """Placeholder — actual dependency injection below."""


# Override dependency at module level to use the real deps
def _make_lineage_dependency() -> Any:
    """Create the lineage service dependency."""
    from nexus.server.api.v2.dependencies import get_nexus_fs, require_auth
    from nexus.server.dependencies import get_operation_context

    async def get_lineage_service(
        nexus_fs: Any = Depends(get_nexus_fs),
        auth_result: dict[str, Any] = Depends(require_auth),
    ) -> Any:
        from nexus.contracts.constants import ROOT_ZONE_ID
        from nexus.storage.lineage_service import LineageService

        context = get_operation_context(auth_result)
        _record_store = getattr(nexus_fs, "_record_store", None)
        session_factory = (
            _record_store.session_factory if _record_store is not None else nexus_fs.SessionLocal
        )
        session = session_factory()
        zone_id = context.zone_id or ROOT_ZONE_ID

        try:
            yield LineageService(session=session), zone_id
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    return get_lineage_service


get_lineage_service = _make_lineage_dependency()


@router.get("/{urn}")
async def get_lineage(
    urn: str = Path(..., description="Entity URN"),
    lineage_and_zone: tuple[Any, str] = Depends(get_lineage_service),
) -> LineageResponse:
    """Get upstream lineage for an entity."""
    lineage_svc, zone_id = lineage_and_zone
    _verify_urn_zone(urn, zone_id)
    try:
        payload = lineage_svc.get_lineage(urn)
        if payload is None:
            raise HTTPException(status_code=404, detail=f"No lineage found for {urn}")
        return LineageResponse(entity_urn=urn, **payload)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("get_lineage error: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to get lineage") from e


@router.get("/downstream/query")
async def get_downstream(
    path: str = Query(..., description="Upstream file path"),
    lineage_and_zone: tuple[Any, str] = Depends(get_lineage_service),
    limit: int = Query(100, ge=1, le=1000, description="Max results"),
) -> DownstreamResponse:
    """Find downstream entities that depend on an upstream path (impact analysis)."""
    lineage_svc, zone_id = lineage_and_zone
    try:
        results = lineage_svc.find_downstream(path, zone_id=zone_id, limit=limit)
        entries = [DownstreamEntry(**r) for r in results]
        return DownstreamResponse(
            upstream_path=path,
            downstream=entries,
            total=len(entries),
        )
    except Exception as e:
        logger.error("get_downstream error: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to query downstream") from e


@router.get("/stale/query")
async def get_stale(
    path: str = Query(..., description="Upstream file path that changed"),
    current_version: int = Query(..., description="Current version of the upstream file"),
    current_content_id: str = Query(..., description="Current content hash of the upstream file"),
    lineage_and_zone: tuple[Any, str] = Depends(get_lineage_service),
    limit: int = Query(100, ge=1, le=1000, description="Max results"),
) -> StaleResponse:
    """Find downstream entities that are stale because upstream changed."""
    lineage_svc, zone_id = lineage_and_zone
    try:
        results = lineage_svc.check_staleness(
            path,
            current_version=current_version,
            current_content_id=current_content_id,
            zone_id=zone_id,
            limit=limit,
        )
        entries = [StaleEntry(**r) for r in results]
        return StaleResponse(
            upstream_path=path,
            current_version=current_version,
            current_content_id=current_content_id,
            stale=entries,
            total=len(entries),
        )
    except Exception as e:
        logger.error("get_stale error: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to check staleness") from e


@router.put("/{urn}")
async def put_lineage(
    body: PutLineageRequest,
    urn: str = Path(..., description="Entity URN of the output file"),
    lineage_and_zone: tuple[Any, str] = Depends(get_lineage_service),
) -> LineageResponse:
    """Explicitly declare lineage for an output file."""
    lineage_svc, zone_id = lineage_and_zone
    _verify_urn_zone(urn, zone_id)
    try:
        from nexus.contracts.aspects import LineageAspect

        upstream_dicts = [u.model_dump() for u in body.upstream]
        lineage = LineageAspect.from_explicit_declaration(
            upstream=upstream_dicts,
            agent_id=body.agent_id,
            agent_generation=body.agent_generation,
        )
        lineage_svc.record_lineage(entity_urn=urn, lineage=lineage, zone_id=zone_id)

        payload = lineage_svc.get_lineage(urn)
        return LineageResponse(entity_urn=urn, **(payload or {}))
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    except HTTPException:
        raise
    except Exception as e:
        logger.error("put_lineage error: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to put lineage") from e


@router.delete("/{urn}", status_code=204)
async def delete_lineage(
    urn: str = Path(..., description="Entity URN"),
    lineage_and_zone: tuple[Any, str] = Depends(get_lineage_service),
) -> None:
    """Delete lineage for an entity."""
    lineage_svc, zone_id = lineage_and_zone
    _verify_urn_zone(urn, zone_id)
    try:
        deleted = lineage_svc.delete_lineage(urn, zone_id=zone_id)
        if not deleted:
            raise HTTPException(status_code=404, detail=f"No lineage found for {urn}")
    except HTTPException:
        raise
    except Exception as e:
        logger.error("delete_lineage error: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to delete lineage") from e


# ---- Scope management endpoints ----


@router.post("/scope/begin")
async def begin_scope(body: ScopeRequest) -> ScopeResponse:
    """Begin a named lineage scope. Subsequent reads go into this scope.

    Agents call this before starting a task to isolate that task's reads
    from other reads in the same session. Each scope's reads are consumed
    independently when the agent writes.
    """
    from nexus.storage.session_read_accumulator import get_accumulator

    try:
        acc = get_accumulator()
        acc.begin_scope(body.agent_id, body.agent_generation, body.scope_id)
        return ScopeResponse(
            agent_id=body.agent_id,
            scope_id=body.scope_id,
            active_scope=body.scope_id,
            reads_count=0,
        )
    except Exception as e:
        logger.error("begin_scope error: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to begin scope") from e


@router.post("/scope/end")
async def end_scope(body: ScopeRequest) -> ScopeResponse:
    """End a named scope and discard unconsumed reads.

    The scope is removed. If it was active, reverts to default scope.
    Returns the number of reads that were in the scope.
    """
    from nexus.storage.session_read_accumulator import get_accumulator

    try:
        acc = get_accumulator()
        reads = acc.end_scope(body.agent_id, body.agent_generation, body.scope_id)
        active = acc.get_active_scope(body.agent_id, body.agent_generation)
        return ScopeResponse(
            agent_id=body.agent_id,
            scope_id=body.scope_id,
            active_scope=active,
            reads_count=len(reads),
        )
    except Exception as e:
        logger.error("end_scope error: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to end scope") from e


@router.get("/scope/active")
async def get_active_scope(
    agent_id: str = Query(..., description="Agent ID"),
    agent_generation: int | None = Query(None, description="Agent generation"),
) -> ScopeResponse:
    """Get the currently active lineage scope for an agent session."""
    from nexus.storage.session_read_accumulator import get_accumulator

    try:
        acc = get_accumulator()
        active = acc.get_active_scope(agent_id, agent_generation)
        count = acc.peek(agent_id, agent_generation, scope_id=active)
        return ScopeResponse(
            agent_id=agent_id,
            scope_id=active,
            active_scope=active,
            reads_count=count,
        )
    except Exception as e:
        logger.error("get_active_scope error: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to get active scope") from e
