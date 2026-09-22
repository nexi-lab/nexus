"""P1b implicit Task/Resolution/Attempt service.

The Session Runtime API is the only writer in this phase.  Database rows are
authoritative; immutable JSON snapshots under the Session home Zone provide
real-I/O placement evidence without extending the five-kind session record
ledger.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from nexus.storage.models import (
    SessionModel,
    SessionRuntimeRunModel,
    TaskAttemptModel,
    TaskResolutionModel,
    TaskSpecModel,
    ZoneModel,
)


class SessionTaskError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


@dataclass(frozen=True)
class TaskRef:
    task_id: str
    session_id: str
    home_zone_id: str
    policy_version: str


@dataclass(frozen=True)
class AttemptRef:
    attempt_id: str
    task_id: str
    session_id: str
    execution_zone_id: str
    state: str


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _implicit_task_id(session_id: str) -> str:
    value = uuid.uuid5(uuid.NAMESPACE_URL, f"nexus:implicit-task:{session_id}")
    return f"task_{value.hex}"


def _json_bytes(value: dict[str, Any]) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()


class SessionTaskService:
    """Own the P1b implicit task lifecycle inside the Session Runtime path."""

    def __init__(self, session_factory: Any, *, fs_writer: Any = None) -> None:
        self._session_factory = session_factory
        self._fs_writer = fs_writer

    def ensure_implicit_task(
        self,
        *,
        session_id: str,
        requested_by: dict[str, Any],
        resource_refs: list[dict[str, Any]],
    ) -> TaskRef:
        """Create the Session's write-once implicit TaskSpec, or return it."""
        if self._fs_writer is None:
            raise SessionTaskError(
                "ZONE_RUNTIME_UNAVAILABLE",
                "zone-scoped task writes are not armed in this deployment",
                503,
            )
        with self._session_factory() as session:
            existing = self._task_for_session(session, session_id)
            if existing is not None:
                return self._task_ref(session, existing)
            parent = session.get(SessionModel, session_id)
            if parent is None:
                raise SessionTaskError("SESSION_NOT_FOUND", f"session {session_id} not found", 404)
            if parent.state != "active":
                raise SessionTaskError(
                    "SESSION_NOT_ACTIVE", f"session {session_id} is {parent.state}", 409
                )

            task_id = _implicit_task_id(session_id)
            created_at = _utcnow()
            path = f"/sessions/{session_id}/tasks/{task_id}/spec.json"
            instruction = f"implicit runtime execution for session {session_id}"
            payload = {
                "api_version": "task.sudo.dev/v1",
                "kind": "TaskSpec",
                "task_id": task_id,
                "session_id": session_id,
                "requested_by": requested_by,
                "input": {
                    "instruction": instruction,
                    "resource_refs": resource_refs,
                },
                "requested_mode": "auto",
                "created_at": created_at.isoformat(),
            }
            written = self._write(path, payload, parent.home_zone_id)
            task = TaskSpecModel(
                task_id=task_id,
                session_id=session_id,
                requested_by=requested_by,
                instruction=instruction,
                resource_refs=resource_refs,
                requested_mode="auto",
                zone_id=parent.home_zone_id,
                vfs_path=path,
                bytes_written=written,
                created_at=created_at,
            )
            session.add(task)
            try:
                session.commit()
            except IntegrityError as exc:
                session.rollback()
                existing = self._task_for_session(session, session_id)
                if existing is None:
                    raise SessionTaskError("TASK_CREATE_CONFLICT", str(exc.orig), 409) from exc
                return self._task_ref(session, existing)
            return self._task_ref(session, task)

    def reject(
        self,
        *,
        task_id: str,
        reason_code: str,
        reason: str,
        policy_version: str | None,
    ) -> dict[str, Any]:
        with self._session_factory() as session:
            task, parent = self._load_task_and_session(session, task_id)
            decided_at = _utcnow()
            resolution_id = _new_id("resolution")
            effective_policy = policy_version or parent.policy_version or "p1b-implicit"
            path = (
                f"/sessions/{task.session_id}/tasks/{task.task_id}/resolutions/{resolution_id}.json"
            )
            payload = {
                "api_version": "task.sudo.dev/v1",
                "kind": "TaskExecutionResolution",
                "resolution_id": resolution_id,
                "task_id": task.task_id,
                "status": "rejected",
                "reason_code": reason_code,
                "reason": reason,
                "policy_version": effective_policy,
                "decided_at": decided_at.isoformat(),
            }
            written = self._write(path, payload, parent.home_zone_id)
            resolution = TaskResolutionModel(
                resolution_id=resolution_id,
                task_id=task.task_id,
                status="rejected",
                attempt_id=None,
                execution_zone_id=None,
                reason_code=reason_code,
                reason=reason,
                policy_version=effective_policy,
                zone_id=parent.home_zone_id,
                vfs_path=path,
                bytes_written=written,
                decided_at=decided_at,
            )
            session.add(resolution)
            session.commit()
            return self._resolution_json(resolution)

    def create_attempt(
        self,
        *,
        task_id: str,
        execution_zone_id: str,
        reason_code: str,
        reason: str,
        policy_version: str | None,
    ) -> AttemptRef:
        """Atomically persist an accepted Resolution and queued Attempt."""
        with self._session_factory() as session:
            task, parent = self._load_task_and_session(session, task_id)
            zone = session.get(ZoneModel, execution_zone_id)
            if zone is None or (zone.canonical_status or "unknown") != "active":
                raise SessionTaskError(
                    "ZONE_NOT_ACTIVE", f"execution zone {execution_zone_id} is not active", 409
                )
            decided_at = _utcnow()
            resolution_id = _new_id("resolution")
            attempt_id = _new_id("attempt")
            effective_policy = policy_version or parent.policy_version or "p1b-implicit"
            resolution_path = (
                f"/sessions/{task.session_id}/tasks/{task.task_id}/resolutions/{resolution_id}.json"
            )
            attempt_path = (
                f"/sessions/{task.session_id}/tasks/{task.task_id}/"
                f"attempts/{attempt_id}/attempt.json"
            )
            resolution_payload = {
                "api_version": "task.sudo.dev/v1",
                "kind": "TaskExecutionResolution",
                "resolution_id": resolution_id,
                "task_id": task.task_id,
                "status": "accepted",
                "attempt_id": attempt_id,
                "execution_zone_id": execution_zone_id,
                "reason_code": reason_code,
                "reason": reason,
                "policy_version": effective_policy,
                "decided_at": decided_at.isoformat(),
            }
            attempt_payload = {
                "api_version": "task.sudo.dev/v1",
                "kind": "TaskAttempt",
                "attempt_id": attempt_id,
                "task_id": task.task_id,
                "session_id": task.session_id,
                "resolution_id": resolution_id,
                "execution_zone_id": execution_zone_id,
                "state": "queued",
                "pid_history": [],
                "created_at": decided_at.isoformat(),
            }
            resolution_written = self._write(
                resolution_path, resolution_payload, parent.home_zone_id
            )
            attempt_written = self._write(attempt_path, attempt_payload, parent.home_zone_id)
            resolution = TaskResolutionModel(
                resolution_id=resolution_id,
                task_id=task.task_id,
                status="accepted",
                attempt_id=attempt_id,
                execution_zone_id=execution_zone_id,
                reason_code=reason_code,
                reason=reason,
                policy_version=effective_policy,
                zone_id=parent.home_zone_id,
                vfs_path=resolution_path,
                bytes_written=resolution_written,
                decided_at=decided_at,
            )
            attempt = TaskAttemptModel(
                attempt_id=attempt_id,
                task_id=task.task_id,
                resolution_id=resolution_id,
                session_id=task.session_id,
                execution_zone_id=execution_zone_id,
                state="queued",
                pid_history=[],
                zone_id=parent.home_zone_id,
                vfs_path=attempt_path,
                bytes_written=attempt_written,
                created_at=decided_at,
            )
            session.add_all((resolution, attempt))
            session.commit()
            return self._attempt_ref(attempt)

    def attach_pid(self, *, attempt_id: str, pid: str) -> AttemptRef:
        with self._session_factory() as session:
            attempt = session.get(TaskAttemptModel, attempt_id)
            if attempt is None:
                raise SessionTaskError("ATTEMPT_NOT_FOUND", f"attempt {attempt_id} not found", 404)
            history = list(attempt.pid_history or [])
            if pid not in history:
                history.append(pid)
                attempt.pid_history = history
            if attempt.state == "queued":
                attempt.state = "running"
                attempt.started_at = _utcnow()
            session.commit()
            return self._attempt_ref(attempt)

    def mark_failed(self, *, attempt_id: str, error: Exception) -> AttemptRef:
        with self._session_factory() as session:
            attempt = session.get(TaskAttemptModel, attempt_id)
            if attempt is None:
                raise SessionTaskError("ATTEMPT_NOT_FOUND", f"attempt {attempt_id} not found", 404)
            code = getattr(error, "code", type(error).__name__)
            message = getattr(error, "message", str(error))
            attempt.state = "failed"
            attempt.failure = {"code": str(code), "message": str(message), "retryable": False}
            attempt.ended_at = _utcnow()
            session.commit()
            return self._attempt_ref(attempt)

    def latest_attempt(self, *, session_id: str) -> AttemptRef | None:
        with self._session_factory() as session:
            attempt = (
                session.execute(
                    select(TaskAttemptModel)
                    .join(
                        SessionRuntimeRunModel,
                        SessionRuntimeRunModel.attempt_id == TaskAttemptModel.attempt_id,
                    )
                    .where(
                        SessionRuntimeRunModel.session_id == session_id,
                        SessionRuntimeRunModel.attempt_id.is_not(None),
                    )
                    .order_by(SessionRuntimeRunModel.started_at.desc())
                )
                .scalars()
                .first()
            )
            return self._attempt_ref(attempt) if attempt is not None else None

    def park_attempts_for_revocation(self) -> int:
        """Cancel active Attempts referenced by parked runtime generations."""
        active_states = ("queued", "starting", "running", "awaiting_input")
        with self._session_factory() as session, session.begin():
            attempt_ids = set(
                session.execute(
                    select(TaskAttemptModel.attempt_id)
                    .join(
                        SessionRuntimeRunModel,
                        SessionRuntimeRunModel.attempt_id == TaskAttemptModel.attempt_id,
                    )
                    .where(
                        SessionRuntimeRunModel.state == "revocation_pending",
                        TaskAttemptModel.state.in_(active_states),
                    )
                )
                .scalars()
                .all()
            )
            attempts = [
                attempt
                for attempt_id in attempt_ids
                if (attempt := session.get(TaskAttemptModel, attempt_id)) is not None
            ]
            now = _utcnow()
            for attempt in attempts:
                attempt.state = "cancelled"
                attempt.failure = {
                    "code": "GRANT_REVOKED",
                    "message": "runtime authorization was revoked",
                    "retryable": False,
                }
                attempt.ended_at = now
            return len(attempts)

    def get_task(self, *, session_id: str, task_id: str) -> dict[str, Any]:
        with self._session_factory() as session:
            task = session.get(TaskSpecModel, task_id)
            if task is None or task.session_id != session_id:
                raise SessionTaskError("TASK_NOT_FOUND", f"task {task_id} not found", 404)
            resolutions = (
                session.execute(
                    select(TaskResolutionModel)
                    .where(TaskResolutionModel.task_id == task_id)
                    .order_by(TaskResolutionModel.decided_at, TaskResolutionModel.resolution_id)
                )
                .scalars()
                .all()
            )
            attempts = (
                session.execute(
                    select(TaskAttemptModel)
                    .where(TaskAttemptModel.task_id == task_id)
                    .order_by(TaskAttemptModel.created_at, TaskAttemptModel.attempt_id)
                )
                .scalars()
                .all()
            )
            return {
                "spec": self._task_json(task),
                "resolutions": [self._resolution_json(row) for row in resolutions],
                "attempts": [self._attempt_json(row) for row in attempts],
            }

    def task_for_session(self, *, session_id: str) -> TaskRef | None:
        with self._session_factory() as session:
            task = self._task_for_session(session, session_id)
            return self._task_ref(session, task) if task is not None else None

    def _write(self, path: str, payload: dict[str, Any], zone_id: str) -> int:
        if self._fs_writer is None:
            raise SessionTaskError(
                "ZONE_RUNTIME_UNAVAILABLE",
                "zone-scoped task writes are not armed in this deployment",
                503,
            )
        return int(self._fs_writer(path, _json_bytes(payload), zone_id))

    @staticmethod
    def _task_for_session(session: Session, session_id: str) -> TaskSpecModel | None:
        return session.execute(
            select(TaskSpecModel).where(TaskSpecModel.session_id == session_id)
        ).scalar_one_or_none()

    @staticmethod
    def _load_task_and_session(
        session: Session, task_id: str
    ) -> tuple[TaskSpecModel, SessionModel]:
        task = session.get(TaskSpecModel, task_id)
        if task is None:
            raise SessionTaskError("TASK_NOT_FOUND", f"task {task_id} not found", 404)
        parent = session.get(SessionModel, task.session_id)
        if parent is None:
            raise SessionTaskError("SESSION_NOT_FOUND", f"session {task.session_id} not found", 404)
        return task, parent

    @staticmethod
    def _task_ref(_session: Session, task: TaskSpecModel) -> TaskRef:
        parent = _session.get(SessionModel, task.session_id)
        assert parent is not None
        return TaskRef(
            task_id=task.task_id,
            session_id=task.session_id,
            home_zone_id=task.zone_id,
            policy_version=parent.policy_version,
        )

    @staticmethod
    def _attempt_ref(attempt: TaskAttemptModel) -> AttemptRef:
        return AttemptRef(
            attempt_id=attempt.attempt_id,
            task_id=attempt.task_id,
            session_id=attempt.session_id,
            execution_zone_id=attempt.execution_zone_id,
            state=attempt.state,
        )

    @staticmethod
    def _task_json(task: TaskSpecModel) -> dict[str, Any]:
        return {
            "api_version": "task.sudo.dev/v1",
            "kind": "TaskSpec",
            "task_id": task.task_id,
            "session_id": task.session_id,
            "requested_by": task.requested_by,
            "input": {
                "instruction": task.instruction,
                "resource_refs": list(task.resource_refs or []),
            },
            "requested_mode": task.requested_mode,
            "created_at": task.created_at.isoformat(),
            "storage": {
                "zone_id": task.zone_id,
                "vfs_path": task.vfs_path,
                "bytes_written": task.bytes_written,
            },
        }

    @staticmethod
    def _resolution_json(resolution: TaskResolutionModel) -> dict[str, Any]:
        result: dict[str, Any] = {
            "api_version": "task.sudo.dev/v1",
            "kind": "TaskExecutionResolution",
            "resolution_id": resolution.resolution_id,
            "task_id": resolution.task_id,
            "status": resolution.status,
            "reason_code": resolution.reason_code,
            "reason": resolution.reason,
            "policy_version": resolution.policy_version,
            "decided_at": resolution.decided_at.isoformat(),
            "storage": {
                "zone_id": resolution.zone_id,
                "vfs_path": resolution.vfs_path,
                "bytes_written": resolution.bytes_written,
            },
        }
        if resolution.status == "accepted":
            result["attempt_id"] = resolution.attempt_id
            result["execution_zone_id"] = resolution.execution_zone_id
        return result

    @staticmethod
    def _attempt_json(attempt: TaskAttemptModel) -> dict[str, Any]:
        result: dict[str, Any] = {
            "api_version": "task.sudo.dev/v1",
            "kind": "TaskAttempt",
            "attempt_id": attempt.attempt_id,
            "task_id": attempt.task_id,
            "session_id": attempt.session_id,
            "resolution_id": attempt.resolution_id,
            "execution_zone_id": attempt.execution_zone_id,
            "state": attempt.state,
            "pid_history": list(attempt.pid_history or []),
            "created_at": attempt.created_at.isoformat(),
            "started_at": attempt.started_at.isoformat() if attempt.started_at else None,
            "ended_at": attempt.ended_at.isoformat() if attempt.ended_at else None,
            "failure": attempt.failure,
            "storage": {
                "zone_id": attempt.zone_id,
                "vfs_path": attempt.vfs_path,
                "bytes_written": attempt.bytes_written,
            },
        }
        return result
