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

import logging
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StrictStr, ValidationError

from nexus.contracts.exceptions import InvalidPathError
from nexus.contracts.zone_v1 import ResourceRef
from nexus.server.api.v2.zone_security import (
    VerifiedZoneDelegation,
    principal_dict,
    require_runtime_delegation,
    require_zone_capability,
)
from nexus.server.dependencies import require_auth
from nexus.services.zones.session_runtime import SessionRuntimeError, SessionRuntimeService
from nexus.services.zones.session_tasks import SessionTaskError, SessionTaskService

logger = logging.getLogger(__name__)


def _client_component(value: str) -> str:
    """Reject path-bearing / traversal / control-character client strings
    before they are ever composed into a VFS path (M-17)."""
    if "/" in value or ".." in value or any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise ValueError("client strings must not contain '/', '..' or control characters")
    return value


#: session/pid identifiers: single path-free component, column-width bounded.
SessionIdStr = Annotated[str, Field(max_length=64), AfterValidator(_client_component)]
PidStr = Annotated[str, Field(max_length=64), AfterValidator(_client_component)]
#: record names additionally bound by the ledger column width.
RecordNameStr = Annotated[str, Field(max_length=256), AfterValidator(_client_component)]


class SessionCreateBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    session_id: SessionIdStr
    home_zone_id: str = Field(min_length=1, max_length=64)
    owner: dict[str, Any] | None = None
    policy_version: str | None = None


class WriteRecordBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    record_kind: Literal["session", "transcript", "context", "artifact", "verify"]
    data: StrictStr
    record_name: RecordNameStr = "default"
    zone_id: str | None = None


class StartBody(BaseModel):
    # extra="allow": _start consumes free-form pass-through fields off the
    # dumped dict (delegation_ref / resource_refs / execution_zone_id / …);
    # "ignore" would silently strip them and break every start request.
    model_config = ConfigDict(extra="allow")

    pid: PidStr
    session_id: SessionIdStr


class CancelRunBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    mode: str = "terminate"


router = APIRouter(prefix="/v2", tags=["sessions-runtime-v2"])


def _service(request: Request) -> SessionRuntimeService:
    svc = getattr(request.app.state, "session_runtime_service", None)
    if svc is None:
        raise HTTPException(
            status_code=503, detail={"code": "SESSION_RUNTIME_UNAVAILABLE", "retryable": False}
        )
    assert isinstance(svc, SessionRuntimeService)
    return svc


def _task_service(request: Request) -> SessionTaskService:
    svc = getattr(request.app.state, "session_task_service", None)
    if svc is None:
        raise HTTPException(
            status_code=503, detail={"code": "SESSION_TASK_UNAVAILABLE", "retryable": False}
        )
    assert isinstance(svc, SessionTaskService)
    return svc


def _svc_error(exc: SessionRuntimeError) -> HTTPException:
    # retryable is carried by the error itself (M-18): a 503-class
    # ZONE_RUNTIME_UNAVAILABLE must be retryable, matching zone_security.
    return HTTPException(
        status_code=exc.status_code,
        detail={"code": exc.code, "message": exc.message, "retryable": exc.retryable},
    )


def _task_svc_error(exc: SessionTaskError) -> HTTPException:
    return HTTPException(
        status_code=exc.status_code,
        detail={"code": exc.code, "message": exc.message, "retryable": exc.retryable},
    )


def _runtime_delegation_ref(request: Request, body: dict[str, Any]) -> str:
    body_ref = str(body.get("delegation_ref") or "").strip()
    header_ref = str(request.headers.get("X-Nexus-Zone-Delegation") or "").strip()
    if body_ref and header_ref and body_ref != header_ref:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "RESOURCE_RELATION_DENIED",
                "message": "runtime delegation header/body mismatch",
                "retryable": False,
            },
        )
    return header_ref or body_ref


def _requester(auth_result: dict[str, Any]) -> dict[str, Any]:
    principal = principal_dict(auth_result)
    return {
        key: principal[key]
        for key in ("subject_type", "subject_id", "trust_domain")
        if principal.get(key) is not None
    }


