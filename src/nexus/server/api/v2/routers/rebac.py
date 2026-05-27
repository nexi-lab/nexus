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
gate (standard admin pipeline only — ``NEXUS_APPROVALS_ADMIN_TOKEN`` is
NOT a fallback here; that token is scoped to the approvals gRPC
server only).

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


class _WildcardSemanticError(ValueError):
    """Raised when an operator supplies a wildcard shape the API can't
    enforce semantically (e.g. shell-style ``/*`` single-level glob —
    ReBAC has no first-level-only enforcement, so collapsing it to a
    directory tuple would silently broaden access to all descendants).
    """


def _normalize_file_object_id(object_namespace: str, object_id: str) -> str:
    """Issue #4239 (round-5 hardened): collapse RECURSIVE wildcard-style
    ``file`` object IDs to the directory path so existing ancestor-walk /
    directory-grant machinery grants every descendant.

    Accepted shapes:

    - ``/workspaces/ws1/**`` → ``/workspaces/ws1``  (recursive subtree)
    - ``/workspaces/ws1/``   → ``/workspaces/ws1``  (trailing slash)
    - ``/**``                → ``/``                (root grant)

    Rejected (round-5 review — codex HIGH):

    - ``/workspaces/ws1/*``  — shell ``/*`` is one-level-only, but the
      ReBAC enforcer inherits directory grants to ALL descendants. The
      previous code collapsed this to the same directory tuple as
      ``/workspaces/ws1/**``, silently broadening to the entire subtree
      (an authorization overgrant). Reject with a clear error so the
      operator picks ``/**`` (recursive) or lists exact paths.

    Only applies to ``object_namespace == "file"``; other namespaces
    (``approvals``, ``zone``, …) are passed through unchanged so a
    capability id literally containing ``*`` isn't mangled.

    Raises:
        _WildcardSemanticError: when the input uses an unsupported
        wildcard shape (currently any ``/*`` segment). Callers should
        translate to a 400 with the error's message.
    """
    if object_namespace != "file" or not object_id:
        return object_id

    # Reject ``/*`` (single-level) anywhere in the path — collapsing
    # it to a directory tuple would grant the whole subtree. Strip
    # every ``/**`` first so we don't false-positive on the recursive
    # form (``/**`` is fine: it IS recursive).
    if "/*" in object_id.replace("/**", ""):
        raise _WildcardSemanticError(
            f"object_id contains unsupported single-level glob '/*' in {object_id!r}. "
            "ReBAC has no first-level-only enforcement — use '/**' for a "
            "recursive subtree grant, or list exact paths."
        )

    out = object_id
    # Strip trailing ``/**`` suffixes (possibly repeated, e.g. ``/a/**``).
    while out.endswith("/**"):
        out = out[:-3]
    # Strip a trailing slash unless we've collapsed all the way to root.
    if len(out) > 1 and out.endswith("/"):
        out = out.rstrip("/")
    # An empty result means the user supplied ``/**`` at the root —
    # collapse to the canonical root grant.
    if not out:
        out = "/"
    return out


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
    # Issue #4239: normalize shell-style globs to a directory path so the
    # tuple inherits to descendants via the existing ancestor walk.
    # Round-5 review: ``/*`` (one-level-only) is rejected because the
    # enforcer can't honor it — collapsing it to a directory tuple would
    # silently broaden access to all descendants.
    try:
        normalized_object_id = _normalize_file_object_id(body.object_namespace, body.object_id)
    except _WildcardSemanticError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if normalized_object_id != body.object_id:
        logger.info(
            "[#4239] rebac tuple object_id normalized: %r -> %r "
            "(wildcard glob collapsed to directory grant)",
            body.object_id,
            normalized_object_id,
        )
    obj = (body.object_namespace, normalized_object_id)

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
        "object_id": normalized_object_id,
        "object_id_input": body.object_id,
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
    zone_id: str | None = None,
    _auth: dict[str, Any] = Depends(require_followup_admin),
) -> dict[str, Any]:
    """List ReBAC tuples matching optional filters (admin-only).

    Diagnostic surface — production callers use the gRPC permission
    check, not this endpoint. Returns up to whatever the manager
    returns; we don't paginate.

    Issue #4242: any subset of filters is honored — ``?subject_id=admin``
    alone is valid (operators debugging a permission denial want to
    grep by subject regardless of ``subject_type``).
    """
    rebac_manager = _resolve_rebac_manager(request)

    # Issue #4239: canonicalize the lookup surface so
    # ``?object_id=/workspaces/ws1/**`` finds tuples written via any of
    # the equivalent glob spellings. Only meaningful when an object_id
    # was actually provided. Round-5: ``/*`` is rejected with 400
    # (mirrors POST/DELETE).
    if object_id is not None and object_namespace is not None:
        try:
            object_id = _normalize_file_object_id(object_namespace, object_id)
        except _WildcardSemanticError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    tuples: list[dict[str, Any]] = rebac_manager.rebac_list_tuples(
        relation=relation,
        subject_type=subject_namespace,
        subject_id=subject_id,
        object_type=object_namespace,
        object_id=object_id,
        zone_id=zone_id,
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
    # Issue #4239: canonicalize so DELETE matches what POST stored.
    # Round-5: ``/*`` is rejected (mirrors POST/GET).
    try:
        normalized_object_id = _normalize_file_object_id(body.object_namespace, body.object_id)
    except _WildcardSemanticError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    obj = (body.object_namespace, normalized_object_id)

    # Round-10 review (codex HIGH): pass subject_relation through to
    # the manager so we delete EXACTLY the tuple shape the operator
    # asked for. Previously the manager collapsed subject to (type, id)
    # and ignored subject_relation — meaning a parallel
    # userset-as-subject tuple sharing (subject, relation, object,
    # zone) would also be deleted. ``body.subject_relation`` is None
    # for direct tuples (POST's default) and a string for usersets.
    # Either value routes through the manager's _UNSET-aware filter.
    matches: list[dict[str, Any]] = rebac_manager.rebac_list_tuples(
        subject=(body.subject_namespace, body.subject_id),
        relation=body.relation,
        object=obj,
        subject_relation=body.subject_relation,
        zone_id=body.zone_id,
    )

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
        "object_id": normalized_object_id,
        "object_id_input": body.object_id,
        "zone_id": body.zone_id,
    }
