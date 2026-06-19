from __future__ import annotations

from typing import Any

from click.testing import CliRunner

from nexus.cli.commands import admin as admin_mod
from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.server._rpc_param_overrides import AdminCreateKeyParams


def _stub_admin_rpc(calls: list[tuple[str, dict[str, Any] | None]]):
    def _get_admin_rpc(_url: str | None, _api_key: str | None):
        def _call(method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
            calls.append((method, params))
            params = params or {}
            return {
                "key_id": "key-1",
                "api_key": "sk-test_key",
                "user_id": params.get("user_id", "user"),
                "name": params.get("name", "key"),
                "subject_type": params.get("subject_type", "user"),
                "subject_id": params.get("subject_id") or params.get("user_id", "user"),
                "zone_id": params.get("zone_id"),
                "is_admin": False,
                "expires_at": None,
            }

        return _call

    return _get_admin_rpc


def test_create_key_sends_default_zone(monkeypatch) -> None:
    calls: list[tuple[str, dict[str, Any] | None]] = []
    monkeypatch.setattr(admin_mod, "get_admin_rpc", _stub_admin_rpc(calls))

    result = CliRunner().invoke(
        admin_mod.admin,
        ["create-key", "alice", "--name", "Alice laptop", "--json"],
    )

    assert result.exit_code == 0, result.output
    assert calls == [
        (
            "admin_create_key",
            {"user_id": "alice", "name": "Alice laptop", "zone_id": ROOT_ZONE_ID},
        )
    ]


def test_create_agent_key_sends_zone_override(monkeypatch) -> None:
    calls: list[tuple[str, dict[str, Any] | None]] = []
    monkeypatch.setattr(admin_mod, "get_admin_rpc", _stub_admin_rpc(calls))

    result = CliRunner().invoke(
        admin_mod.admin,
        [
            "create-agent-key",
            "alice",
            "alice_agent",
            "--name",
            "Agent key",
            "--zone-id",
            "default",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls == [
        (
            "admin_create_key",
            {
                "user_id": "alice",
                "name": "Agent key",
                "zone_id": "default",
                "subject_type": "agent",
                "subject_id": "alice_agent",
            },
        )
    ]


def test_admin_create_key_params_default_to_root_zone() -> None:
    assert AdminCreateKeyParams(name="legacy caller").zone_id == ROOT_ZONE_ID
