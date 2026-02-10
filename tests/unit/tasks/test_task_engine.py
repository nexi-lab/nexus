"""Contract tests for the Rust TaskEngine via PyO3 bindings."""

import tempfile

import pytest

try:
    from _nexus_tasks import TaskEngine

    HAS_NEXUS_TASKS = True
except ImportError:
    HAS_NEXUS_TASKS = False

pytestmark = pytest.mark.skipif(
    not HAS_NEXUS_TASKS,
    reason="nexus_tasks Rust extension not available",
)


@pytest.fixture()
def engine(tmp_path):
    """Create a fresh TaskEngine for each test."""
    return TaskEngine(str(tmp_path / "tasks-db"), max_pending=100, max_wait_secs=300)


class TestSubmitAndStatus:
    def test_submit_returns_task_id(self, engine):
        tid = engine.submit("test.echo", b'{"msg": "hello"}')
        assert isinstance(tid, int)
        assert tid > 0

    def test_status_returns_pending(self, engine):
        tid = engine.submit("test.echo", b"data")
        task = engine.status(tid)
        assert task is not None
        assert task.task_id == tid
        assert task.status == 0  # PENDING
        assert task.task_type == "test.echo"
        assert task.params == b"data"
        assert task.attempt == 0

    def test_status_nonexistent_returns_none(self, engine):
        assert engine.status(999999) is None


class TestClaimNext:
    def test_claim_returns_task(self, engine):
        tid = engine.submit("test.echo", b"data")
        task = engine.claim_next("w-0", 300)
        assert task is not None
        assert task.task_id == tid
        assert task.status == 1  # RUNNING
        assert task.attempt == 1
        assert task.claimed_by == "w-0"

    def test_claim_empty_returns_none(self, engine):
        assert engine.claim_next("w-0", 300) is None

    def test_claim_priority_order(self, engine):
        engine.submit("low", b"", priority=3)  # Low
        engine.submit("critical", b"", priority=0)  # Critical
        engine.submit("normal", b"", priority=2)  # Normal

        t1 = engine.claim_next("w-0", 300)
        assert t1.task_type == "critical"
        t2 = engine.claim_next("w-0", 300)
        assert t2.task_type == "normal"
        t3 = engine.claim_next("w-0", 300)
        assert t3.task_type == "low"


class TestCompleteLifecycle:
    def test_complete_happy_path(self, engine):
        tid = engine.submit("test", b"input")
        engine.claim_next("w-0", 300)
        engine.complete(tid, b"result")

        task = engine.status(tid)
        assert task.status == 2  # COMPLETED
        assert task.result == b"result"
        assert task.completed_at is not None

    def test_complete_without_result(self, engine):
        tid = engine.submit("test", b"input")
        engine.claim_next("w-0", 300)
        engine.complete(tid)

        task = engine.status(tid)
        assert task.status == 2  # COMPLETED


class TestFailAndRetry:
    def test_fail_retries(self, engine):
        tid = engine.submit("test", b"", max_retries=3)
        engine.claim_next("w-0", 300)
        engine.fail(tid, "transient error")

        task = engine.status(tid)
        assert task.status == 0  # Back to PENDING
        assert task.attempt == 1
        assert task.error_message == "transient error"

    def test_fail_dead_letter(self, engine):
        tid = engine.submit("test", b"", max_retries=1)
        engine.claim_next("w-0", 300)
        engine.fail(tid, "fatal error")

        task = engine.status(tid)
        assert task.status == 4  # DEAD_LETTER

    def test_fail_max_retries_zero_immediate_dead_letter(self, engine):
        tid = engine.submit("test", b"", max_retries=0)
        engine.claim_next("w-0", 300)
        engine.fail(tid, "no retries")

        task = engine.status(tid)
        assert task.status == 4  # DEAD_LETTER


class TestCancel:
    def test_cancel_pending(self, engine):
        tid = engine.submit("test", b"")
        engine.cancel(tid)

        task = engine.status(tid)
        assert task.status == 5  # CANCELLED
        assert engine.claim_next("w-0", 300) is None

    def test_cancel_running(self, engine):
        tid = engine.submit("test", b"")
        engine.claim_next("w-0", 300)
        engine.cancel(tid)

        task = engine.status(tid)
        assert task.status == 5  # CANCELLED


