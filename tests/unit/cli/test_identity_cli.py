"""Tests for nexus identity CLI commands."""

from __future__ import annotations

import json
import os
from contextlib import ExitStack
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from nexus.cli.clients.base import BaseServiceClient
from nexus.cli.clients.identity import IdentityClient
from nexus.cli.commands.identity import identity

MOCK_URL = "http://localhost:2026"


def _patch_client(**method_returns: object) -> tuple[ExitStack, dict[str, MagicMock]]:
    """Patch BaseServiceClient so IdentityClient works without httpx."""
    stack = ExitStack()
    stack.enter_context(patch.dict(os.environ, {"NEXUS_NO_AUTO_JSON": "1"}))
    stack.enter_context(patch.object(BaseServiceClient, "__init__", lambda self, *a, **kw: None))
    stack.enter_context(patch.object(BaseServiceClient, "__enter__", lambda self: self))
    stack.enter_context(patch.object(BaseServiceClient, "__exit__", lambda self, *a: None))
    mocks: dict[str, MagicMock] = {}
    for method_name, return_value in method_returns.items():
        m = stack.enter_context(
            patch.object(IdentityClient, method_name, return_value=return_value)
        )
        mocks[method_name] = m
    return stack, mocks


class TestIdentityShow:
    def test_happy_path(self) -> None:
        runner = CliRunner()
        stack, _ = _patch_client(
            show={"did": "did:nexus:abc", "public_key": "pk123", "algorithm": "Ed25519"}
        )
        with stack:
            result = runner.invoke(identity, ["show", "alice", "--remote-url", MOCK_URL])
        assert result.exit_code == 0
        assert "did:nexus:abc" in result.output

    def test_json_output(self) -> None:
        runner = CliRunner()
        stack, _ = _patch_client(show={"did": "did:nexus:abc", "public_key": "pk123"})
        with stack:
            result = runner.invoke(identity, ["show", "alice", "--remote-url", MOCK_URL, "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["data"]["did"] == "did:nexus:abc"

    def test_missing_url_exits_nonzero(self) -> None:
        runner = CliRunner()
        result = runner.invoke(identity, ["show", "alice"])
        assert result.exit_code != 0

    def test_api_error_404(self) -> None:
        from nexus.cli.clients.base import NexusAPIError

        runner = CliRunner()
        stack = ExitStack()
        stack.enter_context(patch.dict(os.environ, {"NEXUS_NO_AUTO_JSON": "1"}))
        stack.enter_context(
            patch.object(BaseServiceClient, "__init__", lambda self, *a, **kw: None)
        )
        stack.enter_context(patch.object(BaseServiceClient, "__enter__", lambda self: self))
        stack.enter_context(patch.object(BaseServiceClient, "__exit__", lambda self, *a: None))
        stack.enter_context(
            patch.object(
                IdentityClient,
                "show",
                side_effect=NexusAPIError(404, "Agent not found"),
            )
        )
        with stack:
            result = runner.invoke(identity, ["show", "unknown", "--remote-url", MOCK_URL])
        assert result.exit_code != 0

    def test_shows_capabilities(self) -> None:
        runner = CliRunner()
        stack, _ = _patch_client(
            show={
                "did": "did:nexus:abc",
                "public_key": "pk123",
                "algorithm": "Ed25519",
                "capabilities": ["read", "write"],
            }
        )
        with stack:
            result = runner.invoke(identity, ["show", "alice", "--remote-url", MOCK_URL])
        assert result.exit_code == 0
        assert "read" in result.output

    def test_client_called_with_agent_id(self) -> None:
        runner = CliRunner()
        stack, mocks = _patch_client(show={"did": "did:nexus:abc"})
        with stack:
            runner.invoke(identity, ["show", "agent_bob", "--remote-url", MOCK_URL])
        mocks["show"].assert_called_once_with("agent_bob")


class TestIdentityVerify:
    def test_valid(self) -> None:
        runner = CliRunner()
        stack, _ = _patch_client(verify={"valid": True})
        with stack:
            result = runner.invoke(identity, ["verify", "alice", "--remote-url", MOCK_URL])
        assert result.exit_code == 0
        assert "Valid" in result.output

    def test_invalid_shows_reason(self) -> None:
        runner = CliRunner()
        stack, _ = _patch_client(verify={"valid": False, "reason": "Expired credential"})
        with stack:
            result = runner.invoke(identity, ["verify", "alice", "--remote-url", MOCK_URL])
        assert result.exit_code == 0
        assert "Invalid" in result.output
        assert "Expired credential" in result.output

    def test_json_output(self) -> None:
        runner = CliRunner()
        stack, _ = _patch_client(verify={"valid": True})
        with stack:
            result = runner.invoke(
                identity, ["verify", "alice", "--remote-url", MOCK_URL, "--json"]
            )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["data"]["valid"] is True


class TestIdentityCredentials:
    def test_happy_path(self) -> None:
        runner = CliRunner()
        stack, _ = _patch_client(
            credentials_list={
                "credentials": [
                    {
                        "credential_id": "cred_1",
                        "capabilities": ["read"],
                        "expires_at": "2025-12-31T00:00:00",
                        "status": "active",
                    }
                ]
            }
        )
        with stack:
            result = runner.invoke(identity, ["credentials", "alice", "--remote-url", MOCK_URL])
        assert result.exit_code == 0

    def test_empty_credentials(self) -> None:
        runner = CliRunner()
        stack, _ = _patch_client(credentials_list={"credentials": []})
        with stack:
            result = runner.invoke(identity, ["credentials", "alice", "--remote-url", MOCK_URL])
        assert result.exit_code == 0
        assert "No active credentials" in result.output

    def test_json_output(self) -> None:
        runner = CliRunner()
        stack, _ = _patch_client(
            credentials_list={
                "credentials": [
                    {
                        "credential_id": "c1",
                        "capabilities": ["read"],
                        "status": "active",
                    }
                ]
            }
        )
        with stack:
            result = runner.invoke(
                identity, ["credentials", "alice", "--remote-url", MOCK_URL, "--json"]
            )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert len(data["data"]["credentials"]) == 1


class TestIdentityPassport:
    def test_happy_path(self) -> None:
        runner = CliRunner()
        stack, _ = _patch_client(
            show={"did": "did:nexus:abc", "public_key": "pk123"},
            credentials_list={"credentials": [{"credential_id": "c1", "capabilities": ["read"]}]},
        )
        with stack:
            result = runner.invoke(identity, ["passport", "alice", "--remote-url", MOCK_URL])
        assert result.exit_code == 0
        assert "did:nexus:abc" in result.output

    def test_json_output_combines_identity_and_creds(self) -> None:
        runner = CliRunner()
        stack, _ = _patch_client(
            show={"did": "did:nexus:abc", "public_key": "pk123"},
            credentials_list={"credentials": [{"credential_id": "c1", "capabilities": ["read"]}]},
        )
        with stack:
            result = runner.invoke(
                identity, ["passport", "alice", "--remote-url", MOCK_URL, "--json"]
            )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["data"]["did"] == "did:nexus:abc"
        assert len(data["data"]["credentials"]) == 1

    def test_calls_both_show_and_credentials(self) -> None:
        runner = CliRunner()
        stack, mocks = _patch_client(
            show={"did": "did:nexus:abc"},
            credentials_list={"credentials": []},
        )
        with stack:
            runner.invoke(identity, ["passport", "alice", "--remote-url", MOCK_URL])
        mocks["show"].assert_called_once_with("alice")
        mocks["credentials_list"].assert_called_once_with("alice")
