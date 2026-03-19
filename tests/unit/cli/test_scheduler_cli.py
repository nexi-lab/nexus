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

_METRICS_RESPONSE: dict[str, Any] = {
    "queue_by_class": [
        {"priority_class": "high", "cnt": 5, "avg_wait": "2.3s", "max_wait": "8.1s"},
        {"priority_class": "low", "cnt": 12, "avg_wait": "10.5s", "max_wait": "45.0s"},
    ],
    "fair_share": {
        "agent-alice": {"running_count": 2, "max_concurrent": 4, "available_slots": 2},
        "agent-bob": {"running_count": 0, "max_concurrent": 4, "available_slots": 4},
    },
    "use_hrrn": True,
}


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
        with _mock_client(status=_METRICS_RESPONSE):
            result = runner.invoke(scheduler, ["status", "--remote-url", MOCK_URL])
        assert result.exit_code == 0
        assert "high" in result.output
        assert "5" in result.output
        assert "HRRN" in result.output

    def test_json_output(self) -> None:
        runner = CliRunner(env=_ENV)
        with _mock_client(status=_METRICS_RESPONSE):
            result = runner.invoke(scheduler, ["status", "--remote-url", MOCK_URL, "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["data"]["use_hrrn"] is True
        assert len(data["data"]["queue_by_class"]) == 2

    def test_empty_queue(self) -> None:
        runner = CliRunner(env=_ENV)
        with _mock_client(status={"queue_by_class": [], "fair_share": {}, "use_hrrn": False}):
            result = runner.invoke(scheduler, ["status", "--remote-url", MOCK_URL])
        assert result.exit_code == 0
        assert "No queued tasks" in result.output

    def test_missing_url_fails(self) -> None:
        runner = CliRunner(env=_ENV)
        result = runner.invoke(scheduler, ["status"])
        assert result.exit_code != 0


class TestSchedulerQueue:
    def test_happy_path_with_classes(self) -> None:
        runner = CliRunner(env=_ENV)
        with _mock_client(status=_METRICS_RESPONSE):
            result = runner.invoke(scheduler, ["queue", "--remote-url", MOCK_URL])
        assert result.exit_code == 0
        assert "high" in result.output
        assert "low" in result.output

    def test_empty_queue(self) -> None:
        runner = CliRunner(env=_ENV)
        with _mock_client(status={"queue_by_class": [], "fair_share": {}, "use_hrrn": False}):
            result = runner.invoke(scheduler, ["queue", "--remote-url", MOCK_URL])
        assert result.exit_code == 0
        assert "No queued tasks" in result.output

    def test_calls_status(self) -> None:
        runner = CliRunner(env=_ENV)
        with _mock_client(status=_METRICS_RESPONSE) as mocks:
            runner.invoke(scheduler, ["queue", "--remote-url", MOCK_URL])
        mocks["status"].assert_called_once()

    def test_json_output(self) -> None:
        runner = CliRunner(env=_ENV)
        with _mock_client(status=_METRICS_RESPONSE):
            result = runner.invoke(scheduler, ["queue", "--remote-url", MOCK_URL, "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert len(data["data"]["queue_by_class"]) == 2
