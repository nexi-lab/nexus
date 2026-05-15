"""Tests for nexus secrets-audit CLI commands."""

from __future__ import annotations

import json
import os
from contextlib import ExitStack
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from nexus.cli.clients.base import BaseServiceClient
from nexus.cli.clients.secrets_audit import SecretsAuditClient
from nexus.cli.commands.secrets_audit import secrets_audit

MOCK_URL = "http://localhost:2026"


def _patch_client(**method_returns: object) -> tuple[ExitStack, dict[str, MagicMock]]:
    """Patch BaseServiceClient so SecretsAuditClient can be instantiated without httpx.

    Returns an ExitStack context manager and a dict of method mocks.
    """
    stack = ExitStack()
    stack.enter_context(patch.dict(os.environ, {"NEXUS_NO_AUTO_JSON": "1"}))
    stack.enter_context(patch.object(BaseServiceClient, "__init__", lambda self, *a, **kw: None))
    stack.enter_context(patch.object(BaseServiceClient, "__enter__", lambda self: self))
    stack.enter_context(patch.object(BaseServiceClient, "__exit__", lambda self, *a: None))
    mocks: dict[str, MagicMock] = {}
    for method_name, return_value in method_returns.items():
        m = stack.enter_context(
            patch.object(SecretsAuditClient, method_name, return_value=return_value)
        )
        mocks[method_name] = m
    return stack, mocks


class TestSecretsAuditList:
    def test_happy_path(self) -> None:
        runner = CliRunner()
        stack, _ = _patch_client(
            list={
                "events": [
                    {
                        "record_id": "rec_001",
                        "action": "read",
                        "secret_name": "DB_PASSWORD",
                        "agent_id": "agent_alice",
                        "timestamp": "2025-01-01T00:00:00Z",
                    }
                ]
            }
        )
        with stack:
            result = runner.invoke(secrets_audit, ["list", "--remote-url", MOCK_URL])
        assert result.exit_code == 0

    def test_json_output(self) -> None:
        runner = CliRunner()
        stack, _ = _patch_client(
            list={
                "events": [{"record_id": "rec_001", "action": "read", "secret_name": "DB_PASSWORD"}]
            }
        )
        with stack:
            result = runner.invoke(secrets_audit, ["list", "--remote-url", MOCK_URL, "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert len(data["data"]["events"]) == 1

    def test_client_args_defaults(self) -> None:
        runner = CliRunner()
        stack, mocks = _patch_client(list={"events": []})
        with stack:
            runner.invoke(secrets_audit, ["list", "--remote-url", MOCK_URL])
        mocks["list"].assert_called_once_with(since=None, action=None, limit=50)

    def test_client_args_with_options(self) -> None:
        runner = CliRunner()
        stack, mocks = _patch_client(list={"events": []})
        with stack:
            runner.invoke(
                secrets_audit,
                [
                    "list",
                    "--since",
                    "1h",
                    "--action",
                    "read",
                    "--limit",
                    "10",
                    "--remote-url",
                    MOCK_URL,
                ],
            )
        mocks["list"].assert_called_once_with(since="1h", action="read", limit=10)

    def test_empty_results(self) -> None:
        runner = CliRunner()
        stack, _ = _patch_client(list={"events": []})
        with stack:
            result = runner.invoke(secrets_audit, ["list", "--remote-url", MOCK_URL])
        assert result.exit_code == 0
        assert "No secret access events" in result.output

    def test_missing_url_exits_nonzero(self) -> None:
        runner = CliRunner()
        result = runner.invoke(secrets_audit, ["list"])
        assert result.exit_code != 0


class TestSecretsAuditVerify:
    def test_happy_path(self) -> None:
        runner = CliRunner()
        stack, _ = _patch_client(verify={"valid": True, "hash": "abc123"})
        with stack:
            result = runner.invoke(secrets_audit, ["verify", "rec_001", "--remote-url", MOCK_URL])
        assert result.exit_code == 0
        assert "Valid" in result.output

    def test_json_output(self) -> None:
        runner = CliRunner()
        stack, _ = _patch_client(verify={"valid": True, "hash": "abc123"})
        with stack:
            result = runner.invoke(
                secrets_audit,
                ["verify", "rec_001", "--remote-url", MOCK_URL, "--json"],
            )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["data"]["valid"] is True

    def test_client_args(self) -> None:
        runner = CliRunner()
        stack, mocks = _patch_client(verify={"valid": True})
        with stack:
            runner.invoke(secrets_audit, ["verify", "rec_001", "--remote-url", MOCK_URL])
        mocks["verify"].assert_called_once_with("rec_001")

    def test_tampered_record(self) -> None:
        runner = CliRunner()
        stack, _ = _patch_client(verify={"valid": False})
        with stack:
            result = runner.invoke(secrets_audit, ["verify", "rec_001", "--remote-url", MOCK_URL])
        assert result.exit_code == 0
        assert "Tampered" in result.output

    def test_missing_url_exits_nonzero(self) -> None:
        runner = CliRunner()
        result = runner.invoke(secrets_audit, ["verify", "rec_001"])
        assert result.exit_code != 0
