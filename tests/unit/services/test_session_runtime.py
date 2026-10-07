"""Session runtime state-machine regressions (audit T9): terminal guards,
pending-resource gates, per-name record paths and reaper convergence."""

from __future__ import annotations

import tempfile

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from nexus.services.zones.session_runtime import (
    PARK_STALE_AFTER_S,
    SessionRuntimeError,
    SessionRuntimeService,
)
from nexus.services.zones.session_tasks import SessionTaskService
from nexus.storage.models import Base, SessionModel
from nexus.storage.models.auth import ZoneModel


def _env(tmp_name: str):
    engine = sa.create_engine(f"sqlite:///{tempfile.mkdtemp()}/{tmp_name}.db")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    with factory() as s, s.begin():
        s.add(
            ZoneModel(
                zone_id="zone-a",
                name="zone-a",
                phase="Active",
                finalizers="[]",
                canonical_status="active",
                canonical_revision="r1",
            )
        )
        s.add(
            SessionModel(
                session_id="sess-1",
                home_zone_id="zone-a",
                state="active",
                owner={},
                created_by={},
                policy_version="p1a-default",
            )
        )

    def fs_write(path, buf, zone_id):
        return len(buf)

    runtime = SessionRuntimeService(factory, fs_writer=fs_write)
    tasks = SessionTaskService(factory, fs_writer=fs_write)
    return factory, runtime, tasks


def _start(runtime: SessionRuntimeService, pid: str = "pid-1"):
    return runtime.start_run(pid=pid, session_id="sess-1")


def test_cancel_cannot_resurrect_a_terminal_run():
    _factory, runtime, _tasks = _env("terminal")
    _start(runtime)
    runtime.cancel_run(pid="pid-1", mode="terminate")
    with pytest.raises(SessionRuntimeError) as exc:
        runtime.cancel_run(pid="pid-1", mode="pending")
    assert exc.value.code == "RUN_ALREADY_TERMINAL"


def test_parked_run_blocks_new_runs_and_attempts():
    _factory, runtime, tasks = _env("pending")
    _start(runtime, "pid-1")
    runtime.cancel_run(pid="pid-1", mode="pending")
    with pytest.raises(SessionRuntimeError) as exc:
        _start(runtime, "pid-2")
    assert exc.value.code == "REVOCATION_PENDING"
    # task creation is resource acquisition too (contract parity)
    with pytest.raises(SessionRuntimeError) as task_exc:
        tasks.ensure_implicit_task(
            session_id="sess-1",
            requested_by={"subject_type": "user", "subject_id": "u1"},
            resource_refs=[],
        )
    assert task_exc.value.code == "REVOCATION_PENDING"


def test_distinct_record_names_get_distinct_vfs_paths():
    _factory, runtime, _tasks = _env("records")
    paths: list[str] = []

    def fs_write(path, buf, zone_id):
        paths.append(path)
        return len(buf)

    runtime._fs_writer = fs_write
    runtime.write_session_record(
        session_id="sess-1", record_kind="transcript", payload=b"one", record_name="a"
    )
    runtime.write_session_record(
        session_id="sess-1", record_kind="transcript", payload=b"two", record_name="b"
    )
    # default name keeps the canonical path
    runtime.write_session_record(
        session_id="sess-1", record_kind="transcript", payload=b"def"
    )
    assert paths == [
        "/sessions/sess-1/transcript.jsonl.a",
        "/sessions/sess-1/transcript.jsonl.b",
        "/sessions/sess-1/transcript.jsonl",
    ]


def test_attach_pid_refuses_terminal_attempt():
    _factory, _runtime, tasks = _env("attach")
    task = tasks.ensure_implicit_task(
        session_id="sess-1", requested_by={"subject_type": "user", "subject_id": "u1"}, resource_refs=[]
    )
    ref = tasks.create_attempt(
        task_id=task.task_id, execution_zone_id="zone-a", reason_code="r", reason="r", policy_version=None
    )
    tasks.attach_pid(attempt_id=ref.attempt_id, pid="pid-9")
    from nexus.services.zones.session_tasks import SessionTaskError

    # simulate a terminal attempt (reaper path) then try to re-attach
    with tasks._session_factory() as s:  # noqa: SLF001
        from nexus.storage.models import TaskAttemptModel

        row = s.get(TaskAttemptModel, ref.attempt_id)
        row.state = "cancelled"
        s.commit()
    with pytest.raises(SessionTaskError) as exc:
        tasks.attach_pid(attempt_id=ref.attempt_id, pid="pid-10")
    assert exc.value.code == "ATTEMPT_ALREADY_TERMINAL"


def test_reaper_terminates_stale_parked_and_registered_runs():
    from datetime import UTC, datetime, timedelta

    _factory, runtime, _tasks = _env("reaper")
    _start(runtime, "pid-parked")
    _start(runtime, "pid-orphan")  # client died right after register
    runtime.cancel_run(pid="pid-parked", mode="pending")
    with _factory() as s, s.begin():
        from nexus.storage.models import SessionRuntimeRunModel

        for pid in ("pid-parked", "pid-orphan"):
            run = s.get(SessionRuntimeRunModel, pid)
            run.started_at = datetime.now(UTC) - timedelta(seconds=PARK_STALE_AFTER_S * 2)
    moved = runtime.revalidate_runtime_dependencies(lambda *a: None)
    assert moved >= 2
    with _factory() as s:
        from nexus.storage.models import SessionRuntimeRunModel

        states = {
            r.pid: r.state
            for r in s.execute(sa.select(SessionRuntimeRunModel)).scalars().all()
        }
        assert states["pid-parked"] == "terminated"
        assert states["pid-orphan"] == "terminated"
