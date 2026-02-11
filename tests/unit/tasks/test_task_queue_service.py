"""Unit tests for TaskQueueService."""

import pytest

try:
    import _nexus_tasks  # noqa: F401

    HAS_NEXUS_TASKS = True
except ImportError:
    HAS_NEXUS_TASKS = False

from nexus.services.task_queue_service import TaskQueueService

pytestmark = pytest.mark.skipif(
    not HAS_NEXUS_TASKS,
    reason="nexus_tasks Rust extension not available",
)


@pytest.fixture()
def service(tmp_path):
    """Create a fresh TaskQueueService for each test."""
    return TaskQueueService(db_path=str(tmp_path / "test-tasks-db"))


class TestSubmitTask:
    def test_submit_returns_dict_with_task_id(self, service):
        result = service.submit_task("test.echo", '{"msg": "hello"}')
        assert isinstance(result, dict)
        assert "task_id" in result
        assert isinstance(result["task_id"], int)
        assert result["task_id"] > 0
        assert result["status"] == "pending"
        assert result["task_type"] == "test.echo"

    def test_submit_with_priority(self, service):
        result = service.submit_task("test.echo", "{}", priority=0)
        assert result["task_id"] > 0

    def test_submit_with_max_retries(self, service):
        result = service.submit_task("test.echo", "{}", max_retries=5)
        task = service.get_task(result["task_id"])
        assert task["max_retries"] == 5


class TestGetTask:
    def test_get_existing_task(self, service):
        result = service.submit_task("test.echo", '{"key": "value"}')
        task = service.get_task(result["task_id"])
        assert task is not None
        assert task["task_id"] == result["task_id"]
        assert task["task_type"] == "test.echo"
        assert task["status"] == 0  # PENDING
        assert task["status_name"] == "pending"
        assert task["params"] == '{"key": "value"}'
        assert task["attempt"] == 0

    def test_get_nonexistent_returns_none(self, service):
        assert service.get_task(999999) is None

    def test_get_task_has_all_fields(self, service):
        result = service.submit_task("test.echo", "{}")
        task = service.get_task(result["task_id"])
        expected_fields = {
            "task_id",
            "task_type",
            "params",
            "priority",
            "status",
            "status_name",
            "result",
            "error_message",
            "attempt",
            "max_retries",
            "created_at",
            "run_at",
            "claimed_by",
            "progress_pct",
            "progress_message",
            "completed_at",
        }
        assert expected_fields.issubset(set(task.keys()))


class TestCancelTask:
    def test_cancel_pending_task(self, service):
        result = service.submit_task("test.echo", "{}")
        cancel_result = service.cancel_task(result["task_id"])
        assert cancel_result["success"] is True
        assert cancel_result["task_id"] == result["task_id"]

        # Verify cancelled
        task = service.get_task(result["task_id"])
        assert task["status"] == 5  # CANCELLED

    def test_cancel_nonexistent_returns_failure(self, service):
        cancel_result = service.cancel_task(999999)
        assert cancel_result["success"] is False

    def test_cancel_completed_returns_failure(self, service):
        result = service.submit_task("test.echo", "{}")
        engine = service.get_engine()
        engine.claim_next("w-0", 300)
        engine.complete(result["task_id"])

        cancel_result = service.cancel_task(result["task_id"])
        assert cancel_result["success"] is False


class TestListTasks:
    def test_list_all_tasks(self, service):
        service.submit_task("type_a", "{}")
        service.submit_task("type_b", "{}")
        service.submit_task("type_a", "{}")

        tasks = service.list_tasks()
        assert len(tasks) == 3
        assert all(isinstance(t, dict) for t in tasks)

    def test_list_by_type(self, service):
        service.submit_task("type_a", "{}")
        service.submit_task("type_b", "{}")
        service.submit_task("type_a", "{}")

        tasks = service.list_tasks(task_type="type_a")
        assert len(tasks) == 2

    def test_list_with_limit(self, service):
        for i in range(5):
            service.submit_task(f"task_{i}", "{}")

        tasks = service.list_tasks(limit=3)
        assert len(tasks) == 3

    def test_list_with_offset(self, service):
        for i in range(5):
            service.submit_task(f"task_{i}", "{}")

        tasks = service.list_tasks(limit=2, offset=3)
        assert len(tasks) == 2

    def test_list_empty_returns_empty(self, service):
        tasks = service.list_tasks()
        assert tasks == []


class TestGetTaskStats:
    def test_stats_empty_queue(self, service):
        stats = service.get_task_stats()
        assert isinstance(stats, dict)
        assert stats["pending"] == 0
        assert stats["running"] == 0
        assert stats["completed"] == 0
        assert stats["failed"] == 0
        assert stats["dead_letter"] == 0

    def test_stats_with_tasks(self, service):
        service.submit_task("a", "{}")
        service.submit_task("b", "{}")

        stats = service.get_task_stats()
        assert stats["pending"] == 2
        assert stats["running"] == 0

    def test_stats_after_claim(self, service):
        service.submit_task("a", "{}")
        service.submit_task("b", "{}")

        engine = service.get_engine()
        engine.claim_next("w-0", 300)

        stats = service.get_task_stats()
        assert stats["pending"] == 1
        assert stats["running"] == 1


class TestLazyInit:
    def test_engine_created_on_first_use(self, tmp_path):
        service = TaskQueueService(db_path=str(tmp_path / "lazy-db"))
        assert service._engine is None

        # Trigger lazy init
        service.submit_task("test", "{}")
        assert service._engine is not None

    def test_engine_reused(self, service):
        service.submit_task("test", "{}")
        engine1 = service._engine
        service.submit_task("test", "{}")
        engine2 = service._engine
        assert engine1 is engine2
