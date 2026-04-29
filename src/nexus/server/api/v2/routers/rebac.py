"""ReBAC tuple-write REST API (Issue #3790 follow-up).

Thin HTTP wrapper around ``ReBACManager.rebac_write`` /
``rebac_list_tuples`` / ``rebac_delete`` so operators can grant generic
ReBAC tuples (e.g. ``(user, alice) -- read --> (approvals, global)``)
through a REST surface — and so the Issue #3790 follow-up E2E
(``test_b_rebac_capability_auth_pipeline``) can drive the full
ReBACCapabilityAuth happy-path from outside the daemon process.

Endpoints
---------
- ``POST   /api/v2/rebac/tuples``  — write a tuple
- ``GET    /api/v2/rebac/tuples``  — list matching tuples (diagnostic)
- ``DELETE /api/v2/rebac/tuples``  — delete a tuple by exact match

All endpoints are admin-only via the shared ``require_followup_admin``
gate (standard admin pipeline OR ``NEXUS_APPROVALS_ADMIN_TOKEN``).

This router is intentionally minimal. ``auth_keys.py`` already creates
file-scoped ReBAC tuples (direct_viewer/editor/owner on
``("file", path)``). This router exists to write the *generic*
non-file-scoped tuples that ReBACCapabilityAuth checks at the gRPC
boundary — file-grant semantics stay in ``auth_keys.py``.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from nexus.server.api.v2._admin_auth import require_followup_admin

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v2/rebac", tags=["rebac"])


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------


class TupleBody(BaseModel):
    """Body for ``POST`` / ``DELETE /api/v2/rebac/tuples``.

    Mirrors ``ReBACManager.rebac_write(...)`` arguments. ``zone_id`` is
    required to keep multi-zone isolation explicit at the REST surface
    (the manager defaults ``None`` to ``"root"`` internally, but we
    require an explicit value here so operators don't accidentally
    write to the wrong zone).
    """

    subject_namespace: str = Field(..., description="Subject type (e.g. 'user', 'agent')")
    subject_id: str = Field(..., description="Subject ID")
    relation: str = Field(..., description="Relation name (e.g. 'read', 'owner', 'member')")
    object_namespace: str = Field(..., description="Object type (e.g. 'approvals', 'file')")
    object_id: str = Field(..., description="Object ID (e.g. 'global', '/path/to/file')")
    zone_id: str = Field(..., description="Zone ID for multi-zone isolation")
    subject_relation: str | None = Field(
        default=None,
        description="Optional userset-as-subject relation (3-tuple form)",
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_rebac_manager(request: Request) -> Any:
    """Resolve the ReBACManager from app.state.

    Mirrors the helper in ``auth_keys.py`` for parity. Returns 503 when
    the brick is not wired (sandbox profile, ReBAC disabled).
    """
    rebac_manager = getattr(request.app.state, "rebac_manager", None)
    if rebac_manager is None:
        raise HTTPException(
            status_code=503,
            detail="ReBAC manager not available",
        )
    return rebac_manager


def _subject_tuple(body: TupleBody) -> tuple[str, str] | tuple[str, str, str]:
    """Build a 2-tuple or 3-tuple subject from the body."""
    if body.subject_relation is not None:
        return (body.subject_namespace, body.subject_id, body.subject_relation)
    return (body.subject_namespace, body.subject_id)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/tuples", status_code=201)
async def write_tuple(
    request: Request,
    body: TupleBody,
    _auth: dict[str, Any] = Depends(require_followup_admin),
) -> dict[str, Any]:
    """Write a ReBAC relationship tuple (admin-only).

    Returns the persisted tuple metadata (``tuple_id``, ``revision``,
    ``consistency_token``) plus the echoed input fields for client
    convenience.

    Validation errors from ``ReBACManager.rebac_write`` (invalid
    relation, malformed tuple shape, etc.) propagate as ``ValueError``
    → 400 via the global handler.
    """
    rebac_manager = _resolve_rebac_manager(request)
    subject = _subject_tuple(body)
    obj = (body.object_namespace, body.object_id)

    try:
        result = rebac_manager.rebac_write(
            subject=subject,
            relation=body.relation,
            object=obj,
            zone_id=body.zone_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {
        "tuple_id": result.tuple_id,
        "revision": result.revision,
        "consistency_token": result.consistency_token,
        "subject_namespace": body.subject_namespace,
        "subject_id": body.subject_id,
        "relation": body.relation,
        "object_namespace": body.object_namespace,
        "object_id": body.object_id,
        "zone_id": body.zone_id,
    }


@router.get("/tuples")
async def list_tuples(
    request: Request,
    subject_namespace: str | None = None,
    subject_id: str | None = None,
    relation: str | None = None,
    object_namespace: str | None = None,
    object_id: str | None = None,
    _auth: dict[str, Any] = Depends(require_followup_admin),
) -> dict[str, Any]:
    """List ReBAC tuples matching optional filters (admin-only).

    Diagnostic surface — production callers use the gRPC permission
    check, not this endpoint. Returns up to whatever the manager
    returns; we don't paginate.
    """
    rebac_manager = _resolve_rebac_manager(request)

    subject: tuple[str, str] | None = None
    if subject_namespace is not None and subject_id is not None:
        subject = (subject_namespace, subject_id)
    elif subject_namespace is not None or subject_id is not None:
        raise HTTPException(
            status_code=400,
            detail="subject_namespace and subject_id must be provided together",
        )

    obj: tuple[str, str] | None = None
    if object_namespace is not None and object_id is not None:
        obj = (object_namespace, object_id)
    elif object_namespace is not None or object_id is not None:
        raise HTTPException(
            status_code=400,
            detail="object_namespace and object_id must be provided together",
        )

    tuples: list[dict[str, Any]] = rebac_manager.rebac_list_tuples(
        subject=subject,
        relation=relation,
        object=obj,
    )
    return {"tuples": tuples, "count": len(tuples)}


@router.delete("/tuples")
async def delete_tuple(
    request: Request,
    body: TupleBody,
    _auth: dict[str, Any] = Depends(require_followup_admin),
) -> dict[str, Any]:
    """Delete a ReBAC tuple by exact match (admin-only).

    Looks up the matching tuple_id via ``rebac_list_tuples`` then calls
    ``rebac_delete``. Returns ``{"deleted": N}`` so callers can detect
    the no-op case (tuple did not exist) without a separate GET.
    """
    rebac_manager = _resolve_rebac_manager(request)
    # Subject lookup uses the 2-tuple form (rebac_list_tuples doesn't
    # filter on subject_relation); the 3-tuple is only relevant for
    # writes. The zone_id filter below disambiguates same-shape tuples
    # across zones.
    obj = (body.object_namespace, body.object_id)

    matches: list[dict[str, Any]] = rebac_manager.rebac_list_tuples(
        subject=(body.subject_namespace, body.subject_id),
        relation=body.relation,
        object=obj,
    )
    # Filter to the requested zone — list_tuples doesn't zone-filter, so
    # we do it here to avoid deleting a same-shape tuple in another zone.
    matches = [t for t in matches if t.get("zone_id") == body.zone_id]

    deleted = 0
    for t in matches:
        tid = t.get("tuple_id")
        if tid and rebac_manager.rebac_delete(tid):
            deleted += 1

    return {
        "deleted": deleted,
        "subject_namespace": body.subject_namespace,
        "subject_id": body.subject_id,
        "relation": body.relation,
        "object_namespace": body.object_namespace,
        "object_id": body.object_id,
        "zone_id": body.zone_id,
    }
