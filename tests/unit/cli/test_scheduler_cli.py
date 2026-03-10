"""Tests for nexus scheduler CLI commands."""

from __future__ import annotations

import json
from contextlib import ExitStack, contextmanager
from typing import Any
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from nexus.cli.clients.scheduler import SchedulerClient
from nexus.cli.commands.scheduler_cli import scheduler

MOCK_URL = "http://localhost:2026"
_ENV = {"NEXUS_NO_AUTO_JSON": "1"}


@contextmanager
def _mock_client(**overrides: Any):
    """Patch SchedulerClient so service_command uses a mock instance.

    The @service_command decorator captures client_class in a closure at import
    time, so patching the module-level name has no effect.  Instead we patch the
    class methods directly via ``patch.object``.
    """
    with ExitStack() as stack:
        stack.enter_context(patch.object(SchedulerClient, "__init__", lambda self, **kw: None))
        stack.enter_context(patch.object(SchedulerClient, "__enter__", lambda self: self))
        stack.enter_context(patch.object(SchedulerClient, "__exit__", lambda self, *a: False))
        mocks: dict[str, MagicMock] = {}
        for name, retval in overrides.items():
            m = MagicMock(return_value=retval)
            stack.enter_context(patch.object(SchedulerClient, name, m))
            mocks[name] = m
        yield mocks


class TestSchedulerStatus:
    def test_happy_path(self) -> None:
        runner = CliRunner(env=_ENV)
        with _mock_client(
            status={
                "queue_depth": 5,
                "active_workers": 3,
                "throughput": 12,
                "avg_wait_ms": 150,
            }
        ):
            result = runner.invoke(scheduler, ["status", "--remote-url", MOCK_URL])
        assert result.exit_code == 0
        assert "5" in result.output

    def test_json_output(self) -> None:
        runner = CliRunner(env=_ENV)
        with _mock_client(
            status={
                "queue_depth": 5,
                "active_workers": 3,
                "throughput": 12,
                "avg_wait_ms": 150,
            }
        ):
            result = runner.invoke(scheduler, ["status", "--remote-url", MOCK_URL, "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["data"]["queue_depth"] == 5

    def test_missing_url_fails(self) -> None:
        runner = CliRunner(env=_ENV)
        result = runner.invoke(scheduler, ["status"])
        assert result.exit_code != 0


class TestSchedulerQueue:
    def test_happy_path_with_tasks(self) -> None:
        runner = CliRunner(env=_ENV)
        with _mock_client(
            status={
                "pending_tasks": [
                    {
                        "task_id": "t1",
                        "priority_class": "high",
                        "agent_id": "alice",
                        "submitted_at": "2025-01-01T00:00:00",
                    }
                ]
            }
        ):
            result = runner.invoke(scheduler, ["queue", "--remote-url", MOCK_URL])
        assert result.exit_code == 0

    def test_no_pending_tasks(self) -> None:
        runner = CliRunner(env=_ENV)
        with _mock_client(status={"pending_tasks": []}):
            result = runner.invoke(scheduler, ["queue", "--remote-url", MOCK_URL])
        assert result.exit_code == 0
        assert "No pending tasks" in result.output

    def test_calls_status(self) -> None:
        runner = CliRunner(env=_ENV)
        with _mock_client(status={"pending_tasks": []}) as mocks:
            runner.invoke(scheduler, ["queue", "--remote-url", MOCK_URL])
        mocks["status"].assert_called_once()


class TestSchedulerPause:
    def test_happy_path(self) -> None:
        runner = CliRunner(env=_ENV)
        with _mock_client(pause={"status": "paused"}):
            result = runner.invoke(scheduler, ["pause", "--remote-url", MOCK_URL])
        assert result.exit_code == 0
        assert "paused" in result.output.lower()

    def test_json_output(self) -> None:
        runner = CliRunner(env=_ENV)
        with _mock_client(pause={"status": "paused"}):
            result = runner.invoke(scheduler, ["pause", "--remote-url", MOCK_URL, "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["data"]["status"] == "paused"


class TestSchedulerResume:
    def test_happy_path(self) -> None:
        runner = CliRunner(env=_ENV)
        with _mock_client(resume={"status": "running"}):
            result = runner.invoke(scheduler, ["resume", "--remote-url", MOCK_URL])
        assert result.exit_code == 0
        assert "resumed" in result.output.lower()

    def test_json_output(self) -> None:
        runner = CliRunner(env=_ENV)
        with _mock_client(resume={"status": "running"}):
            result = runner.invoke(scheduler, ["resume", "--remote-url", MOCK_URL, "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["data"]["status"] == "running"
