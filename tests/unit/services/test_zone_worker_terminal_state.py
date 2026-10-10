"""Outbox-exhaustion terminal marking: a create/deprovision saga whose outbox
burns all retries must leave the zone row's canonical_status telling the truth
("failed") instead of an in-flight "creating"/"deleting" forever. The row stays
for deprovision/rebuild (service.py's 409 hint); only the status changes."""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from nexus.services.zones.worker import MAX_ATTEMPTS, ZoneOperationWorker
from nexus.storage.models.auth import ZoneModel
from nexus.storage.models.zone_v1 import (
    ZoneGrantProjectionOutboxModel,
    ZoneOperationModel,
    ZoneRuntimeOutboxModel,
)


# Same unit-level pattern as test_zone_service.py: SQLite in memory, only the
# four tables the exhaustion path touches.
@pytest.fixture()
def session_factory():
    engine = sa.create_engine("sqlite://", future=True)

    @sa.event.listens_for(engine, "connect")
    def _fk_on(dbapi_conn, _rec):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    ZoneModel.__table__.create(engine)
    ZoneOperationModel.__table__.create(engine)
    ZoneRuntimeOutboxModel.__table__.create(engine)
    ZoneGrantProjectionOutboxModel.__table__.create(engine)
    return sessionmaker(bind=engine, future=True, expire_on_commit=False)


def _seed_zone(session_factory, zone_id: str, canonical_status: str) -> None:
    with session_factory() as session, session.begin():
        session.add(ZoneModel(zone_id=zone_id, name=zone_id, canonical_status=canonical_status))


def _seed_operation(
    session_factory, operation_id: str, action: str, zone_id: str, grant_id: str | None = None
) -> None:
    with session_factory() as session, session.begin():
        session.add(
            ZoneOperationModel(
                operation_id=operation_id,
                action=action,
                zone_id=zone_id,
                grant_id=grant_id,
                state="running",
                step=f"{action}.seed",
                idempotency_scope=f"scope-{operation_id}",
                idempotency_key=f"key-{operation_id}",
                request_hash=f"hash-{operation_id}",
            )
        )


def _exhausted(worker: ZoneOperationWorker, model) -> int:
    """One pump over a row pre-seeded one attempt below exhaustion; the
    handler always fails, so attempts reaches MAX_ATTEMPTS and the worker
    takes the terminal branch."""
    return worker._pump_outbox(model, lambda event: False)


def test_create_exhaustion_marks_zone_failed(session_factory):
    # Deployment-proven shape (org-63597be5): the create saga dies in the
    # grant-projection outbox — that model carries no operation_id, the
    # worker resolves it through the grant_id back-reference.
    zone_id = "org-exhausted-create"
    _seed_zone(session_factory, zone_id, "creating")
    _seed_operation(session_factory, "op-create-1", "create", zone_id, grant_id="grant-1")
    with session_factory() as session, session.begin():
        session.add(
            ZoneGrantProjectionOutboxModel(
                grant_id="grant-1",
                event_type="grant.projection",
                payload={"grant_id": "grant-1", "zone_id": zone_id},
                attempt_count=MAX_ATTEMPTS - 1,
            )
        )

    worker = ZoneOperationWorker(session_factory, runtime=None, service=None)
    processed = _exhausted(worker, ZoneGrantProjectionOutboxModel)

    assert processed == 1
    with session_factory() as session:
        operation = session.get(ZoneOperationModel, "op-create-1")
        assert operation.state == "failed"
        assert operation.error["code"] == "PROJECTION_FAILED"
        zone = session.get(ZoneModel, zone_id)
        assert zone.canonical_status == "failed"


def test_deprovision_exhaustion_marks_zone_failed(session_factory):
    # Deployment-proven shape (org-b822e990 / org-0fdabe12): deprovision dies
    # in the runtime outbox, which reaches the operation directly through
    # its operation_id column.
    zone_id = "org-exhausted-deprovision"
    _seed_zone(session_factory, zone_id, "deleting")
    _seed_operation(session_factory, "op-deprov-1", "deprovision", zone_id)
    with session_factory() as session, session.begin():
        session.add(
            ZoneRuntimeOutboxModel(
                operation_id="op-deprov-1",
                event_type="runtime.delete",
                payload={"zone_id": zone_id},
                attempt_count=MAX_ATTEMPTS - 1,
            )
        )

    worker = ZoneOperationWorker(session_factory, runtime=None, service=None)
    processed = _exhausted(worker, ZoneRuntimeOutboxModel)

    assert processed == 1
    with session_factory() as session:
        operation = session.get(ZoneOperationModel, "op-deprov-1")
        assert operation.state == "failed"
        assert operation.error["code"] == "ZONE_RUNTIME_UNAVAILABLE"
        zone = session.get(ZoneModel, zone_id)
        assert zone.canonical_status == "failed"
