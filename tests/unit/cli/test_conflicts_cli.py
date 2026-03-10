"""Tests for nexus conflicts CLI commands."""

from __future__ import annotations

import json
from contextlib import ExitStack, contextmanager
from typing import Any
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from nexus.cli.clients.conflicts import ConflictsClient
from nexus.cli.commands.conflicts import conflicts

MOCK_URL = "http://localhost:2026"
_ENV = {"NEXUS_NO_AUTO_JSON": "1"}


@contextmanager
def _mock_client(**overrides: Any):
    """Patch ConflictsClient so service_command uses a mock instance.

    The @service_command decorator captures client_class in a closure at import
    time, so patching the module-level name has no effect.  Instead we patch the
    class methods directly via ``patch.object``.
    """
    with ExitStack() as stack:
        stack.enter_context(patch.object(ConflictsClient, "__init__", lambda self, **kw: None))
        stack.enter_context(patch.object(ConflictsClient, "__enter__", lambda self: self))
        stack.enter_context(patch.object(ConflictsClient, "__exit__", lambda self, *a: False))
        mocks: dict[str, MagicMock] = {}
        for name, retval in overrides.items():
            m = MagicMock(return_value=retval)
            stack.enter_context(patch.object(ConflictsClient, name, m))
            mocks[name] = m
        yield mocks


class TestConflictsList:
    def test_happy_path(self) -> None:
        runner = CliRunner(env=_ENV)
        with _mock_client(
            list={
                "conflicts": [
                    {
                        "conflict_id": "c1",
                        "path": "/file.txt",
                        "backend_name": "s3-main",
                        "status": "unresolved",
                    }
                ]
            }
        ):
            result = runner.invoke(conflicts, ["list", "--remote-url", MOCK_URL])
        assert result.exit_code == 0

    def test_no_conflicts(self) -> None:
        runner = CliRunner(env=_ENV)
        with _mock_client(list={"conflicts": []}):
            result = runner.invoke(conflicts, ["list", "--remote-url", MOCK_URL])
        assert result.exit_code == 0
        assert "No unresolved conflicts" in result.output

    def test_json_output(self) -> None:
        runner = CliRunner(env=_ENV)
        with _mock_client(
            list={
                "conflicts": [{"conflict_id": "c1", "path": "/file.txt", "backend_name": "s3-main"}]
            }
        ):
            result = runner.invoke(conflicts, ["list", "--remote-url", MOCK_URL, "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert len(data["data"]["conflicts"]) == 1

    def test_missing_url_fails(self) -> None:
        runner = CliRunner(env=_ENV)
        result = runner.invoke(conflicts, ["list"])
        assert result.exit_code != 0


class TestConflictsShow:
    def test_happy_path(self) -> None:
        runner = CliRunner(env=_ENV)
        with _mock_client(
            show={
                "path": "/file.txt",
                "backend_name": "s3-main",
                "strategy": "last-writer-wins",
                "outcome": "nexus_wins",
                "status": "resolved",
                "resolved_at": "2025-01-01T12:00:00",
                "nexus_content_hash": "abc123",
                "backend_content_hash": "def456",
            }
        ):
            result = runner.invoke(conflicts, ["show", "c1", "--remote-url", MOCK_URL])
        assert result.exit_code == 0
        assert "/file.txt" in result.output

    def test_json_output(self) -> None:
        runner = CliRunner(env=_ENV)
        with _mock_client(
            show={
                "path": "/file.txt",
                "backend_name": "s3-main",
                "strategy": "last-writer-wins",
                "outcome": "nexus_wins",
                "status": "resolved",
            }
        ):
            result = runner.invoke(conflicts, ["show", "c1", "--remote-url", MOCK_URL, "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["data"]["backend_name"] == "s3-main"

    def test_client_called_with_id(self) -> None:
        runner = CliRunner(env=_ENV)
        with _mock_client(show={"path": "/file.txt"}) as mocks:
            runner.invoke(conflicts, ["show", "c_abc", "--remote-url", MOCK_URL])
        mocks["show"].assert_called_once_with("c_abc")


class TestConflictsResolve:
    def test_happy_path(self) -> None:
        runner = CliRunner(env=_ENV)
        with _mock_client(resolve={}) as mocks:
            result = runner.invoke(
                conflicts,
                ["resolve", "c1", "--outcome", "nexus_wins", "--remote-url", MOCK_URL],
            )
        assert result.exit_code == 0
        assert "resolved (nexus_wins)" in result.output
        mocks["resolve"].assert_called_once_with("c1", outcome="nexus_wins")

    def test_backend_wins_outcome(self) -> None:
        runner = CliRunner(env=_ENV)
        with _mock_client(resolve={}) as mocks:
            result = runner.invoke(
                conflicts,
                ["resolve", "c1", "--outcome", "backend_wins", "--remote-url", MOCK_URL],
            )
        assert result.exit_code == 0
        mocks["resolve"].assert_called_once_with("c1", outcome="backend_wins")

    def test_outcome_required(self) -> None:
        runner = CliRunner(env=_ENV)
        result = runner.invoke(conflicts, ["resolve", "c1", "--remote-url", MOCK_URL])
        assert result.exit_code != 0

    def test_json_output(self) -> None:
        runner = CliRunner(env=_ENV)
        with _mock_client(resolve={}):
            result = runner.invoke(
                conflicts,
                [
                    "resolve",
                    "c1",
                    "--outcome",
                    "nexus_wins",
                    "--remote-url",
                    MOCK_URL,
                    "--json",
                ],
            )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["data"] is not None
