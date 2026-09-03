"""GET /api/v2/operations/wait — fence on a write's projection sequence (#4738).

Runs against a real SQLite RecordStore so the probe sees rows committed
from another connection while it polls (the reason each probe opens a
fresh session).
"""

from __future__ import annotations

import tempfile
import threading
import time
from collections.abc import Generator
from pathlib import Path
from types import SimpleNamespace

import pytest

try:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    _HAS_FASTAPI = True
except ImportError:
    _HAS_FASTAPI = False

from nexus.storage.operation_logger import OperationLogger
from nexus.storage.record_store import SQLAlchemyRecordStore

pytestmark = pytest.mark.skipif(not _HAS_FASTAPI, reason="fastapi test client unavailable")

_AUTH = {
    "authenticated": True,
    "subject_type": "user",
    "subject_id": "alice",
    "zone_id": "eng",
    "zone_perms": [["eng", "rw"]],
    "is_admin": False,
}


@pytest.fixture
def record_store() -> Generator[SQLAlchemyRecordStore, None, None]:
    with tempfile.TemporaryDirectory() as tmpdir:
        rs = SQLAlchemyRecordStore(db_path=Path(tmpdir) / "metadata.db")
        yield rs
        rs.close()


def _log(record_store: SQLAlchemyRecordStore, path: str, *, zone_id: str = "eng") -> int:
    with record_store.session_factory() as session:
        op_logger = OperationLogger(session)
        op_logger.log_operation(operation_type="write", path=path, zone_id=zone_id)
        session.commit()
        assert op_logger.last_sequence_number is not None
        return op_logger.last_sequence_number


def _client(record_store: SQLAlchemyRecordStore) -> TestClient:
    from nexus.server.api.v2.dependencies import get_nexus_fs
    from nexus.server.api.v2.routers.operations import router
    from nexus.server.dependencies import require_auth

    fake_fs = SimpleNamespace(record_store=record_store)
    app = FastAPI()
    app.state.nexus_fs = fake_fs
    app.dependency_overrides[get_nexus_fs] = lambda: fake_fs
    app.dependency_overrides[require_auth] = lambda: _AUTH
    app.include_router(router)
    return TestClient(app)


def test_committed_sequence_is_applied_immediately(record_store: SQLAlchemyRecordStore) -> None:
    seq = _log(record_store, "/a")
    later = _log(record_store, "/b")
    resp = _client(record_store).get(f"/api/v2/operations/wait?seq={seq}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["applied"] is True
    assert body["seq"] == seq
    assert body["latest_seq"] == later
    assert body["zone_id"] == "eng"


def test_unknown_sequence_is_412_with_latest_after_timeout(
    record_store: SQLAlchemyRecordStore,
) -> None:
    latest = _log(record_store, "/a")
    resp = _client(record_store).get(f"/api/v2/operations/wait?seq={latest + 5}&timeout_ms=0")
    assert resp.status_code == 412, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "projection_not_applied"
    assert detail["seq"] == latest + 5
    assert detail["latest_seq"] == latest
    assert detail["zone_id"] == "eng"


def test_other_zones_sequence_looks_unapplied(record_store: SQLAlchemyRecordStore) -> None:
    foreign = _log(record_store, "/secret", zone_id="finance")
    resp = _client(record_store).get(f"/api/v2/operations/wait?seq={foreign}&timeout_ms=0")
    assert resp.status_code == 412
    assert resp.json()["detail"]["latest_seq"] is None, "no rows in the caller's zone"


def test_wait_returns_once_the_row_lands_from_another_connection(
    record_store: SQLAlchemyRecordStore,
) -> None:
    first = _log(record_store, "/a")
    target = first + 1
    client = _client(record_store)  # build the app before starting the clock

    def _late_commit() -> None:
        time.sleep(0.15)
        _log(record_store, "/b")

    threading.Thread(target=_late_commit, daemon=True).start()
    resp = client.get(f"/api/v2/operations/wait?seq={target}&timeout_ms=3000")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["applied"] is True and body["latest_seq"] == target
    assert body["waited_ms"] >= 100


def test_invalid_parameters_are_400(record_store: SQLAlchemyRecordStore) -> None:
    client = _client(record_store)
    assert client.get("/api/v2/operations/wait?seq=0").status_code == 422
    assert client.get("/api/v2/operations/wait?seq=1&timeout_ms=60000").status_code == 422
    assert client.get("/api/v2/operations/wait").status_code == 422
