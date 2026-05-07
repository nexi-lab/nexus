from __future__ import annotations

import json

from click.testing import CliRunner

from nexus.cli.commands import _hub_remote
from nexus.cli.commands.hub import hub


def test_normalize_remote_url_appends_mcp_path():
    assert (
        _hub_remote.normalize_mcp_url("https://nexus.example.com")
        == "https://nexus.example.com/mcp"
    )
    assert (
        _hub_remote.normalize_mcp_url("https://nexus.example.com/mcp")
        == "https://nexus.example.com/mcp"
    )
    assert (
        _hub_remote.normalize_mcp_url("https://nexus.example.com/")
        == "https://nexus.example.com/mcp"
    )


def test_remote_list_requires_admin_token(monkeypatch):
    monkeypatch.delenv("NEXUS_HUB_ADMIN_TOKEN", raising=False)

    result = CliRunner().invoke(hub, ["token", "list", "--remote", "https://hub.example"])

    assert result.exit_code != 0
    assert "--admin-token or NEXUS_HUB_ADMIN_TOKEN is required with --remote" in result.output


def test_remote_list_uses_env_token_and_renders_json(monkeypatch):
    calls = []

    def fake_call(remote, token, tool_name, arguments):
        calls.append((remote, token, tool_name, arguments))
        return {
            "tokens": [
                {
                    "key_id": "nk_123",
                    "name": "admin",
                    "zone": None,
                    "zones": [],
                    "admin": True,
                    "created": "2026-05-04T12:00:00",
                    "last_used": "-",
                    "revoked": False,
                    "revoked_at": "-",
                }
            ]
        }

    monkeypatch.setenv("NEXUS_HUB_ADMIN_TOKEN", "sk-env")
    monkeypatch.setattr("nexus.cli.commands.hub.call_hub_admin_tool", fake_call)

    result = CliRunner().invoke(
        hub,
        ["token", "list", "--remote", "https://hub.example", "--json"],
    )

    assert result.exit_code == 0
    assert calls == [
        ("https://hub.example", "sk-env", "nexus_hub_token_list", {"show_revoked": False})
    ]
    assert json.loads(result.output)["tokens"][0]["key_id"] == "nk_123"


def test_remote_create_calls_mcp_tool_and_prints_one_time_token(monkeypatch):
    calls = []

    def fake_call(remote, token, tool_name, arguments):
        calls.append((remote, token, tool_name, arguments))
        return {
            "key_id": "nk_new",
            "token": "sk-new",
            "name": "ci",
            "admin": False,
            "zones": [{"zone_id": "eng", "permission": "rw"}],
        }

    monkeypatch.setattr("nexus.cli.commands.hub.call_hub_admin_tool", fake_call)

    result = CliRunner().invoke(
        hub,
        [
            "token",
            "create",
            "--name",
            "ci",
            "--zones",
            "eng:rw",
            "--remote",
            "https://hub.example/mcp",
            "--admin-token",
            "sk-admin",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls == [
        (
            "https://hub.example/mcp",
            "sk-admin",
            "nexus_hub_token_create",
            {
                "name": "ci",
                "zones": "eng:rw",
                "zones_glob": None,
                "admin": False,
                "expires": None,
                "user_id": None,
            },
        )
    ]
    assert "key_id: nk_new" in result.output
    assert "token:  sk-new" in result.output


def test_remote_revoke_calls_mcp_tool(monkeypatch):
    calls = []

    def fake_call(remote, token, tool_name, arguments):
        calls.append((remote, token, tool_name, arguments))
        return {
            "key_id": "nk_old",
            "name": "old",
            "message": "revoked old (nk_old). Effective within 60s (auth cache TTL).",
        }

    monkeypatch.setattr("nexus.cli.commands.hub.call_hub_admin_tool", fake_call)

    result = CliRunner().invoke(
        hub,
        [
            "token",
            "revoke",
            "old",
            "--remote",
            "https://hub.example",
            "--admin-token",
            "sk-admin",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls == [
        ("https://hub.example", "sk-admin", "nexus_hub_token_revoke", {"identifier": "old"})
    ]
    assert "revoked old (nk_old)" in result.output


def test_remote_status_calls_mcp_tool_and_renders_json(monkeypatch):
    calls = []

    def fake_call(remote, token, tool_name, arguments):
        calls.append((remote, token, tool_name, arguments))
        return {
            "endpoint": "https://hub.example/mcp",
            "profile": "full",
            "postgres": "ok",
            "redis": "n/a",
            "tokens": {"active": 1, "revoked": 0},
            "connections": None,
            "qps_5m": None,
        }

    monkeypatch.setattr("nexus.cli.commands.hub.call_hub_admin_tool", fake_call)

    result = CliRunner().invoke(
        hub,
        ["status", "--remote", "https://hub.example", "--admin-token", "sk-admin", "--json"],
    )

    assert result.exit_code == 0, result.output
    assert calls == [("https://hub.example", "sk-admin", "nexus_hub_status", {})]
    assert json.loads(result.output)["postgres"] == "ok"