def _resource_refs(body: dict[str, Any]) -> list[dict[str, Any]]:
    raw = body.get("resource_refs", [])
    if not isinstance(raw, list):
        raise HTTPException(
            status_code=422,
            detail={"code": "INVALID_TASK_SPEC", "message": "resource_refs must be a list"},
        )
    try:
        return [
            ResourceRef.model_validate(item).model_dump(mode="json", exclude_none=True)
            for item in raw
        ]
    except ValidationError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "INVALID_TASK_SPEC", "message": str(exc)},
        ) from exc


def _minimum_task_write_gate(
    request: Request,
    auth_result: dict[str, Any],
    *,
    home_zone_id: str,
    execution_zone_id: str,
    delegation_ref: str,
    session_id: str,
) -> VerifiedZoneDelegation | None:
    if auth_result.get("is_admin", False):
        require_zone_capability(
            request,
            auth_result,
            zone_id=home_zone_id,
            capability="zone.data.write",
            resource_path=f"/sessions/{session_id}",
        )
        return None
    return require_runtime_delegation(
        request,
        auth_result,
        delegation_id=delegation_ref,
        zone_id=execution_zone_id,
        capability="zone.runtime.execute",
        resource_path=f"/sessions/{session_id}",
    )


def _require_runtime_access(
    request: Request,
    auth_result: dict[str, Any],
    *,
    zone_id: str,
    capability: str,
    resource_path: str,
) -> None:
    """Authorize an admin control-plane call or a delegated user access."""
    if auth_result.get("is_admin", False):
        require_zone_capability(
            request,
            auth_result,
            zone_id=zone_id,
            capability=capability,
            resource_path=resource_path,
        )
        return
    require_runtime_delegation(
        request,
        auth_result,
        delegation_id=str(request.headers.get("X-Nexus-Zone-Delegation") or ""),
        zone_id=zone_id,
        capability=capability,
        resource_path=resource_path,
    )


def _require_runtime_access_or_not_found(
    request: Request,
    auth_result: dict[str, Any],
    *,
    not_found_code: str,
    zone_id: str,
    capability: str,
    resource_path: str,
) -> None:
    """Anti-enumeration (L-8③) on the sessions read surface: an access
    denial answers with the same 404 shape as the resource not existing,
    mirroring the zones surface's authorize-before-lookup order."""
    try:
        _require_runtime_access(
            request,
            auth_result,
            zone_id=zone_id,
            capability=capability,
            resource_path=resource_path,
        )
    except HTTPException as exc:
        if exc.status_code == 403:
            raise HTTPException(
                status_code=404,
                detail={"code": not_found_code, "message": "not found", "retryable": False},
            ) from exc
        raise


def _require_zone_alive(request: Request, zone_id: str) -> None:
    """§5.6 defense in depth: a deprovisioned zone's data is unreadable.

    The authorization helpers above intentionally answer "who may read";
    lifecycle is a separate axis — an admin credential must not read back
    business rows of a zone whose deletion the same control plane
    completed. Management surfaces (tombstone/operation queries) do not
    pass through here.
    """
    factory = getattr(request.app.state, "zone_session_factory", None)
    if factory is None:
        raise HTTPException(status_code=503, detail="zone store unavailable")
    from nexus.storage.models.auth import ZoneModel

    with factory() as session:
        zone = session.get(ZoneModel, zone_id)
        if zone is not None and zone.canonical_status == "deleted":
            raise HTTPException(
                status_code=404,
                detail={
                    "code": "ZONE_DELETED",
                    "message": "zone data has been deprovisioned",
                    "retryable": False,
                },
            )


@router.post("/sessions", status_code=201)
def create_session(
    request: Request,
    body: SessionCreateBody,
    auth_result: dict[str, Any] = Depends(require_auth),
) -> dict[str, Any]:
    svc = _service(request)
    session_id = body.session_id
    home_zone_id = body.home_zone_id

    # The ingress policy check: the caller must hold zone.data.write on the
    # named home zone. A client payload alone never establishes authority.
    def capability_check(zone_id: str) -> bool:
        try:
            require_zone_capability(
                request,
                auth_result,
                zone_id=zone_id,
                capability="zone.data.write",
                resource_path=f"/sessions/{session_id}",
            )
            return True
        except HTTPException:
            return False

    try:
        view = svc.create_session(
            session_id=session_id,
            home_zone_id=home_zone_id,
            owner=body.owner or principal_dict(auth_result),
            created_by=principal_dict(auth_result),
            policy_version=body.policy_version or "p1a-default",
            capability_check=capability_check,
        )
    except SessionRuntimeError as exc:
        raise _svc_error(exc) from exc
    return view.as_json()