class TestHeartbeat:
    def test_heartbeat_active_returns_true(self, engine):
        tid = engine.submit("test", b"")
        engine.claim_next("w-0", 300)

        alive = engine.heartbeat(tid, 50, "halfway")
        assert alive is True

        task = engine.status(tid)
        assert task.progress_pct == 50
        assert task.progress_message == "halfway"

    def test_heartbeat_cancelled_returns_false(self, engine):
        tid = engine.submit("test", b"")
        engine.claim_next("w-0", 300)
        engine.cancel(tid)

        alive = engine.heartbeat(tid, 50, "check")
        assert alive is False


class TestAdmissionControl:
    def test_queue_full_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            eng = TaskEngine(str(tmp), max_pending=2, max_wait_secs=0)
            eng.submit("a", b"")
            eng.submit("b", b"")
            with pytest.raises(RuntimeError, match="queue full"):
                eng.submit("c", b"")


class TestStats:
    def test_stats_reflect_queue_state(self, engine):
        engine.submit("a", b"")
        engine.submit("b", b"")

        stats = engine.stats()
        assert stats.pending == 2
        assert stats.running == 0
        assert stats.completed == 0

        engine.claim_next("w-0", 300)

        stats = engine.stats()
        assert stats.pending == 1
        assert stats.running == 1


class TestListTasks:
    def test_list_all(self, engine):
        engine.submit("a", b"")
        engine.submit("b", b"")
        engine.submit("a", b"")

        tasks = engine.list_tasks()
        assert len(tasks) == 3

    def test_list_by_type(self, engine):
        engine.submit("type_a", b"")
        engine.submit("type_b", b"")
        engine.submit("type_a", b"")

        tasks = engine.list_tasks(task_type="type_a")
        assert len(tasks) == 2

    def test_list_pagination(self, engine):
        for i in range(5):
            engine.submit(f"task_{i}", b"")

        page1 = engine.list_tasks(limit=2, offset=0)
        assert len(page1) == 2
        page2 = engine.list_tasks(limit=2, offset=2)
        assert len(page2) == 2
        page3 = engine.list_tasks(limit=2, offset=4)
        assert len(page3) == 1


class TestRequeueAbandoned:
    def test_requeue_returns_count(self, engine):
        engine.submit("test", b"")
        engine.claim_next("w-0", 1)  # 1s lease

        # Immediately check — lease hasn't expired yet in real time
        # but internally the lease is clock-based
        count = engine.requeue_abandoned()
        # Count depends on timing, just verify it returns an int
        assert isinstance(count, int)


class TestPersistence:
    def test_data_survives_reopen(self, tmp_path):
        db_path = str(tmp_path / "persist-db")
        tid = None

        # Create and write
        eng1 = TaskEngine(db_path)
        tid = eng1.submit("persist_test", b"payload")
        del eng1  # Close

        # Reopen and verify
        eng2 = TaskEngine(db_path)
        task = eng2.status(tid)
        assert task is not None
        assert task.task_type == "persist_test"
        assert task.params == b"payload"


class TestInvalidInput:
    def test_invalid_priority_raises(self, engine):
        with pytest.raises(RuntimeError, match="invalid priority"):
            engine.submit("test", b"", priority=99)

    def test_invalid_status_filter_raises(self, engine):
        with pytest.raises(RuntimeError, match="invalid status"):
            engine.list_tasks(status=99)

    def test_complete_wrong_state_raises(self, engine):
        tid = engine.submit("test", b"")
        with pytest.raises(RuntimeError, match="invalid state transition"):
            engine.complete(tid, b"result")  # Not yet claimed

    def test_fail_wrong_state_raises(self, engine):
        tid = engine.submit("test", b"")
        with pytest.raises(RuntimeError, match="invalid state transition"):
            engine.fail(tid, "error")  # Not yet claimed

    def test_complete_not_found_raises(self, engine):
        with pytest.raises(RuntimeError, match="not found"):
            engine.complete(999, b"")

    def test_cancel_terminal_raises(self, engine):
        tid = engine.submit("test", b"")
        engine.claim_next("w-0", 300)
        engine.complete(tid, b"done")
        with pytest.raises(RuntimeError, match="invalid state transition"):
            engine.cancel(tid)


class TestTaskRecordRepr:
    def test_repr_format(self, engine):
        tid = engine.submit("test.echo", b"data", max_retries=5)
        task = engine.status(tid)
        r = repr(task)
        assert "TaskRecord" in r
        assert str(tid) in r
        assert "test.echo" in r
