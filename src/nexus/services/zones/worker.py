"""HA zone operation worker (2C): lease/generation/fence, crash-resumable.

The loop claims due outbox work under a lease, does the external effect
through the runtime port, and writes results back fenced — a worker whose
lease expired (crash, pause, partition) matches zero rows on its way back,
so the takeover worker's state is never overwritten (§5.7).

Reconciliation is the crash-recovery path: operations stuck mid-flight are
re-driven from their recorded step plus a fresh runtime read-back, never
from assumptions (§5.2 failure rules).
"""

from __future__ import annotations

import logging
import secrets
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import or_, select, update

from nexus.remote.zone_runtime_client import ZoneRuntimePort, ZoneRuntimeUnavailable
from nexus.storage.models import (
    ZoneGrantProjectionOutboxModel,
    ZoneOperationModel,
    ZoneRuntimeOutboxModel,
)

logger = logging.getLogger(__name__)

WORKER_ID = f"zone-worker-{secrets.token_hex(4)}"
LEASE_S = 30
MAX_ATTEMPTS = 8


class ZoneOperationWorker:
    def __init__(
        self,
        session_factory: Callable[[], Any],
        runtime: ZoneRuntimePort,
        service: Any,
        session_runtime: Any = None,
        session_tasks: Any = None,
        runtime_dependency_validator: Callable[[str, str, str, int, str], bool | None]
        | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._runtime = runtime
        self._service = service  # ZoneApplicationService, for saga continuation
        self._session_runtime = session_runtime
        self._session_tasks = session_tasks
        self._runtime_dependency_validator = runtime_dependency_validator

    # ── outbox pumping ───────────────────────────────────────────────────────

    def pump_once(self) -> int:
        """One pass over due work; returns the number of items processed."""
        processed = 0
        processed += self._pump_outbox(ZoneRuntimeOutboxModel, self._do_runtime_event)
        processed += self._pump_outbox(ZoneGrantProjectionOutboxModel, self._do_projection_event)
        if self._session_runtime is not None and self._runtime_dependency_validator is not None:
            self._session_runtime.revalidate_runtime_dependencies(
                self._runtime_dependency_validator
            )
            if self._session_tasks is not None:
                self._session_tasks.park_attempts_for_revocation()
        return processed

    def _pump_outbox(self, model: type[Any], handler: Callable[[dict[str, Any]], bool]) -> int:
        now = datetime.now(UTC)
        count = 0
        while True:
            with self._session_factory() as session, session.begin():
                due = (
                    session.execute(
                        select(model)
                        .where(model.processed_at.is_(None))
                        .where((model.next_retry_at.is_(None)) | (model.next_retry_at <= now))
                        .where(
                            or_(
                                model.lease_owner.is_(None),
                                model.lease_expires_at.is_(None),
                                model.lease_expires_at <= now,
                            )
                        )
                        .order_by(model.id)
                        .limit(1)
                        .with_for_update(skip_locked=True)
                    )
                    .scalars()
                    .first()
                )
                if due is None:
                    return count
                due.lease_owner = WORKER_ID
                due.lease_expires_at = now + timedelta(seconds=LEASE_S)
                due.generation += 1
                due.fence += 1
                lease_token = (due.lease_owner, due.generation, due.fence)
                event = dict(due.payload) if isinstance(due.payload, dict) else {}
                event["event_type"] = due.event_type
                operation_id = getattr(due, "operation_id", None)
                if operation_id:
                    event["operation_id"] = operation_id
                row_id = due.id

            ok = False
            try:
                ok = handler(event)
            except ZoneRuntimeUnavailable:
                ok = False  # unknown, not failed — retry keeps the idempotent key
            except Exception:
                logger.exception(
                    "zone outbox handler failed for %s#%s", model.__tablename__, row_id
                )
                ok = False

            with self._session_factory() as session, session.begin():
                attempts = select_stmt_count(model, session, row_id) + 1
                done = ok or attempts >= MAX_ATTEMPTS
                if not ok and attempts >= MAX_ATTEMPTS:
                    operation_id = (
                        getattr(row, "operation_id", None)
                        if (row := session.get(model, row_id))
                        else None
                    )
                    if operation_id is None and row is not None:
                        grant_id = getattr(row, "grant_id", None)
                        if grant_id:
                            operation_id = session.execute(
                                select(ZoneOperationModel.operation_id).where(
                                    ZoneOperationModel.grant_id == grant_id,
                                    ZoneOperationModel.state.in_(
                                        ("queued", "running", "waiting_dependency")
                                    ),
                                )
                            ).scalar_one_or_none()
                    if operation_id:
                        operation = session.get(ZoneOperationModel, operation_id)
                        if operation is not None:
                            operation.state = "failed"
                            operation.step = (
                                f"{event.get('event_type', 'outbox')}.retries-exhausted"
                            )
                            operation.retryable = False
                            operation.error = {
                                "code": "PROJECTION_FAILED"
                                if model is ZoneGrantProjectionOutboxModel
                                else "ZONE_RUNTIME_UNAVAILABLE",
                                "message": f"outbox retries exhausted after {attempts} attempts",
                                "retryable": False,
                            }
                            operation.completed_at = datetime.now(UTC)
                stmt = (
                    update(model)
                    .where(
                        model.id == row_id,
                        model.lease_owner == lease_token[0],
                        model.generation == lease_token[1],
                        model.fence == lease_token[2],
                    )
                    .values(
                        processed_at=datetime.now(UTC) if done else None,
                        attempt_count=attempts,
                        next_retry_at=None if done else datetime.now(UTC) + timedelta(seconds=5),
                        lease_owner=None,
                        lease_expires_at=None,
                    )
                )
                session.execute(stmt.execution_options(synchronize_session="fetch"))
            count += 1
            if count >= 16:  # bound one pass
                return count

    # ── event handlers ───────────────────────────────────────────────────────

    def _do_runtime_event(self, event: dict) -> bool:
        kind = event.get("event_type")
        operation_id = event.get("operation_id")
        if not operation_id:
            return True
        if kind == "zone.create":
            result = self._service._pump_create(operation_id=operation_id)
        else:
            result = self._service.process_runtime_operation(
                operation_id=operation_id,
                event_type=str(kind),
                payload=event,
            )
        return result.state in ("succeeded", "waiting_dependency")

    def _do_projection_event(self, event: dict) -> bool:
        grant_id = event.get("grant_id")
        if not grant_id:
            return True
        kind = event.get("event_type")
        if kind == "grant.cleanup_projections":
            self._service.cleanup_grant_projection(grant_id=grant_id)
            if self._session_runtime is not None:
                self._session_runtime.park_runs_for_revocation(
                    zone_id=str(event.get("zone_id") or ""),
                    grant_ref=str(grant_id),
                    authorization_epoch=int(event.get("authorization_epoch") or 0),
                )
                if self._session_tasks is not None:
                    self._session_tasks.park_attempts_for_revocation()
            return True
        # activation: epoch advances only when the mandatory projections exist
        return bool(self._service.complete_grant_projection(grant_id=grant_id))

    # ── reconciliation (crash recovery) ──────────────────────────────────────

    def reconcile_stale_operations(self) -> int:
        now = datetime.now(UTC)
        recovered = 0
        with self._session_factory() as session, session.begin():
            stale = (
                session.execute(
                    select(ZoneOperationModel).where(
                        ZoneOperationModel.state.in_(("queued", "running")),
                        (ZoneOperationModel.lease_expires_at.is_(None))
                        | (ZoneOperationModel.lease_expires_at <= now),
                    )
                )
                .scalars()
                .all()
            )
            for op in stale:
                if op.action == "create" and op.zone_id:
                    recovered += 1
                    # resume via saga continuation, not assumption
                    op.lease_owner = WORKER_ID
                    op.lease_expires_at = now + timedelta(seconds=LEASE_S)
        for op_id in self._collect_pending_creates():
            self._service._pump_create(operation_id=op_id)
        return recovered

    def _collect_pending_creates(self) -> list[str]:
        with self._session_factory() as session:
            rows = (
                session.execute(
                    select(ZoneOperationModel.operation_id).where(
                        ZoneOperationModel.action == "create",
                        ZoneOperationModel.state.in_(("queued", "running")),
                        ~ZoneOperationModel.step.like("zone.join%"),
                    )
                )
                .scalars()
                .all()
            )
            return list(rows)


def select_stmt_count(model: type[Any], session: Any, row_id: int) -> int:
    row = session.get(model, row_id)
    return int(row.attempt_count) if row is not None else 0
