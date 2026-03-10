"""Tests for nexus delegation CLI commands."""

from __future__ import annotations

import json
import os
from contextlib import ExitStack
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from nexus.cli.clients.base import BaseServiceClient
from nexus.cli.clients.delegation import DelegationClient
from nexus.cli.commands.delegation import delegation

MOCK_URL = "http://localhost:2026"


def _patch_client(**method_returns: object) -> tuple[ExitStack, dict[str, MagicMock]]:
    """Patch BaseServiceClient so DelegationClient works without httpx."""
    stack = ExitStack()
    stack.enter_context(patch.dict(os.environ, {"NEXUS_NO_AUTO_JSON": "1"}))
    stack.enter_context(patch.object(BaseServiceClient, "__init__", lambda self, *a, **kw: None))
    stack.enter_context(patch.object(BaseServiceClient, "__enter__", lambda self: self))
    stack.enter_context(patch.object(BaseServiceClient, "__exit__", lambda self, *a: None))
    mocks: dict[str, MagicMock] = {}
    for method_name, return_value in method_returns.items():
        m = stack.enter_context(
            patch.object(DelegationClient, method_name, return_value=return_value)
        )
        mocks[method_name] = m
    return stack, mocks


class TestDelegationCreate:
    def test_happy_path(self) -> None:
        runner = CliRunner()
        stack, _ = _patch_client(
            create={
                "delegation_id": "dlg_1",
                "coordinator_agent_id": "coord",
                "worker_id": "worker",
                "delegation_mode": "COPY",
            }
        )
        with stack:
            result = runner.invoke(
                delegation, ["create", "coord", "worker", "--remote-url", MOCK_URL]
            )
        assert result.exit_code == 0
        assert "dlg_1" in result.output

    def test_default_mode_is_copy(self) -> None:
        runner = CliRunner()
        stack, mocks = _patch_client(create={"delegation_id": "dlg_2"})
        with stack:
            runner.invoke(delegation, ["create", "coord", "worker", "--remote-url", MOCK_URL])
        mocks["create"].assert_called_once_with(
            "coord",
            "worker",
            mode="COPY",
            scope_prefix=None,
            ttl_seconds=None,
            zone_id=None,
        )

    def test_with_scope_and_ttl(self) -> None:
        runner = CliRunner()
        stack, mocks = _patch_client(create={"delegation_id": "dlg_2"})
        with stack:
            result = runner.invoke(
                delegation,
                [
                    "create",
                    "coord",
                    "worker",
                    "--mode",
                    "CLEAN",
                    "--scope",
                    "/project/*",
                    "--ttl",
                    "3600",
                    "--remote-url",
                    MOCK_URL,
                ],
            )
        assert result.exit_code == 0
        mocks["create"].assert_called_once_with(
            "coord",
            "worker",
            mode="CLEAN",
            scope_prefix="/project/*",
            ttl_seconds=3600,
            zone_id=None,
        )

    def test_json_output(self) -> None:
        runner = CliRunner()
        stack, _ = _patch_client(create={"delegation_id": "dlg_3", "delegation_mode": "SHARED"})
        with stack:
            result = runner.invoke(
                delegation,
                ["create", "c", "w", "--remote-url", MOCK_URL, "--json"],
            )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["data"]["delegation_id"] == "dlg_3"

    def test_with_zone_id(self) -> None:
        runner = CliRunner()
        stack, mocks = _patch_client(create={"delegation_id": "dlg_4"})
        with stack:
            runner.invoke(
                delegation,
                [
                    "create",
                    "coord",
                    "worker",
                    "--zone-id",
                    "org_acme",
                    "--remote-url",
                    MOCK_URL,
                ],
            )
        mocks["create"].assert_called_once_with(
            "coord",
            "worker",
            mode="COPY",
            scope_prefix=None,
            ttl_seconds=None,
            zone_id="org_acme",
        )

    def test_missing_url_fails(self) -> None:
        runner = CliRunner()
        result = runner.invoke(delegation, ["create", "coord", "worker"])
        assert result.exit_code != 0


