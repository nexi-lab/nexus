"""``/api/v2/search/index-directory``, ``/indexed-dirs``, ``/indexing-mode``,
``/purge-unscoped`` — per-directory semantic-index scoping (Issue #3698).

Extracted from ``routers/search.py`` as part of the search-router split
that removed the 2000-line file-size exemption.  Underscore-prefixed
because ``search.py`` includes this module's ``router`` at module-load
time via ``include_router``; no other caller should import this module
directly.

Shared helpers (``_get_search_daemon``) come from the parent module.
"""

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from nexus.server.api.v2.routers.search import _get_search_daemon
from nexus.server.dependencies import require_auth

logger = logging.getLogger(__name__)

# Sub-router with NO prefix — parent ``search.router`` already carries
# ``/api/v2/search`` and includes this via ``include_router``.
router = APIRouter(tags=["search"])


async def _require_admin_or_path_write(
    request: Request,
    auth_result: dict[str, Any],
    zone_id: str,
    directory_path: str,
) -> None:
    """Policy gate for directory-scope mutation endpoints (Issue #3698 #6.7).

    Policy: admin bypass, otherwise require write permission on the target
    path via the (sync) ``permission_enforcer`` wired onto ``app.state``.
    If no enforcer is available, deny (fail-closed) — a deployment without
    a permission enforcer should be admin-only for mutation endpoints.
    """
    if auth_result.get("is_admin", False):
        return

    enforcer = getattr(request.app.state, "permission_enforcer", None)
    if enforcer is None:
        # Fail closed — no enforcer wired means non-admins cannot mutate
        # index scope. Admins already bypassed above.
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
        # PermissionEnforcer.check is sync — no await.
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
    """Register a directory for scoped semantic indexing (Issue #3698).

    Request body:
        {"path": "/zone/zone_a/project/src"}  -- or any canonical virtual path

    Policies (Issue #6):
    - Non-existent directories are ALLOWED (register for future use).
    - File paths (non-directory) are rejected — the daemon does not verify
      this today; v1 trusts the caller. Filesystem existence check is a
      follow-up.
    - Path escapes (``..``) → 400.
    - Missing zone → 404.
    - Duplicate registration: **idempotent recovery**. Instead of 409,
      we re-trigger the backfill so an operator who hit a previous
      backfill failure can retry by re-issuing the same POST. The
      response includes ``status: "already_registered"`` so the
      caller can distinguish from a fresh registration.

    Backfill outcomes (``backfill_status``):
    - ``ok``: backfill ran cleanly (``backfill_files`` may be 0 if
      the zone genuinely has no in-scope chunks yet).
    - ``skewed``: a concurrent scope mutation superseded this
      backfill. Response includes ``degraded: true``; operator should
      retry by re-issuing this POST.
    - ``failed``: hard failure reading or writing txtai. Metadata
      change is committed; response includes ``degraded: true`` and
      the error message. Operator can retry by re-issuing.
    - ``no_op``: daemon has no DB or backend (test scaffolding).
    """
    from nexus.bricks.search.index_scope import (
        DirectoryAlreadyRegisteredError,
        InvalidDirectoryPathError,
        ZoneNotFoundError,
    )
    from nexus.bricks.search.scope_ops import BackfillFailedError
    from nexus.contracts.constants import ROOT_ZONE_ID

    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="request body must be a JSON object")
    directory_path = payload.get("path")
    if not isinstance(directory_path, str) or not directory_path:
        raise HTTPException(status_code=400, detail="'path' field is required")

    zone_id = auth_result.get("zone_id") or ROOT_ZONE_ID

    await _require_admin_or_path_write(request, auth_result, zone_id, directory_path)

    canonical = directory_path
    status_label = "registered"
    backfill_status = "ok"
    backfill_files = 0
    backfill_error: str | None = None
    backfill_attempted = 0

    try:
        canonical, result = await search_daemon.add_indexed_directory(zone_id, directory_path)
        backfill_status = result.status
        backfill_files = result.files
    except ZoneNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except InvalidDirectoryPathError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except DirectoryAlreadyRegisteredError:
        # **Idempotent retry**: instead of 409, re-trigger backfill so
        # an operator who hit an earlier backfill failure can recover
        # by re-issuing the same POST. Mark the response so the caller
        # can distinguish from a fresh registration.
        from nexus.bricks.search.scope_ops import validate_directory_path

        canonical = validate_directory_path(directory_path)
        status_label = "already_registered"
        try:
            result = await search_daemon.rerun_backfill_for_directory(zone_id, directory_path)
            backfill_status = result.status
            backfill_files = result.files
        except BackfillFailedError as exc:
            backfill_status = "failed"
            backfill_error = str(exc)
            backfill_attempted = exc.files_attempted
            logger.warning(
                "rerun backfill for already-registered %s failed: %s",
                canonical,
                exc,
            )
    except BackfillFailedError as exc:
        # The metadata change committed BUT the backfill failed.
        # Recompute canonical from the input since the exception
        # doesn't carry it.
        from nexus.bricks.search.scope_ops import validate_directory_path

        try:
            canonical = validate_directory_path(directory_path)
        except Exception:
            canonical = directory_path
        backfill_status = "failed"
        backfill_error = str(exc)
        backfill_attempted = exc.files_attempted
        logger.warning(
            "register /index-directory %s succeeded but backfill failed: %s",
            canonical,
            exc,
        )

    response: dict[str, Any] = {
        "zone_id": zone_id,
        "path": canonical,
        "status": status_label,
        "backfill_status": backfill_status,
        "backfill_files": backfill_files,
    }
    if backfill_status == "failed":
        response["backfill_error"] = backfill_error
        response["backfill_attempted"] = backfill_attempted
        response["degraded"] = True
    elif backfill_status == "skewed":
        # Concurrent mutation superseded this backfill. The metadata
        # change is committed but historical content was not
        # backfilled. Operator should retry by re-issuing this POST.
        response["degraded"] = True
        response["backfill_hint"] = (
            "concurrent scope mutation superseded the backfill; re-issue this POST to retry"
        )
    return response


