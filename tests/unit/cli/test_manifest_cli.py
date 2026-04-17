"""Tests for nexus manifest CLI commands."""

from __future__ import annotations

import json
import os
from contextlib import ExitStack
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from nexus.cli.clients.base import BaseServiceClient
from nexus.cli.clients.manifest import ManifestClient
from nexus.cli.commands.manifest_cli import manifest
from nexus.contracts.constants import ROOT_ZONE_ID

MOCK_URL = "http://localhost:2026"


def _patch_client(**method_returns: object) -> tuple[ExitStack, dict[str, MagicMock]]:
    """Patch BaseServiceClient so ManifestClient can be instantiated without httpx.

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
            patch.object(ManifestClient, method_name, return_value=return_value)
        )
        mocks[method_name] = m
    return stack, mocks


class TestManifestCreate:
    def test_happy_path(self) -> None:
        runner = CliRunner()
        stack, _ = _patch_client(
            create={
                "manifest_id": "mfst_123",
                "agent_id": "agent_alice",
                "name": "dev tools",
                "entries": [
                    {"tool_pattern": "read_*", "permission": "allow"},
                    {"tool_pattern": "write_file", "permission": "allow"},
                ],
            }
        )
        with stack:
            result = runner.invoke(
                manifest,
                [
                    "create",
                    "agent_alice",
                    "--name",
                    "dev tools",
                    "--entry",
                    "read_*:allow",
                    "--entry",
                    "write_file:allow",
                    "--remote-url",
                    MOCK_URL,
                ],
            )
        assert result.exit_code == 0
        assert "Manifest created" in result.output

    def test_json_output(self) -> None:
        runner = CliRunner()
        stack, _ = _patch_client(
            create={
                "manifest_id": "mfst_123",
                "agent_id": "agent_alice",
                "name": "dev",
                "entries": [{"tool_pattern": "read_*", "permission": "allow"}],
            }
        )
        with stack:
            result = runner.invoke(
                manifest,
                [
                    "create",
                    "agent_alice",
                    "--name",
                    "dev",
                    "--entry",
                    "read_*:allow",
                    "--remote-url",
                    MOCK_URL,
                    "--json",
                ],
            )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["data"]["manifest_id"] == "mfst_123"

    def test_client_args(self) -> None:
        runner = CliRunner()
        stack, mocks = _patch_client(create={"manifest_id": "mfst_123"})
        with stack:
            runner.invoke(
                manifest,
                [
                    "create",
                    "agent_alice",
                    "--name",
                    "dev",
                    "--entry",
                    "read_*:allow",
                    "--entry",
                    "write_file:deny",
                    "--remote-url",
                    MOCK_URL,
                ],
            )
        mocks["create"].assert_called_once_with(
            "agent_alice",
            name="dev",
            entries=[
                {"tool_pattern": "read_*", "permission": "allow"},
                {"tool_pattern": "write_file", "permission": "deny"},
            ],
            zone_id=ROOT_ZONE_ID,
            valid_hours=720,
        )

    def test_missing_url_exits_nonzero(self) -> None:
        runner = CliRunner()
        result = runner.invoke(
            manifest, ["create", "agent_alice", "--name", "dev", "--entry", "read_*:allow"]
        )
        assert result.exit_code != 0


class TestManifestList:
    def test_happy_path(self) -> None:
        runner = CliRunner()
        stack, _ = _patch_client(
            list={
                "manifests": [
                    {
                        "manifest_id": "mfst_123",
                        "agent_id": "agent_alice",
                        "sources": ["/data"],
                        "status": "active",
                        "created_at": "2025-01-01T00:00:00Z",
                    }
                ]
            }
        )
        with stack:
            result = runner.invoke(manifest, ["list", "--remote-url", MOCK_URL])
        assert result.exit_code == 0

    def test_json_output(self) -> None:
        runner = CliRunner()
        stack, _ = _patch_client(
            list={"manifests": [{"manifest_id": "mfst_123", "agent_id": "agent_alice"}]}
        )
        with stack:
            result = runner.invoke(manifest, ["list", "--remote-url", MOCK_URL, "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert len(data["data"]["manifests"]) == 1

    def test_empty_results(self) -> None:
        runner = CliRunner()
        stack, _ = _patch_client(list={"manifests": []})
        with stack:
            result = runner.invoke(manifest, ["list", "--remote-url", MOCK_URL])
        assert result.exit_code == 0
        assert "No manifests" in result.output

    def test_missing_url_exits_nonzero(self) -> None:
        runner = CliRunner()
        result = runner.invoke(manifest, ["list"])
        assert result.exit_code != 0


class TestManifestShow:
    def test_happy_path(self) -> None:
        runner = CliRunner()
        stack, _ = _patch_client(
            show={
                "manifest_id": "mfst_123",
                "agent_id": "agent_alice",
                "status": "active",
                "created_at": "2025-01-01T00:00:00Z",
                "sources": ["/data"],
            }
        )
        with stack:
            result = runner.invoke(manifest, ["show", "mfst_123", "--remote-url", MOCK_URL])
        assert result.exit_code == 0
        assert "mfst_123" in result.output

    def test_json_output(self) -> None:
        runner = CliRunner()
        stack, _ = _patch_client(show={"manifest_id": "mfst_123", "agent_id": "agent_alice"})
        with stack:
            result = runner.invoke(
                manifest, ["show", "mfst_123", "--remote-url", MOCK_URL, "--json"]
            )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["data"]["manifest_id"] == "mfst_123"

    def test_client_args(self) -> None:
        runner = CliRunner()
        stack, mocks = _patch_client(show={"manifest_id": "mfst_123"})
        with stack:
            runner.invoke(manifest, ["show", "mfst_123", "--remote-url", MOCK_URL])
        mocks["show"].assert_called_once_with("mfst_123")

    def test_missing_url_exits_nonzero(self) -> None:
        runner = CliRunner()
        result = runner.invoke(manifest, ["show", "mfst_123"])
        assert result.exit_code != 0


class TestManifestEvaluate:
    def test_happy_path(self) -> None:
        runner = CliRunner()
        stack, _ = _patch_client(evaluate={"permission": "allow", "manifest_id": "mfst_123"})
        with stack:
            result = runner.invoke(
                manifest,
                ["evaluate", "mfst_123", "--tool-name", "read", "--remote-url", MOCK_URL],
            )
        assert result.exit_code == 0
        assert "Allowed" in result.output

    def test_json_output(self) -> None:
        runner = CliRunner()
        stack, _ = _patch_client(evaluate={"permission": "allow"})
        with stack:
            result = runner.invoke(
                manifest,
                [
                    "evaluate",
                    "mfst_123",
                    "--tool-name",
                    "read",
                    "--remote-url",
                    MOCK_URL,
                    "--json",
                ],
            )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["data"]["permission"] == "allow"

    def test_client_args(self) -> None:
        runner = CliRunner()
        stack, mocks = _patch_client(evaluate={"permission": "deny"})
        with stack:
            runner.invoke(
                manifest,
                ["evaluate", "mfst_123", "--tool-name", "read", "--remote-url", MOCK_URL],
            )
        mocks["evaluate"].assert_called_once_with("mfst_123", tool_name="read")

    def test_denied(self) -> None:
        runner = CliRunner()
        stack, _ = _patch_client(evaluate={"permission": "deny", "manifest_id": "mfst_123"})
        with stack:
            result = runner.invoke(
                manifest,
                ["evaluate", "mfst_123", "--tool-name", "write", "--remote-url", MOCK_URL],
            )
        assert result.exit_code == 0
        assert "Denied" in result.output

    def test_missing_url_exits_nonzero(self) -> None:
        runner = CliRunner()
        result = runner.invoke(manifest, ["evaluate", "mfst_123", "--tool-name", "read"])
        assert result.exit_code != 0


class TestManifestRevoke:
    def test_happy_path(self) -> None:
        runner = CliRunner()
        stack, _ = _patch_client(revoke={"status": "revoked"})
        with stack:
            result = runner.invoke(manifest, ["revoke", "mfst_123", "--remote-url", MOCK_URL])
        assert result.exit_code == 0
        assert "revoked" in result.output

    def test_json_output(self) -> None:
        runner = CliRunner()
        stack, _ = _patch_client(revoke={"status": "revoked"})
        with stack:
            result = runner.invoke(
                manifest, ["revoke", "mfst_123", "--remote-url", MOCK_URL, "--json"]
            )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["data"]["status"] == "revoked"

    def test_client_args(self) -> None:
        runner = CliRunner()
        stack, mocks = _patch_client(revoke={"status": "revoked"})
        with stack:
            runner.invoke(manifest, ["revoke", "mfst_123", "--remote-url", MOCK_URL])
        mocks["revoke"].assert_called_once_with("mfst_123")

    def test_missing_url_exits_nonzero(self) -> None:
        runner = CliRunner()
        result = runner.invoke(manifest, ["revoke", "mfst_123"])
        assert result.exit_code != 0
