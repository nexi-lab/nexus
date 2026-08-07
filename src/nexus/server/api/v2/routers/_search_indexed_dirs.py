"""``/api/v2/search/index-directory``, ``/indexed-dirs``, ``/indexing-mode``.

Per-zone indexed-directories registry + on/off/sandbox indexing mode,
served by the Rust ``nexus-search-plugin`` cdylib.  Underscore-prefixed
because ``search.py`` includes this module's ``router`` via
``include_router``; no other caller should import this module directly.

Shared helpers (``_get_search_daemon``) come from the parent module.
"""

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from nexus.server.api.v2.routers._search_deps import _get_search_daemon
from nexus.server.dependencies import require_auth

logger = logging.getLogger(__name__)

router = APIRouter(tags=["search"])


async def _require_admin_or_path_write(
    request: Request,
    auth_result: dict[str, Any],
    zone_id: str,
    directory_path: str,
) -> None:
    """Admin bypass, otherwise require write on the target path via the
    (sync) ``permission_enforcer`` wired onto ``app.state``.  No enforcer
    ⇒ deny (fail-closed) so a deployment without one is admin-only for
    mutation endpoints."""
    if auth_result.get("is_admin", False):
        return

    enforcer = getattr(request.app.state, "permission_enforcer", None)
    if enforcer is None:
        raise HTTPException(
            status_code=403,
            detail="index scope mutation requires admin privileges in this deployment",
        )

    from nexus.contracts.constants import ROOT_ZONE_ID
    from nexus.contracts.types import OperationContext, Permission

    ctx = OperationContext(
        user_id=auth_result.get("subject_id", ""),
        groups=auth_result.get("groups", []),
        zone_id=zone_id or ROOT_ZONE_ID,
        is_admin=False,
        subject_type=auth_result.get("subject_type", "user"),
        subject_id=auth_result.get("subject_id"),
    )
    try:
        allowed = bool(enforcer.check(directory_path, Permission.WRITE, ctx))
    except Exception as exc:
        logger.warning("ReBAC write check failed for %s: %s", directory_path, exc)
        raise HTTPException(status_code=500, detail="permission check failed") from exc

    if not allowed:
        raise HTTPException(
            status_code=403,
            detail=f"write permission required on {directory_path}",
        )


@router.post("/index-directory")
async def register_indexed_directory(
    request: Request,
    payload: dict[str, Any],
    auth_result: dict[str, Any] = Depends(require_auth),
    search_daemon: Any = Depends(_get_search_daemon),
) -> dict[str, Any]:
    """Register a directory for scoped semantic indexing.

    Body: ``{"path": "/zone/zone_a/project/src"}``.

    Idempotent: re-registering the same path returns ``status:
    "already_registered"``.  Path escapes (``..``) ⇒ 400; missing zone
    ⇒ 404 (raised as gRPC status by the plugin).
    """
    from nexus.contracts.constants import ROOT_ZONE_ID

    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="request body must be a JSON object")
    directory_path = payload.get("path")
    if not isinstance(directory_path, str) or not directory_path:
        raise HTTPException(status_code=400, detail="'path' field is required")

    zone_id = auth_result.get("zone_id") or ROOT_ZONE_ID
    await _require_admin_or_path_write(request, auth_result, zone_id, directory_path)

    result = await search_daemon.add_indexed_directory(zone_id, directory_path)
    return {
        "zone_id": zone_id,
        "path": directory_path,
        "status": "registered" if result["added"] else "already_registered",
    }


@router.delete("/index-directory")
async def unregister_indexed_directory(
    request: Request,
    payload: dict[str, Any],
    auth_result: dict[str, Any] = Depends(require_auth),
    search_daemon: Any = Depends(_get_search_daemon),
) -> dict[str, Any]:
    """Unregister a directory from the per-zone registry.

    Body: ``{"path": ...}``.  404 if the path was not registered.
    """
    from nexus.contracts.constants import ROOT_ZONE_ID

    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="request body must be a JSON object")
    directory_path = payload.get("path")
    if not isinstance(directory_path, str) or not directory_path:
        raise HTTPException(status_code=400, detail="'path' field is required")

    zone_id = auth_result.get("zone_id") or ROOT_ZONE_ID
    await _require_admin_or_path_write(request, auth_result, zone_id, directory_path)

    outcome = await search_daemon.remove_indexed_directory(zone_id, directory_path)
    if outcome == "not_found":
        raise HTTPException(
            status_code=404,
            detail=f"directory not registered for zone {zone_id}: {directory_path}",
        )
    return {
        "zone_id": zone_id,
        "path": directory_path,
        "status": "unregistered",
    }


@router.get("/indexed-dirs")
async def list_indexed_dirs(
    auth_result: dict[str, Any] = Depends(require_auth),
    search_daemon: Any = Depends(_get_search_daemon),
) -> dict[str, Any]:
    """Admin-only: list per-zone indexed directories + current indexing mode.

    Admin gate because the registered directory list encodes prefix
    layout (customer / repo / project names) that a mere zone-authenticated
    caller has no need to see.
    """
    from nexus.contracts.constants import ROOT_ZONE_ID

    if not auth_result.get("is_admin", False):
        raise HTTPException(
            status_code=403,
            detail="indexed-dirs is admin-only (registered directory "
            "names can encode sensitive metadata)",
        )

    zone_id = auth_result.get("zone_id") or ROOT_ZONE_ID
    modes = await search_daemon.get_zone_indexing_modes()
    mode = modes.get(zone_id, "on")
    directories = await search_daemon.list_indexed_directories(zone_id)
    return {
        "zone_id": zone_id,
        "indexing_mode": mode,
        "directories": directories,
    }


@router.post("/indexing-mode")
async def set_indexing_mode(
    payload: dict[str, Any],
    auth_result: dict[str, Any] = Depends(require_auth),
    search_daemon: Any = Depends(_get_search_daemon),
) -> dict[str, Any]:
    """Admin-only: flip a zone's indexing mode.

    Body: ``{"mode": "on" | "off" | "sandbox", "zone_id": ...}``.
    """
    from nexus.contracts.constants import ROOT_ZONE_ID

    if not auth_result.get("is_admin", False):
        raise HTTPException(status_code=403, detail="set-indexing-mode is admin-only")

    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="request body must be a JSON object")
    mode = payload.get("mode")
    if not isinstance(mode, str) or not mode:
        raise HTTPException(status_code=400, detail="'mode' field is required")

    zone_id = payload.get("zone_id") or auth_result.get("zone_id") or ROOT_ZONE_ID

    try:
        await search_daemon.set_zone_indexing_mode(zone_id, mode)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {
        "zone_id": zone_id,
        "indexing_mode": mode,
        "status": "updated",
    }