class TestDelegationList:
    def test_happy_path(self) -> None:
        runner = CliRunner()
        stack, _ = _patch_client(
            list={
                "delegations": [
                    {
                        "delegation_id": "dlg_1",
                        "coordinator_agent_id": "c",
                        "worker_id": "w",
                        "delegation_mode": "COPY",
                        "status": "ACTIVE",
                        "created_at": "2025-01-01T00:00:00",
                    }
                ]
            }
        )
        with stack:
            result = runner.invoke(delegation, ["list", "--remote-url", MOCK_URL])
        assert result.exit_code == 0

    def test_empty(self) -> None:
        runner = CliRunner()
        stack, _ = _patch_client(list={"delegations": []})
        with stack:
            result = runner.invoke(delegation, ["list", "--remote-url", MOCK_URL])
        assert result.exit_code == 0
        assert "No active delegations" in result.output

    def test_default_limit(self) -> None:
        runner = CliRunner()
        stack, mocks = _patch_client(list={"delegations": []})
        with stack:
            runner.invoke(delegation, ["list", "--remote-url", MOCK_URL])
        mocks["list"].assert_called_once_with(None, limit=50)

    def test_filter_by_coordinator(self) -> None:
        runner = CliRunner()
        stack, mocks = _patch_client(list={"delegations": []})
        with stack:
            runner.invoke(
                delegation,
                ["list", "--coordinator", "coord_1", "--remote-url", MOCK_URL],
            )
        mocks["list"].assert_called_once_with("coord_1", limit=50)

    def test_json_output(self) -> None:
        runner = CliRunner()
        stack, _ = _patch_client(
            list={"delegations": [{"delegation_id": "dlg_1", "delegation_mode": "COPY"}]}
        )
        with stack:
            result = runner.invoke(delegation, ["list", "--remote-url", MOCK_URL, "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert len(data["data"]["delegations"]) == 1


class TestDelegationRevoke:
    def test_happy_path(self) -> None:
        runner = CliRunner()
        stack, mocks = _patch_client(revoke={})
        with stack:
            result = runner.invoke(delegation, ["revoke", "dlg_1", "--remote-url", MOCK_URL])
        assert result.exit_code == 0
        assert "revoked" in result.output.lower()
        mocks["revoke"].assert_called_once_with("dlg_1")

    def test_json_output(self) -> None:
        runner = CliRunner()
        stack, _ = _patch_client(revoke={})
        with stack:
            result = runner.invoke(
                delegation, ["revoke", "dlg_1", "--remote-url", MOCK_URL, "--json"]
            )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["data"] is not None


class TestDelegationShow:
    def test_happy_path(self) -> None:
        runner = CliRunner()
        stack, _ = _patch_client(
            show={
                "chain": [
                    {
                        "coordinator_agent_id": "root",
                        "worker_id": "mid",
                        "delegation_mode": "COPY",
                    },
                    {
                        "coordinator_agent_id": "mid",
                        "worker_id": "leaf",
                        "delegation_mode": "CLEAN",
                    },
                ]
            }
        )
        with stack:
            result = runner.invoke(delegation, ["show", "dlg_1", "--remote-url", MOCK_URL])
        assert result.exit_code == 0
        assert "root" in result.output

    def test_json_output(self) -> None:
        runner = CliRunner()
        stack, _ = _patch_client(
            show={
                "chain": [
                    {
                        "coordinator_agent_id": "root",
                        "worker_id": "leaf",
                        "delegation_mode": "COPY",
                    },
                ]
            }
        )
        with stack:
            result = runner.invoke(
                delegation, ["show", "dlg_1", "--remote-url", MOCK_URL, "--json"]
            )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert len(data["data"]["chain"]) == 1

    def test_client_called_with_id(self) -> None:
        runner = CliRunner()
        stack, mocks = _patch_client(show={"chain": []})
        with stack:
            runner.invoke(delegation, ["show", "dlg_xyz", "--remote-url", MOCK_URL])
        mocks["show"].assert_called_once_with("dlg_xyz")