@router.delete("/index-directory")
async def unregister_indexed_directory(
    request: Request,
    payload: dict[str, Any],
    auth_result: dict[str, Any] = Depends(require_auth),
    search_daemon: Any = Depends(_get_search_daemon),
) -> dict[str, Any]:
    """Unregister a directory from scoped semantic indexing (Issue #3698).

    Returns 404 if the directory was not registered.

    After successful unregistration this endpoint runs
    ``purge_unscoped_embeddings`` for the zone so any txtai rows that
    were under the removed directory disappear from semantic search at
    the same instant. The canonical ``document_chunks`` rows are
    preserved (purge only touches derived txtai state) so a future
    re-registration or mode flip back to ``'all'`` can rebuild the
    semantic index from the existing chunk store.
    """
    from nexus.bricks.search.index_scope import (
        DirectoryNotRegisteredError,
        InvalidDirectoryPathError,
    )
    from nexus.contracts.constants import ROOT_ZONE_ID

    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="request body must be a JSON object")
    directory_path = payload.get("path")
    if not isinstance(directory_path, str) or not directory_path:
        raise HTTPException(status_code=400, detail="'path' field is required")

    zone_id = auth_result.get("zone_id") or ROOT_ZONE_ID

    await _require_admin_or_path_write(request, auth_result, zone_id, directory_path)

    try:
        canonical = await search_daemon.remove_indexed_directory(zone_id, directory_path)
    except DirectoryNotRegisteredError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except InvalidDirectoryPathError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # Auto-purge stale derived txtai rows so query results stop showing
    # the now-unscoped files immediately. The metadata change is the
    # source of truth and is already committed.
    #
    # **Fail-closed at the HTTP boundary**: if purge raises, return
    # 503 Service Unavailable so clients keying off HTTP status know
    # the request did NOT fully succeed and stale txtai data may
    # still be searchable. Returning 200 with ``degraded=true`` was
    # misleading — automation that only inspects status codes would
    # treat the de-scope as complete while txtai still served the
    # old data, which is a real data-exposure risk at this trust
    # boundary. The metadata change is NOT rolled back: the periodic
    # _scope_refresh_loop will retry the purge on its next tick, and
    # operators can also retry via /purge-unscoped explicitly.
    try:
        purged = await search_daemon.purge_unscoped_embeddings(zone_id)
    except Exception as exc:
        logger.warning(
            "auto-purge after unregister failed for zone %s; "
            "metadata committed, returning 503 so the caller retries",
            zone_id,
            exc_info=True,
        )
        raise HTTPException(
            status_code=503,
            detail={
                "error": "purge_failed",
                "message": str(exc),
                "zone_id": zone_id,
                "path": canonical,
                "metadata_committed": True,
                "retry_via": "/api/v2/search/purge-unscoped",
            },
        ) from exc

    return {
        "zone_id": zone_id,
        "path": canonical,
        "status": "unregistered",
        "purged": purged,
        "purge_status": "ok",
    }


