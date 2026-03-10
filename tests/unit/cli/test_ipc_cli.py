"""Tests for nexus ipc CLI commands."""

from __future__ import annotations

import json
import os
from contextlib import ExitStack
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from nexus.cli.clients.base import BaseServiceClient
from nexus.cli.clients.ipc import IPCClient
from nexus.cli.commands.ipc import ipc

MOCK_URL = "http://localhost:2026"


def _patch_client(**method_returns: object) -> tuple[ExitStack, dict[str, MagicMock]]:
    """Patch BaseServiceClient so IPCClient works without httpx."""
    stack = ExitStack()
    stack.enter_context(patch.dict(os.environ, {"NEXUS_NO_AUTO_JSON": "1"}))
    stack.enter_context(patch.object(BaseServiceClient, "__init__", lambda self, *a, **kw: None))
    stack.enter_context(patch.object(BaseServiceClient, "__enter__", lambda self: self))
    stack.enter_context(patch.object(BaseServiceClient, "__exit__", lambda self, *a: None))
    mocks: dict[str, MagicMock] = {}
    for method_name, return_value in method_returns.items():
        m = stack.enter_context(patch.object(IPCClient, method_name, return_value=return_value))
        mocks[method_name] = m
    return stack, mocks


class TestIPCSend:
    def test_happy_path(self) -> None:
        runner = CliRunner()
        stack, _ = _patch_client(send={"message_id": "msg_123"})
        with stack:
            result = runner.invoke(ipc, ["send", "bob", "Hello", "--remote-url", MOCK_URL])
        assert result.exit_code == 0
        assert "msg_123" in result.output

    def test_json_output(self) -> None:
        runner = CliRunner()
        stack, _ = _patch_client(send={"message_id": "msg_123"})
        with stack:
            result = runner.invoke(
                ipc, ["send", "bob", "Hello", "--remote-url", MOCK_URL, "--json"]
            )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["data"]["message_id"] == "msg_123"

    def test_default_type_is_task(self) -> None:
        runner = CliRunner()
        stack, mocks = _patch_client(send={"message_id": "msg_456"})
        with stack:
            runner.invoke(ipc, ["send", "bob", "Hello", "--remote-url", MOCK_URL])
        mocks["send"].assert_called_once_with("bob", "Hello", message_type="task", zone_id=None)

    def test_with_type_and_zone(self) -> None:
        runner = CliRunner()
        stack, mocks = _patch_client(send={"message_id": "msg_456"})
        with stack:
            result = runner.invoke(
                ipc,
                [
                    "send",
                    "bob",
                    "cancel",
                    "--type",
                    "cancel",
                    "--zone-id",
                    "org_1",
                    "--remote-url",
                    MOCK_URL,
                ],
            )
        assert result.exit_code == 0
        mocks["send"].assert_called_once_with(
            "bob", "cancel", message_type="cancel", zone_id="org_1"
        )

    def test_missing_url_fails(self) -> None:
        runner = CliRunner()
        result = runner.invoke(ipc, ["send", "bob", "Hello"])
        assert result.exit_code != 0


class TestIPCInbox:
    def test_happy_path(self) -> None:
        runner = CliRunner()
        stack, _ = _patch_client(
            inbox={
                "messages": [
                    {
                        "message_id": "msg_1",
                        "from_agent": "alice",
                        "message_type": "task",
                        "body": "hello",
                        "created_at": "2025-01-01T00:00:00",
                    }
                ]
            }
        )
        with stack:
            result = runner.invoke(ipc, ["inbox", "bob", "--remote-url", MOCK_URL])
        assert result.exit_code == 0

    def test_empty_inbox(self) -> None:
        runner = CliRunner()
        stack, _ = _patch_client(inbox={"messages": []})
        with stack:
            result = runner.invoke(ipc, ["inbox", "bob", "--remote-url", MOCK_URL])
        assert result.exit_code == 0
        assert "empty" in result.output.lower()

    def test_default_limit_is_50(self) -> None:
        runner = CliRunner()
        stack, mocks = _patch_client(inbox={"messages": []})
        with stack:
            runner.invoke(ipc, ["inbox", "bob", "--remote-url", MOCK_URL])
        mocks["inbox"].assert_called_once_with("bob", limit=50)

    def test_with_limit(self) -> None:
        runner = CliRunner()
        stack, mocks = _patch_client(inbox={"messages": []})
        with stack:
            result = runner.invoke(ipc, ["inbox", "bob", "--limit", "5", "--remote-url", MOCK_URL])
        assert result.exit_code == 0
        mocks["inbox"].assert_called_once_with("bob", limit=5)

    def test_json_output(self) -> None:
        runner = CliRunner()
        stack, _ = _patch_client(
            inbox={
                "messages": [
                    {
                        "message_id": "msg_1",
                        "from_agent": "alice",
                        "message_type": "task",
                        "body": "hello",
                        "created_at": "2025-01-01T00:00:00",
                    }
                ]
            }
        )
        with stack:
            result = runner.invoke(ipc, ["inbox", "bob", "--remote-url", MOCK_URL, "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert len(data["data"]["messages"]) == 1


class TestIPCCount:
    def test_happy_path(self) -> None:
        runner = CliRunner()
        stack, _ = _patch_client(inbox_count={"count": 42})
        with stack:
            result = runner.invoke(ipc, ["count", "bob", "--remote-url", MOCK_URL])
        assert result.exit_code == 0
        assert "42" in result.output

    def test_json_output(self) -> None:
        runner = CliRunner()
        stack, _ = _patch_client(inbox_count={"count": 7})
        with stack:
            result = runner.invoke(ipc, ["count", "bob", "--remote-url", MOCK_URL, "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["data"]["count"] == 7

    def test_client_called_with_agent_id(self) -> None:
        runner = CliRunner()
        stack, mocks = _patch_client(inbox_count={"count": 0})
        with stack:
            runner.invoke(ipc, ["count", "alice", "--remote-url", MOCK_URL])
        mocks["inbox_count"].assert_called_once_with("alice")
