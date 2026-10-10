"""HTTP adapter for the Search host's per-path index status RPC."""

import time
from typing import Any

import grpc
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from nexus.bricks.search.search_auth import token_zone_filter_from_auth
from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.server.api.v2.error_handling import grpc_http_exception
from nexus.server.api.v2.routers._search_deps import _get_search_daemon
from nexus.server.dependencies import require_auth
from nexus.server.zone_execution import run_zone_scoped

router = APIRouter(tags=["search"])


class LocateRequest(BaseModel):
    """Identify one VFS path whose index state the caller wants to inspect."""

    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, pattern=r"^/")
    zone_id: str | None = Field(default=None, min_length=1)


@router.post("/locate")
async def search_locate(
    request: Request,
    payload: LocateRequest,
    auth_result: dict[str, Any] = Depends(require_auth),
    search_daemon: Any = Depends(_get_search_daemon),
) -> dict[str, Any]:
    """Return the owning host's authorized index status for one path."""
    if not search_daemon.is_initialized:
        raise HTTPException(status_code=503, detail="Search daemon is still initializing")
    zone_id = payload.zone_id or auth_result.get("zone_id") or ROOT_ZONE_ID
    readable_zones = token_zone_filter_from_auth(auth_result, root_zone_id=ROOT_ZONE_ID)
    if readable_zones is not None and zone_id not in readable_zones:
        raise HTTPException(
            status_code=403, detail=f"Token has no read permission for zone {zone_id}"
        )

    async def work() -> dict[str, Any]:
        start = time.perf_counter()
        try:
            status = await search_daemon.locate(payload.path, zone_id=zone_id)
        except grpc.RpcError as error:
            raise grpc_http_exception(error) from error
        return {**status, "elapsed_ms": round((time.perf_counter() - start) * 1000, 2)}

    return await run_zone_scoped(
        getattr(request.app.state, "zone_registry", None),
        zone_id if zone_id != ROOT_ZONE_ID else None,
        work,
    )