@router.get("/indexed-dirs")
async def list_indexed_dirs(
    auth_result: dict[str, Any] = Depends(require_auth),
    search_daemon: Any = Depends(_get_search_daemon),
) -> dict[str, Any]:
    """List directories currently registered for scoped semantic indexing.

    **Admin-only**: returning the registered directory list verbatim
    leaks the prefix layout (e.g., customer / repo / project names
    embedded in paths) to anyone who can authenticate against the
    zone, even if they have no read permission on the prefixes
    themselves. The mutation endpoints already require admin or
    explicit ReBAC write; this read should match.

    Returns an empty list if no directories are registered for the
    zone (which, combined with zone mode 'all', means the zone
    indexes everything).
    """
    from nexus.contracts.constants import ROOT_ZONE_ID

    if not auth_result.get("is_admin", False):
        raise HTTPException(
            status_code=403,
            detail="indexed-dirs is admin-only (registered directory "
            "names can encode sensitive metadata)",
        )

    zone_id = auth_result.get("zone_id") or ROOT_ZONE_ID
    mode = search_daemon._zone_indexing_modes.get(zone_id, "all")
    directories = search_daemon.list_indexed_directories(zone_id)
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
    """Flip a zone between ``'all'`` and ``'scoped'`` indexing modes (Issue #3698).

    Admin-only. Takes effect immediately — the daemon updates its
    in-memory state under ``_refresh_lock`` alongside the DB write.

    When flipping a zone from ``'all'`` to ``'scoped'``, this endpoint
    also runs ``purge_unscoped_embeddings`` for that zone so previously
    embedded out-of-scope files become invisible to semantic search at
    the same instant as the metadata change. Without that, the API
    would claim "takes effect immediately" while stale txtai rows
    remained searchable until a separate ``/purge-unscoped`` call ran.

    Request body:
        {"mode": "all" | "scoped", "zone_id": "optional — defaults to caller's zone"}
    """
    from nexus.bricks.search.index_scope import (
        INDEX_MODE_ALL,
        INDEX_MODE_SCOPED,
        InvalidDirectoryPathError,
        ZoneNotFoundError,
    )
    from nexus.bricks.search.scope_ops import BackfillFailedError
    from nexus.contracts.constants import ROOT_ZONE_ID

    if not auth_result.get("is_admin", False):
        raise HTTPException(status_code=403, detail="set-indexing-mode is admin-only")

    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="request body must be a JSON object")
    mode = payload.get("mode")
    if not isinstance(mode, str) or not mode:
        raise HTTPException(status_code=400, detail="'mode' field is required")

    zone_id = payload.get("zone_id") or auth_result.get("zone_id") or ROOT_ZONE_ID

    backfill_status = "ok"
    backfill_files = 0
    backfill_error: str | None = None
    backfill_attempted = 0
    try:
        result = await search_daemon.set_zone_indexing_mode(zone_id, mode)
        if result is not None:
            backfill_status = result.status
            backfill_files = result.files
    except ZoneNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except InvalidDirectoryPathError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except BackfillFailedError as exc:
        # The mode flip from 'scoped' to 'all' committed BUT the
        # backfill of historical content failed. The metadata change
        # is irreversible (already persisted) so we surface a
        # degraded-state response. The next write or daemon restart
        # will pick up where backfill left off.
        backfill_status = "failed"
        backfill_error = str(exc)
        backfill_attempted = exc.files_attempted
        logger.warning(
            "indexing-mode flip on zone %s succeeded but backfill failed: %s",
            zone_id,
            exc,
        )

    purged: dict[str, int] | None = None
    if mode == INDEX_MODE_SCOPED:
        # Auto-purge stale derived rows so de-scoping is enforced
        # immediately at query time, not just at the next write.
        #
        # **Fail-closed at the HTTP boundary**: if purge raises,
        # return 503 so clients keying off HTTP status know the
        # request did NOT fully succeed and stale txtai data may
        # still be searchable. Returning 200 with ``degraded=true``
        # was misleading at this trust boundary. The metadata change
        # is NOT rolled back: the periodic _scope_refresh_loop will
        # retry the purge on its next tick, and operators can also
        # retry via /purge-unscoped explicitly.
        try:
            purged = await search_daemon.purge_unscoped_embeddings(zone_id)
        except Exception as exc:
            logger.warning(
                "auto-purge after mode=scoped flip failed for zone %s; "
                "metadata committed, returning 503 so the caller retries",
                zone_id,
                exc_info=True,
            )
            raise HTTPException(
                status_code=503,
                detail={
                    "error": "purge_failed",
                    "message": str(exc),
                    "zone_id": zone_id,
                    "indexing_mode": mode,
                    "metadata_committed": True,
                    "retry_via": "/api/v2/search/purge-unscoped",
                },
            ) from exc

    response: dict[str, Any] = {
        "zone_id": zone_id,
        "indexing_mode": mode,
        "status": "updated",
    }
    if mode == INDEX_MODE_ALL:
        response["backfill_status"] = backfill_status
        response["backfill_files"] = backfill_files
        if backfill_status == "failed":
            response["backfill_error"] = backfill_error
            response["backfill_attempted"] = backfill_attempted
            response["degraded"] = True
        elif backfill_status == "skewed":
            response["degraded"] = True
            response["backfill_hint"] = (
                "concurrent scope mutation superseded the backfill; re-issue this POST to retry"
            )
    if mode == INDEX_MODE_SCOPED:
        response["purge_status"] = "ok"
        if purged is not None:
            response["purged"] = purged
    return response