@router.get("/sessions/{session_id}")
def get_session(
    session_id: str, request: Request, auth_result: dict[str, Any] = Depends(require_auth)
) -> dict[str, Any]:
    try:
        view = _service(request).get_session(session_id)
        _require_zone_alive(request, view.home_zone_id)
        _require_runtime_access_or_not_found(
            request,
            auth_result,
            not_found_code="SESSION_NOT_FOUND",
            zone_id=view.home_zone_id,
            capability="zone.data.read",
            resource_path=f"/sessions/{session_id}",
        )
        return view.as_json()
    except SessionRuntimeError as exc:
        raise _svc_error(exc) from exc


@router.post("/sessions/{session_id}/records", status_code=201)
def write_record(
    session_id: str,
    request: Request,
    body: WriteRecordBody,
    auth_result: dict[str, Any] = Depends(require_auth),
) -> dict[str, Any]:
    svc = _service(request)
    try:
        view = svc.get_session(session_id)
        _require_zone_alive(request, view.home_zone_id)
        _require_runtime_access(
            request,
            auth_result,
            zone_id=view.home_zone_id,
            capability="zone.data.write",
            resource_path=f"/sessions/{session_id}",
        )
        return svc.write_session_record(
            session_id=session_id,
            record_kind=body.record_kind,
            payload=body.data.encode("utf-8"),
            record_name=body.record_name,
            zone_hint=body.zone_id,
        )
    except SessionRuntimeError as exc:
        raise _svc_error(exc) from exc
    except InvalidPathError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "INVALID_PATH", "message": str(exc), "retryable": False},
        ) from exc


@router.get("/sessions/{session_id}/records")
def list_records(
    session_id: str,
    request: Request,
    auth_result: dict[str, Any] = Depends(require_auth),
) -> dict[str, Any]:
    try:
        svc = _service(request)
        view = svc.get_session(session_id)  # 404 when the session does not exist
        _require_zone_alive(request, view.home_zone_id)
        _require_runtime_access_or_not_found(
            request,
            auth_result,
            not_found_code="SESSION_NOT_FOUND",
            zone_id=view.home_zone_id,
            capability="zone.data.read",
            resource_path=f"/sessions/{session_id}",
        )
        return {"records": svc.record_ledger(session_id)}
    except SessionRuntimeError as exc:
        raise _svc_error(exc) from exc


