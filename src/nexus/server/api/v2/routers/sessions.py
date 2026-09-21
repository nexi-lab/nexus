"""P1a session/runtime routes (SW-20260915-002 §8.9; ADR-001 §4.2 surface).

POST /v2/sessions                    create (home zone resolved by policy)
GET  /v2/sessions/{session_id}       read (home zone immutable)
POST /v2/sessions/{sid}/records      home-zone record write (5 kinds, real I/O)
GET  /v2/sessions/{sid}/records      routing ledger (zone + path + bytes)
POST /v2/runtime/start               register run (execution zone solidified)
POST /v2/runtime/resume              new pid under the same session
GET  /v2/runtime/runs/{pid}          run descriptor
POST /v2/runtime/runs/{pid}/cancel   terminate or park revocation_pending
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from nexus.server.api.v2.zone_security import principal_dict, require_zone_capability
from nexus.server.dependencies import require_auth
from nexus.services.zones.session_runtime import SessionRuntimeError, SessionRuntimeService

router = APIRouter(prefix="/v2", tags=["sessions-runtime-v2"])


def _service(request: Request) -> SessionRuntimeService:
    svc = getattr(request.app.state, "session_runtime_service", None)
    if svc is None:
        raise HTTPException(
            status_code=503, detail={"code": "SESSION_RUNTIME_UNAVAILABLE", "retryable": False}
        )
    assert isinstance(svc, SessionRuntimeService)
    return svc


def _svc_error(exc: SessionRuntimeError) -> HTTPException:
    return HTTPException(
        status_code=exc.status_code,
        detail={"code": exc.code, "message": exc.message, "retryable": False},
    )


@router.post("/sessions", status_code=201)
def create_session(
    request: Request,
    body: dict[str, Any],
    auth_result: dict[str, Any] = Depends(require_auth),
) -> dict[str, Any]:
    svc = _service(request)
    session_id = str(body.get("session_id") or "").strip()
    home_zone_id = str(body.get("home_zone_id") or "").strip()
    if not session_id or not home_zone_id:
        raise HTTPException(status_code=422, detail={"code": "MISSING_FIELDS", "retryable": False})

    # The ingress policy check: the caller must hold zone.data.write on the
    # named home zone. A client payload alone never establishes authority.
    def capability_check(zone_id: str) -> bool:
        try:
            require_zone_capability(
                request, auth_result, zone_id=zone_id, capability="zone.data.write"
            )
            return True
        except HTTPException:
            return False

    try:
        view = svc.create_session(
            session_id=session_id,
            home_zone_id=home_zone_id,
            owner=body.get("owner") or principal_dict(auth_result),
            created_by=principal_dict(auth_result),
            policy_version=str(body.get("policy_version") or "p1a-default"),
            capability_check=capability_check,
        )
    except SessionRuntimeError as exc:
        raise _svc_error(exc) from exc
    return view.as_json()


@router.get("/sessions/{session_id}")
def get_session(
    session_id: str, request: Request, _auth: dict[str, Any] = Depends(require_auth)
) -> dict[str, Any]:
    try:
        return _service(request).get_session(session_id).as_json()
    except SessionRuntimeError as exc:
        raise _svc_error(exc) from exc


@router.post("/sessions/{session_id}/records", status_code=201)
def write_record(
    session_id: str,
    request: Request,
    body: dict[str, Any],
    _auth: dict[str, Any] = Depends(require_auth),
) -> dict[str, Any]:
    svc = _service(request)
    record_kind = str(body.get("record_kind") or "").strip()
    data = str(body.get("data") or "")
    record_name = str(body.get("record_name") or "default")
    zone_hint = body.get("zone_id")
    try:
        return svc.write_session_record(
            session_id=session_id,
            record_kind=record_kind,
            payload=data.encode("utf-8"),
            record_name=record_name,
            zone_hint=str(zone_hint) if zone_hint is not None else None,
        )
    except SessionRuntimeError as exc:
        raise _svc_error(exc) from exc


@router.get("/sessions/{session_id}/records")
def list_records(
    session_id: str,
    request: Request,
    _auth: dict[str, Any] = Depends(require_auth),
) -> dict[str, Any]:
    try:
        svc = _service(request)
        svc.get_session(session_id)  # 404 when the session does not exist
        return {"records": svc.record_ledger(session_id)}
    except SessionRuntimeError as exc:
        raise _svc_error(exc) from exc


def _start(request: Request, body: dict[str, Any], auth_result: dict[str, Any]) -> dict[str, Any]:
    svc = _service(request)
    pid = str(body.get("pid") or "").strip()
    session_id = str(body.get("session_id") or "").strip()
    if not pid or not session_id:
        raise HTTPException(status_code=422, detail={"code": "MISSING_FIELDS", "retryable": False})

    def zone_active_check(zone_id: str) -> bool:
        try:
            require_zone_capability(
                request, auth_result, zone_id=zone_id, capability="zone.runtime.execute"
            )
            return True
        except HTTPException:
            return False

    try:
        view = svc.start_run(
            pid=pid,
            session_id=session_id,
            execution_zone_hint=body.get("execution_zone_id") or None,
            delegation_ref=body.get("delegation_ref") or None,
            grant_ref=body.get("grant_ref") or None,
            authorization_epoch=body.get("authorization_epoch"),
            decision_reason=body.get("decision_reason") or None,
            policy_version=body.get("policy_version") or None,
            zone_active_check=zone_active_check,
        )
    except SessionRuntimeError as exc:
        raise _svc_error(exc) from exc
    return view.as_json()


@router.post("/runtime/start", status_code=201)
def runtime_start(
    request: Request,
    body: dict[str, Any],
    auth_result: dict[str, Any] = Depends(require_auth),
) -> dict[str, Any]:
    return _start(request, body, auth_result)


@router.post("/runtime/resume", status_code=201)
def runtime_resume(
    request: Request,
    body: dict[str, Any],
    auth_result: dict[str, Any] = Depends(require_auth),
) -> dict[str, Any]:
    """Resume = a fresh pid under the same session (ADR-001 §4.2). The
    caller supplies the new pid; everything else mirrors start."""
    return _start(request, body, auth_result)


@router.get("/runtime/runs/{pid}")
def get_run(
    pid: str, request: Request, _auth: dict[str, Any] = Depends(require_auth)
) -> dict[str, Any]:
    try:
        return _service(request).get_run(pid).as_json()
    except SessionRuntimeError as exc:
        raise _svc_error(exc) from exc


@router.post("/runtime/runs/{pid}/cancel")
def cancel_run(
    pid: str,
    request: Request,
    body: dict[str, Any],
    response: Response,
    _auth: dict[str, Any] = Depends(require_auth),
) -> dict[str, Any]:
    mode = str(body.get("mode") or "terminate")
    try:
        view = _service(request).cancel_run(pid=pid, mode=mode)
    except SessionRuntimeError as exc:
        raise _svc_error(exc) from exc
    response.headers["X-Runtime-State"] = view.state
    return view.as_json()