@router.post("/purge-unscoped")
async def purge_unscoped_embeddings(
    payload: dict[str, Any] | None = None,
    auth_result: dict[str, Any] = Depends(require_auth),
    search_daemon: Any = Depends(_get_search_daemon),
) -> dict[str, Any]:
    """Admin-only: purge derived embedding artifacts for files outside any scope.

    Destructive operation (Issue #3698). Call this after unregistering
    directories to clean up stale txtai sections/vectors for files that
    are no longer in scope. Only zones in ``'scoped'`` mode are affected.

    Only deletes **derived** txtai state (``sections``, ``vectors``,
    in-memory index). The canonical ``document_chunks`` table is
    preserved so a future mode-flip back to ``'all'`` can rebuild
    semantic search from existing chunks.

    Request body (optional):
        {"zone_id": "zone-name"}    # defaults to caller's zone

    Both this endpoint and ``/indexing-mode`` accept the same payload
    shape so an admin operating across zones doesn't accidentally
    purge the wrong one.
    """
    from nexus.contracts.constants import ROOT_ZONE_ID

    if not auth_result.get("is_admin", False):
        raise HTTPException(
            status_code=403,
            detail="purge-unscoped is admin-only",
        )

    body: dict[str, Any] = payload or {}
    zone_id = body.get("zone_id") or auth_result.get("zone_id") or ROOT_ZONE_ID
    counts = await search_daemon.purge_unscoped_embeddings(zone_id)
    return {
        "zone_id": zone_id,
        "purged": counts,
    }