def _start(
    request: Request,
    body: StartBody,
    auth_result: dict[str, Any],
    *,
    resume: bool = False,
) -> dict[str, Any]:
    svc = _service(request)
    task_svc = _task_service(request)
    pid = body.pid
    session_id = body.session_id
    # The body model validated the client strings (M-17); the remaining
    # free-form fields are consumed below via the dict view.
    payload = body.model_dump()

    try:
        session_view = svc.get_session(session_id)
        execution_zone = (
            svc.resume_zone_of(session_id)
            if resume
            else str(payload.get("execution_zone_id") or session_view.home_zone_id)
        )
    except SessionRuntimeError as exc:
        raise _svc_error(exc) from exc
    delegation_ref = _runtime_delegation_ref(request, payload)
    resource_refs = _resource_refs(payload)
    requested_execution_zone = payload.get("execution_zone_id")
    if resume and requested_execution_zone and str(requested_execution_zone) != execution_zone:
        raise HTTPException(
            status_code=409,
            detail={"code": "ZONE_IDENTITY_DRIFT", "retryable": False},
        )
    if (
        not resume
        and execution_zone != session_view.home_zone_id
        and (not payload.get("decision_reason") or not payload.get("policy_version"))
    ):
        _minimum_task_write_gate(
            request,
            auth_result,
            home_zone_id=session_view.home_zone_id,
            execution_zone_id=execution_zone,
            delegation_ref=delegation_ref,
            session_id=session_id,
        )
        try:
            task = task_svc.ensure_implicit_task(
                session_id=session_id,
                requested_by=_requester(auth_result),
                resource_refs=resource_refs,
            )
            task_svc.reject(
                task_id=task.task_id,
                reason_code="INVALID_TASK_SPEC",
                reason="cross-zone execution requires decision_reason and policy_version",
                policy_version=str(payload.get("policy_version") or task.policy_version),
            )
        except SessionTaskError as exc:
            raise _task_svc_error(exc) from exc
        raise HTTPException(
            status_code=422,
            detail={"code": "CROSS_ZONE_DECISION_REQUIRED", "retryable": False},
        )

    verified = require_runtime_delegation(
        request,
        auth_result,
        delegation_id=delegation_ref,
        zone_id=execution_zone,
        capability="zone.runtime.execute",
        resource_path=f"/sessions/{session_id}",
    )
    if payload.get("grant_ref") not in (None, verified.grant_id) or payload.get(
        "authorization_epoch"
    ) not in (None, verified.authorization_epoch):
        raise HTTPException(
            status_code=403,
            detail={
                "code": "RESOURCE_RELATION_DENIED",
                "message": "runtime grant/epoch references are owned by Nexus",
                "retryable": False,
            },
        )

    def zone_active_check(zone_id: str) -> bool:
        try:
            require_zone_capability(
                request, auth_result, zone_id=zone_id, capability="zone.runtime.execute"
            )
            return True
        except HTTPException:
            return False

    attempt: Any = None  # bound only after create_attempt commits (M-11)
    try:
        if resume:
            latest_attempt = task_svc.latest_attempt(session_id=session_id)
            if latest_attempt is not None and latest_attempt.state in {
                "completed",
                "failed",
                "cancelled",
            }:
                raise SessionTaskError(
                    "ATTEMPT_NOT_ACTIVE",
                    f"attempt {latest_attempt.attempt_id} is {latest_attempt.state}",
                    409,
                )
            view = svc.resume_run(
                pid=pid,
                session_id=session_id,
                execution_zone_hint=str(requested_execution_zone)
                if requested_execution_zone
                else None,
                delegation_ref=verified.delegation_id,
                grant_ref=verified.grant_id,
                authorization_epoch=verified.authorization_epoch,
                attempt_id=latest_attempt.attempt_id if latest_attempt is not None else None,
                zone_active_check=zone_active_check,
            )
            if latest_attempt is not None:
                task_svc.attach_pid(attempt_id=latest_attempt.attempt_id, pid=pid)
        else:
            task = task_svc.ensure_implicit_task(
                session_id=session_id,
                requested_by=_requester(auth_result),
                resource_refs=resource_refs,
            )
            for resource_ref in resource_refs:
                try:
                    if auth_result.get("is_admin", False):
                        require_zone_capability(
                            request,
                            auth_result,
                            zone_id=str(resource_ref["zone_id"]),
                            capability="zone.data.read",
                            resource_path=str(resource_ref["path"]),
                        )
                    else:
                        require_runtime_delegation(
                            request,
                            auth_result,
                            delegation_id=verified.delegation_id,
                            zone_id=str(resource_ref["zone_id"]),
                            capability="zone.data.read",
                            resource_path=str(resource_ref["path"]),
                        )
                except HTTPException as exc:
                    task_svc.reject(
                        task_id=task.task_id,
                        reason_code="ZONE_ACCESS_DENIED",
                        reason="a declared resource reference is not accessible",
                        policy_version=str(body.get("policy_version") or task.policy_version),
                    )
                    raise HTTPException(
                        status_code=403,
                        detail={
                            "code": "ZONE_ACCESS_DENIED",
                            "message": "a declared resource reference is not accessible",
                            "retryable": False,
                        },
                    ) from exc

            cross_zone = execution_zone != session_view.home_zone_id
            attempt = task_svc.create_attempt(
                task_id=task.task_id,
                execution_zone_id=execution_zone,
                reason_code="CROSS_ZONE_POLICY_ACCEPTED" if cross_zone else "HOME_ZONE_DEFAULT",
                reason=str(body.get("decision_reason") or "session home zone default"),
                policy_version=str(body.get("policy_version") or task.policy_version),
            )
            try:
                view = svc.start_run(
                    pid=pid,
                    session_id=session_id,
                    execution_zone_hint=str(requested_execution_zone)
                    if requested_execution_zone
                    else None,
                    delegation_ref=verified.delegation_id,
                    grant_ref=verified.grant_id,
                    authorization_epoch=verified.authorization_epoch,
                    decision_reason=body.get("decision_reason") or None,
                    policy_version=body.get("policy_version") or None,
                    attempt_id=attempt.attempt_id,
                    zone_active_check=zone_active_check,
                )
                task_svc.attach_pid(attempt_id=attempt.attempt_id, pid=pid)
            except SessionRuntimeError as exc:
                task_svc.mark_failed(attempt_id=attempt.attempt_id, error=exc)
                raise
    except SessionTaskError as exc:
        raise _task_svc_error(exc) from exc
    except SessionRuntimeError as exc:
        raise _svc_error(exc) from exc
    except Exception:
        # Non-domain failure (DB hiccup, process-level error): a committed
        # attempt must not strand as a forever-queued orphan.  Guarded on
        # `attempt is not None` — create_attempt's own failures raise
        # SessionTaskError above, before any attempt exists (M-11).
        if attempt is not None:
            try:
                task_svc.mark_failed(
                    attempt_id=attempt.attempt_id,
                    error=SessionRuntimeError("START_INTERRUPTED", "start interrupted", 500),
                )
            except Exception:
                logger.exception(
                    "failed to mark attempt %s after a non-domain start failure",
                    attempt.attempt_id,
                )
        raise
    return view.as_json()


