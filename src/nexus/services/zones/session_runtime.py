"""P1a SessionRuntimeService (SW-20260915-002 §8.9).

Owns the SessionStore semantics for the runtime-zone subset:

- session creation resolves the home zone from the authenticated ingress
  (the caller names a zone; the service validates it exists and is active
  and that the caller holds zone.data.write there — a client payload can
  never conjure the home zone by itself);
- home_zone_id is immutable: there is no update path, and any attempt is
  rejected loudly;
- the five record kinds (session/transcript/context/artifact/verify)
  default-write into the home zone through the typed kernel — real VFS
  bytes under ``/sessions/{sid}/...`` — and every write lands in the
  routing ledger;
- runtime start/resume solidifies execution_zone_id (default = home zone;
  cross-zone requires decision_reason/policy_version) plus delegation/
  grant/epoch references, feeding the dependency index;
- cancel marks termination; when the run cannot terminate it parks in
  revocation_pending, which blocks new resource acquisition (a new record
  write on a revocation_pending session is refused).

Restarts do not drift: home/execution zones live in rows written once and
re-read from the canonical store; the routing ledger records where every
record byte physically landed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from nexus.storage.models import (
    SessionDataRecordModel,
    SessionModel,
    SessionRuntimeRunModel,
    SessionZoneDependencyModel,
    ZoneModel,
)

logger = logging.getLogger(__name__)

RECORD_VFS_SUBPATHS: dict[str, str] = {
    "session": "session.json",
    "transcript": "transcript.jsonl",
    "context": "context.json",
    "artifact": "artifacts",
    "verify": "verify.json",
}


class SessionRuntimeError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


@dataclass(frozen=True)
class SessionView:
    session_id: str
    home_zone_id: str
    owner: dict
    state: str
    created_at: datetime
    updated_at: datetime

    def as_json(self) -> dict[str, Any]:
        return {
            "api_version": "runtime.sudo.dev/v2",
            "kind": "SessionMetadata",
            "session_id": self.session_id,
            "home_zone_id": self.home_zone_id,
            "owner": self.owner,
            "state": self.state,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }


@dataclass(frozen=True)
class RunView:
    pid: str
    session_id: str
    execution_zone_id: str
    state: str
    delegation_ref: str | None
    grant_ref: str | None
    authorization_epoch: int | None
    decision_reason: str | None
    policy_version: str | None
    started_at: datetime
    ended_at: datetime | None

    def as_json(self) -> dict[str, Any]:
        return {
            "api_version": "runtime.sudo.dev/v2",
            "kind": "RuntimeRun",
            "pid": self.pid,
            "session_id": self.session_id,
            "execution_zone_id": self.execution_zone_id,
            "state": self.state,
            "delegation_ref": self.delegation_ref,
            "grant_ref": self.grant_ref,
            "authorization_epoch": self.authorization_epoch,
            "decision_reason": self.decision_reason,
            "policy_version": self.policy_version,
            "started_at": self.started_at.isoformat(),
            "ended_at": self.ended_at.isoformat() if self.ended_at else None,
        }


def _utcnow() -> datetime:
    return datetime.now(UTC)


class SessionRuntimeService:
    def __init__(self, session_factory: Any, *, fs_writer: Any = None) -> None:
        """``fs_writer(path, buf, zone_id)`` performs the zone-scoped kernel
        write; when absent the service records the intent but refuses record
        writes (fail closed — never pretend SQL columns are zone I/O)."""
        self._session_factory = session_factory
        self._fs_writer = fs_writer

    # ── sessions ────────────────────────────────────────────────────────────
    def create_session(
        self,
        *,
        session_id: str,
        home_zone_id: str,
        owner: dict,
        created_by: dict,
        policy_version: str = "p1a-default",
        capability_check: Any = None,
    ) -> SessionView:
        """Create the session record; the home zone must exist and be active.

        ``capability_check(zone_id)`` is the ingress-provided policy hook
        (the router passes the authenticated zone.data.write check); the
        service itself additionally verifies the zone row exists and is
        active. The client naming a zone is a *target hint* validated by
        policy — it is never authority by itself.
        """
        if capability_check is not None and not capability_check(home_zone_id):
            raise SessionRuntimeError(
                "RESOURCE_RELATION_DENIED",
                f"caller may not create a session homed in {home_zone_id}",
                403,
            )
        with self._session_factory() as session:
            zone = session.get(ZoneModel, home_zone_id)
            if zone is None:
                raise SessionRuntimeError(
                    "ZONE_NOT_FOUND", f"home zone {home_zone_id} does not exist", 404
                )
            if (zone.canonical_status or "unknown") != "active":
                raise SessionRuntimeError(
                    "ZONE_NOT_ACTIVE",
                    f"home zone {home_zone_id} is {zone.canonical_status}, not active",
                    409,
                )
            record = SessionModel(
                session_id=session_id,
                home_zone_id=home_zone_id,
                owner=owner,
                created_by=created_by,
                policy_version=policy_version,
            )
            session.add(record)
            try:
                session.commit()
            except IntegrityError as exc:
                raise SessionRuntimeError("SESSION_ALREADY_EXISTS", str(exc.orig), 409) from exc
            return self._view(session, record)

    def get_session(self, session_id: str) -> SessionView:
        with self._session_factory() as session:
            record = session.get(SessionModel, session_id)
            if record is None:
                raise SessionRuntimeError(
                    "SESSION_NOT_FOUND", f"session {session_id} not found", 404
                )
            return self._view(session, record)

    def home_zone_of(self, session_id: str, session: Session) -> str:
        record = session.get(SessionModel, session_id)
        if record is None:
            raise SessionRuntimeError("SESSION_NOT_FOUND", f"session {session_id} not found", 404)
        return record.home_zone_id

    @staticmethod
    def _view(_session: Session, record: SessionModel) -> SessionView:
        return SessionView(
            session_id=record.session_id,
            home_zone_id=record.home_zone_id,
            owner=record.owner,
            state=record.state,
            created_at=record.created_at,
            updated_at=record.updated_at,
        )

    # ── home-zone record routing (§8.9 item 2) ─────────────────────────────
    def write_session_record(
        self,
        *,
        session_id: str,
        record_kind: str,
        payload: bytes,
        record_name: str = "default",
        zone_hint: str | None = None,
    ) -> dict[str, Any]:
        """Default-route one record write into the session's home zone.

        The zone hint from the caller can only name the home zone itself;
        anything else is refused (a payload zone never overrides the
        authenticated home zone). Real kernel bytes are written and the
        routing ledger records zone + path + size.
        """
        if record_kind not in RECORD_VFS_SUBPATHS:
            raise SessionRuntimeError(
                "UNSUPPORTED_RECORD_KIND", f"unknown record kind {record_kind}", 422
            )
        if self._fs_writer is None:
            raise SessionRuntimeError(
                "ZONE_RUNTIME_UNAVAILABLE",
                "zone-scoped record writes are not armed in this deployment",
                503,
            )
        with self._session_factory() as session:
            record = session.get(SessionModel, session_id)
            if record is None:
                raise SessionRuntimeError(
                    "SESSION_NOT_FOUND", f"session {session_id} not found", 404
                )
            if record.state != "active":
                raise SessionRuntimeError(
                    "SESSION_NOT_ACTIVE", f"session {session_id} is {record.state}", 409
                )
            home_zone = record.home_zone_id
            if zone_hint is not None and zone_hint != home_zone:
                raise SessionRuntimeError(
                    "ZONE_OVERRIDE_DENIED",
                    "a client-supplied zone hint may not override the session home zone",
                    403,
                )
            self._require_no_pending_runs(session, session_id)

            subpath = RECORD_VFS_SUBPATHS[record_kind]
            name_part = record_name if record_kind == "artifact" else ""
            vfs_path = (
                f"/sessions/{session_id}/{subpath}/{record_name}"
                if name_part
                else f"/sessions/{session_id}/{subpath}"
            )
            written = self._fs_writer(vfs_path, payload, home_zone)
            ledger = session.execute(
                select(SessionDataRecordModel).where(
                    SessionDataRecordModel.session_id == session_id,
                    SessionDataRecordModel.record_kind == record_kind,
                    SessionDataRecordModel.record_name == record_name,
                )
            ).scalar_one_or_none()
            if ledger is None:
                ledger = SessionDataRecordModel(
                    session_id=session_id,
                    record_kind=record_kind,
                    record_name=record_name,
                    zone_id=home_zone,
                    vfs_path=vfs_path,
                    bytes_written=written,
                )
                session.add(ledger)
            else:
                ledger.zone_id = home_zone
                ledger.vfs_path = vfs_path
                ledger.bytes_written = written
                ledger.updated_at = _utcnow()
            session.commit()
            return {
                "session_id": session_id,
                "record_kind": record_kind,
                "record_name": record_name,
                "zone_id": home_zone,
                "vfs_path": vfs_path,
                "bytes_written": written,
            }

    def record_ledger(self, session_id: str) -> list[dict[str, Any]]:
        with self._session_factory() as session:
            rows = (
                session.execute(
                    select(SessionDataRecordModel).where(
                        SessionDataRecordModel.session_id == session_id
                    )
                )
                .scalars()
                .all()
            )
            return [
                {
                    "record_kind": r.record_kind,
                    "record_name": r.record_name,
                    "zone_id": r.zone_id,
                    "vfs_path": r.vfs_path,
                    "bytes_written": r.bytes_written,
                }
                for r in rows
            ]

    @staticmethod
    def _require_no_pending_runs(session: Session, session_id: str) -> None:
        pending = (
            session.execute(
                select(SessionRuntimeRunModel.pid).where(
                    SessionRuntimeRunModel.session_id == session_id,
                    SessionRuntimeRunModel.state == "revocation_pending",
                )
            )
            .scalars()
            .first()
        )
        if pending is not None:
            raise SessionRuntimeError(
                "REVOCATION_PENDING",
                f"session {session_id} has run {pending} in revocation_pending; no new resources",
                409,
            )

    # ── runtime runs (§8.9 items 3/4) ──────────────────────────────────────
    def start_run(
        self,
        *,
        pid: str,
        session_id: str,
        execution_zone_hint: str | None = None,
        delegation_ref: str | None = None,
        grant_ref: str | None = None,
        authorization_epoch: int | None = None,
        decision_reason: str | None = None,
        policy_version: str | None = None,
        zone_active_check: Any = None,
    ) -> RunView:
        """Register a run; solidify execution_zone_id and dependency refs.

        The execution zone defaults to the session home zone. A different
        zone requires an explicit decision_reason + policy_version
        (cross-zone execution is a recorded policy decision) and an
        active-zone check. The grant/epoch references feed the dependency
        index used by cancellation and revocation_pending handling.
        """
        with self._session_factory() as session:
            record = session.get(SessionModel, session_id)
            if record is None:
                raise SessionRuntimeError(
                    "SESSION_NOT_FOUND", f"session {session_id} not found", 404
                )
            if record.state != "active":
                raise SessionRuntimeError(
                    "SESSION_NOT_ACTIVE", f"session {session_id} is {record.state}", 409
                )
            home_zone = record.home_zone_id
            if execution_zone_hint is None or execution_zone_hint == home_zone:
                execution_zone = home_zone
                reason = None
            else:
                if not decision_reason or not policy_version:
                    raise SessionRuntimeError(
                        "CROSS_ZONE_DECISION_REQUIRED",
                        "execution outside the home zone requires decision_reason and policy_version",
                        422,
                    )
                execution_zone = execution_zone_hint
                reason = decision_reason
            zone_row = session.get(ZoneModel, execution_zone)
            if zone_row is None or (zone_row.canonical_status or "unknown") != "active":
                raise SessionRuntimeError(
                    "ZONE_NOT_ACTIVE", f"execution zone {execution_zone} is not active", 409
                )
            if zone_active_check is not None and not zone_active_check(execution_zone):
                raise SessionRuntimeError(
                    "RESOURCE_RELATION_DENIED",
                    f"policy denies executing in {execution_zone}",
                    403,
                )
            run = SessionRuntimeRunModel(
                pid=pid,
                session_id=session_id,
                execution_zone_id=execution_zone,
                delegation_ref=delegation_ref,
                grant_ref=grant_ref,
                authorization_epoch=authorization_epoch,
                decision_reason=reason,
                policy_version=policy_version
                if execution_zone != home_zone
                else (policy_version or "p1a-default"),
                state="registered",
            )
            session.add(run)
            session.add(
                SessionZoneDependencyModel(
                    zone_id=execution_zone,
                    pid=pid,
                    session_id=session_id,
                    grant_ref=grant_ref,
                    authorization_epoch=authorization_epoch,
                )
            )
            try:
                session.commit()
            except IntegrityError as exc:
                raise SessionRuntimeError("RUN_ALREADY_EXISTS", str(exc.orig), 409) from exc
            return self._run_view(run)

    def resume_run(
        self,
        *,
        pid: str,
        session_id: str,
        execution_zone_hint: str | None = None,
        delegation_ref: str | None = None,
        grant_ref: str | None = None,
        authorization_epoch: int | None = None,
        zone_active_check: Any = None,
    ) -> RunView:
        """Create a new PID without allowing execution-zone drift.

        Resume inherits the most recent run's execution zone.  A caller may
        repeat that zone as a consistency hint, but may not silently move the
        resumed runtime to another zone.
        """
        with self._session_factory() as session:
            previous = (
                session.execute(
                    select(SessionRuntimeRunModel)
                    .where(SessionRuntimeRunModel.session_id == session_id)
                    .order_by(SessionRuntimeRunModel.started_at.desc())
                )
                .scalars()
                .first()
            )
            if previous is None:
                raise SessionRuntimeError(
                    "RUN_NOT_FOUND", f"session {session_id} has no run to resume", 404
                )
            inherited_zone = previous.execution_zone_id
            if execution_zone_hint is not None and execution_zone_hint != inherited_zone:
                raise SessionRuntimeError(
                    "ZONE_IDENTITY_DRIFT",
                    "resume may not change the previous execution zone",
                    409,
                )
            session_record = session.get(SessionModel, session_id)
            assert session_record is not None
            cross_zone = inherited_zone != session_record.home_zone_id
            decision_reason = previous.decision_reason if cross_zone else None
            policy_version = previous.policy_version if cross_zone else None

        return self.start_run(
            pid=pid,
            session_id=session_id,
            execution_zone_hint=inherited_zone,
            delegation_ref=delegation_ref,
            grant_ref=grant_ref,
            authorization_epoch=authorization_epoch,
            decision_reason=decision_reason,
            policy_version=policy_version,
            zone_active_check=zone_active_check,
        )

    def resume_zone_of(self, session_id: str) -> str:
        """Return the immutable execution zone a new resume PID must inherit."""
        with self._session_factory() as session:
            previous = (
                session.execute(
                    select(SessionRuntimeRunModel)
                    .where(SessionRuntimeRunModel.session_id == session_id)
                    .order_by(SessionRuntimeRunModel.started_at.desc())
                )
                .scalars()
                .first()
            )
            if previous is None:
                raise SessionRuntimeError(
                    "RUN_NOT_FOUND", f"session {session_id} has no run to resume", 404
                )
            return str(previous.execution_zone_id)

    def get_run(self, pid: str) -> RunView:
        with self._session_factory() as session:
            run = session.get(SessionRuntimeRunModel, pid)
            if run is None:
                raise SessionRuntimeError("RUN_NOT_FOUND", f"run {pid} not found", 404)
            return self._run_view(run)

    def cancel_run(self, *, pid: str, mode: str = "terminate") -> RunView:
        """Cancel a run. ``terminate`` marks it terminated; ``pending`` parks
        it in revocation_pending — the run may not obtain new resources
        afterwards (write_session_record refuses while any run is pending)."""
        if mode not in ("terminate", "pending"):
            raise SessionRuntimeError("BAD_CANCEL_MODE", f"unknown cancel mode {mode}", 422)
        with self._session_factory() as session:
            run = session.get(SessionRuntimeRunModel, pid)
            if run is None:
                raise SessionRuntimeError("RUN_NOT_FOUND", f"run {pid} not found", 404)
            run.state = "revocation_pending" if mode == "pending" else "terminated"
            if mode == "terminate":
                run.ended_at = _utcnow()
            session.commit()
            return self._run_view(run)

    def runs_depending_on(self, *, zone_id: str, grant_ref: str | None = None) -> list[RunView]:
        """Dependency-index query: active runs bound to a zone/grant — the
        input set for cancellation when a grant is revoked or an epoch moves."""
        with self._session_factory() as session:
            stmt = select(SessionRuntimeRunModel).where(
                SessionRuntimeRunModel.execution_zone_id == zone_id,
                SessionRuntimeRunModel.state.in_(
                    ("registered", "warming_up", "ready", "busy", "awaiting_input")
                ),
            )
            if grant_ref is not None:
                stmt = stmt.where(SessionRuntimeRunModel.grant_ref == grant_ref)
            runs = session.execute(stmt).scalars().all()
            return [self._run_view(r) for r in runs]

    def park_runs_for_revocation(
        self, *, zone_id: str, grant_ref: str, authorization_epoch: int
    ) -> int:
        """Fail closed after a grant/epoch change.

        Every pre-existing run in the zone carries the previous epoch.  Runs
        directly tied to the revoked grant, carrying a stale epoch, or missing
        dependency references are parked until a fresh delegation creates a
        new runtime generation.
        """
        active_states = ("registered", "warming_up", "ready", "busy", "awaiting_input")
        parked = 0
        with self._session_factory() as session, session.begin():
            runs = (
                session.execute(
                    select(SessionRuntimeRunModel).where(
                        SessionRuntimeRunModel.execution_zone_id == zone_id,
                        SessionRuntimeRunModel.state.in_(active_states),
                    )
                )
                .scalars()
                .all()
            )
            for run in runs:
                if (
                    run.grant_ref == grant_ref
                    or run.authorization_epoch is None
                    or run.authorization_epoch < authorization_epoch
                ):
                    run.state = "revocation_pending"
                    parked += 1
        return parked

    def park_session_runs(self, *, session_id: str) -> int:
        """Park active runs after an access-time delegation failure."""
        active_states = ("registered", "warming_up", "ready", "busy", "awaiting_input")
        parked = 0
        with self._session_factory() as session, session.begin():
            runs = (
                session.execute(
                    select(SessionRuntimeRunModel).where(
                        SessionRuntimeRunModel.session_id == session_id,
                        SessionRuntimeRunModel.state.in_(active_states),
                    )
                )
                .scalars()
                .all()
            )
            for run in runs:
                run.state = "revocation_pending"
                parked += 1
        return parked

    def revalidate_runtime_dependencies(self, validator: Any) -> int:
        """Park active runtimes whose delegation/grant/epoch is no longer current."""
        active_states = ("registered", "warming_up", "ready", "busy", "awaiting_input")
        with self._session_factory() as session:
            snapshots = [
                (
                    run.pid,
                    run.delegation_ref,
                    run.execution_zone_id,
                    run.grant_ref,
                    run.authorization_epoch,
                )
                for run in (
                    session.execute(
                        select(SessionRuntimeRunModel).where(
                            SessionRuntimeRunModel.state.in_(active_states)
                        )
                    )
                    .scalars()
                    .all()
                )
            ]

        invalid = [
            pid
            for pid, delegation_ref, zone_id, grant_ref, epoch in snapshots
            if not delegation_ref
            or grant_ref is None
            or epoch is None
            or not validator(delegation_ref, zone_id, grant_ref, int(epoch))
        ]
        if not invalid:
            return 0
        with self._session_factory() as session, session.begin():
            parked = 0
            for pid in invalid:
                run = session.get(SessionRuntimeRunModel, pid)
                if run is not None and run.state in active_states:
                    run.state = "revocation_pending"
                    parked += 1
            return parked

    @staticmethod
    def _run_view(run: SessionRuntimeRunModel) -> RunView:
        return RunView(
            pid=run.pid,
            session_id=run.session_id,
            execution_zone_id=run.execution_zone_id,
            state=run.state,
            delegation_ref=run.delegation_ref,
            grant_ref=run.grant_ref,
            authorization_epoch=run.authorization_epoch,
            decision_reason=run.decision_reason,
            policy_version=run.policy_version,
            started_at=run.started_at,
            ended_at=run.ended_at,
        )