@router.get("/sessions/{session_id}/tasks/{task_id}")
def get_task(
    session_id: str,
    task_id: str,
    request: Request,
    auth_result: dict[str, Any] = Depends(require_auth),
) -> dict[str, Any]:
    try:
        payload = _task_service(request).get_task(session_id=session_id, task_id=task_id)
        _require_zone_alive(request, str(payload["spec"]["storage"]["zone_id"]))
        _require_runtime_access_or_not_found(
            request,
            auth_result,
            not_found_code="TASK_NOT_FOUND",
            zone_id=str(payload["spec"]["storage"]["zone_id"]),
            capability="zone.data.read",
            resource_path=f"/sessions/{session_id}",
        )
        return payload
    except SessionTaskError as exc:
        raise _task_svc_error(exc) from exc


@router.post("/runtime/start", status_code=201)
def runtime_start(
    request: Request,
    body: StartBody,
    auth_result: dict[str, Any] = Depends(require_auth),
) -> dict[str, Any]:
    return _start(request, body, auth_result)


@router.post("/runtime/resume", status_code=201)
def runtime_resume(
    request: Request,
    body: StartBody,
    auth_result: dict[str, Any] = Depends(require_auth),
) -> dict[str, Any]:
    """Resume = a fresh pid under the same session (ADR-001 §4.2). The
    caller supplies the new pid; everything else mirrors start."""
    return _start(request, body, auth_result, resume=True)


@router.get("/runtime/runs/{pid}")
def get_run(
    pid: str, request: Request, auth_result: dict[str, Any] = Depends(require_auth)
) -> dict[str, Any]:
    try:
        view = _service(request).get_run(pid)
        _require_zone_alive(request, view.execution_zone_id)
        _require_runtime_access_or_not_found(
            request,
            auth_result,
            not_found_code="RUN_NOT_FOUND",
            zone_id=view.execution_zone_id,
            capability="zone.runtime.execute",
            resource_path=f"/sessions/{view.session_id}",
        )
        return view.as_json()
    except SessionRuntimeError as exc:
        raise _svc_error(exc) from exc


@router.post("/runtime/runs/{pid}/cancel")
def cancel_run(
    pid: str,
    request: Request,
    body: CancelRunBody,
    response: Response,
    auth_result: dict[str, Any] = Depends(require_auth),
) -> dict[str, Any]:
    mode = body.mode
    try:
        svc = _service(request)
        current = svc.get_run(pid)
        _require_runtime_access(
            request,
            auth_result,
            zone_id=current.execution_zone_id,
            capability="zone.runtime.execute",
            resource_path=f"/sessions/{current.session_id}",
        )
        view = svc.cancel_run(pid=pid, mode=mode)
    except SessionRuntimeError as exc:
        raise _svc_error(exc) from exc
    response.headers["X-Runtime-State"] = view.state
    return view.as_json()
