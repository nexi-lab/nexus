"""CLI coverage for agent, workspace, and snapshot lifecycle RPC wrappers."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from typing import Any

from click.testing import CliRunner

from nexus.cli.commands.agent import agent
from nexus.cli.commands.snapshots import snapshot
from nexus.cli.commands.workspace import workspace_group


class _FakeFS:
    def __init__(self, services: dict[str, Any]) -> None:
        self._services = services

    def service(self, name: str) -> Any:
        return self._services[name]


def _patch_open_filesystem(monkeypatch, module: Any, services: dict[str, Any]) -> None:
    @asynccontextmanager
    async def _open_filesystem(*_args: Any, **_kwargs: Any):
        yield _FakeFS(services)

    monkeypatch.setattr(module, "open_filesystem", _open_filesystem)


class TestAgentLifecycleCli:
    def test_update_calls_update_agent(self, monkeypatch) -> None:
        import nexus.cli.commands.agent as agent_module

        calls: dict[str, Any] = {}

        class AgentRPC:
            async def update_agent(self, **kwargs: Any) -> dict[str, Any]:
                calls["update"] = kwargs
                return {
                    "agent_id": kwargs["agent_id"],
                    "name": kwargs["name"],
                    "description": kwargs["description"],
                }

        _patch_open_filesystem(monkeypatch, agent_module, {"agent_rpc": AgentRPC()})

        result = CliRunner().invoke(
            agent,
            [
                "update",
                "alice",
                "--name",
                "Alice Bot",
                "--description",
                "Updated",
                "--metadata",
                "tier=gold",
            ],
        )

        assert result.exit_code == 0, result.output
        assert calls["update"] == {
            "agent_id": "alice",
            "name": "Alice Bot",
            "description": "Updated",
            "metadata": {"tier": "gold"},
        }
        assert "Updated agent: alice" in result.output

    def test_transition_calls_agent_transition(self, monkeypatch) -> None:
        import nexus.cli.commands.agent as agent_module

        calls: dict[str, Any] = {}

        class AgentRPC:
            async def agent_transition(self, **kwargs: Any) -> dict[str, Any]:
                calls["transition"] = kwargs
                return {"agent_id": kwargs["agent_id"], "state": kwargs["target_state"]}

        _patch_open_filesystem(monkeypatch, agent_module, {"agent_rpc": AgentRPC()})

        result = CliRunner().invoke(
            agent,
            ["transition", "alice", "CONNECTED", "--expected-generation", "7"],
        )

        assert result.exit_code == 0, result.output
        assert calls["transition"] == {
            "agent_id": "alice",
            "target_state": "CONNECTED",
            "expected_generation": 7,
        }
        assert "Transitioned agent: alice" in result.output

    def test_heartbeat_calls_agent_heartbeat(self, monkeypatch) -> None:
        import nexus.cli.commands.agent as agent_module

        calls: dict[str, Any] = {}

        class AgentRPC:
            def agent_heartbeat(self, agent_id: str) -> dict[str, bool]:
                calls["heartbeat"] = agent_id
                return {"ok": True}

        _patch_open_filesystem(monkeypatch, agent_module, {"agent_rpc": AgentRPC()})

        result = CliRunner().invoke(agent, ["heartbeat", "alice"])

        assert result.exit_code == 0, result.output
        assert calls["heartbeat"] == "alice"
        assert "Recorded heartbeat: alice" in result.output


class TestWorkspaceLifecycleCli:
    def test_update_calls_update_workspace(self, monkeypatch) -> None:
        import nexus.cli.commands.workspace as workspace_module

        calls: dict[str, Any] = {}

        class WorkspaceRPC:
            def update_workspace(self, **kwargs: Any) -> dict[str, Any]:
                calls["update"] = kwargs
                return {
                    "path": kwargs["path"],
                    "name": kwargs["name"],
                    "description": kwargs["description"],
                    "metadata": kwargs["metadata"],
                }

        _patch_open_filesystem(monkeypatch, workspace_module, {"workspace_rpc": WorkspaceRPC()})

        result = CliRunner().invoke(
            workspace_group,
            [
                "update",
                "/workspace/project",
                "--name",
                "Project",
                "--description",
                "Main workspace",
                "--metadata",
                "owner=alice",
            ],
        )

        assert result.exit_code == 0, result.output
        assert calls["update"] == {
            "path": "/workspace/project",
            "name": "Project",
            "description": "Main workspace",
            "metadata": {"owner": "alice"},
        }
        assert "Updated workspace: /workspace/project" in result.output

    def test_config_load_calls_load_workspace_config(self, monkeypatch, tmp_path) -> None:
        import nexus.cli.commands.workspace as workspace_module

        calls: dict[str, Any] = {}

        class WorkspaceRPC:
            def load_workspace_config(self, **kwargs: Any) -> dict[str, int]:
                calls["load"] = kwargs
                return {"workspaces_registered": 1, "workspaces_skipped": 0}

        _patch_open_filesystem(monkeypatch, workspace_module, {"workspace_rpc": WorkspaceRPC()})
        config_path = tmp_path / "workspaces.json"
        config_path.write_text(
            json.dumps({"workspaces": [{"path": "/workspace/project", "name": "Project"}]}),
            encoding="utf-8",
        )

        result = CliRunner().invoke(workspace_group, ["config", "load", str(config_path)])

        assert result.exit_code == 0, result.output
        assert calls["load"] == {"workspaces": [{"path": "/workspace/project", "name": "Project"}]}
        assert "Loaded workspace config" in result.output


class TestSnapshotLifecycleCli:
    def test_info_calls_snapshot_get(self, monkeypatch) -> None:
        import nexus.cli.commands.snapshots as snapshots_module

        calls: list[tuple[str, dict[str, Any]]] = []

        def fake_rpc_call(_url: str | None, _key: str | None, method: str, **kwargs: Any):
            calls.append((method, kwargs))
            return {"transaction_id": kwargs["transaction_id"], "status": "active"}

        monkeypatch.setattr(snapshots_module, "rpc_call", fake_rpc_call)

        result = CliRunner(env={"NEXUS_NO_AUTO_JSON": "1"}).invoke(snapshot, ["info", "txn-1"])

        assert result.exit_code == 0, result.output
        assert calls == [("snapshot_get", {"transaction_id": "txn-1"})]
        assert "txn-1" in result.output

    def test_commit_calls_snapshot_commit(self, monkeypatch) -> None:
        import nexus.cli.commands.snapshots as snapshots_module

        calls: list[tuple[str, dict[str, Any]]] = []

        def fake_rpc_call(_url: str | None, _key: str | None, method: str, **kwargs: Any):
            calls.append((method, kwargs))
            return {"transaction_id": kwargs["transaction_id"], "status": "committed"}

        monkeypatch.setattr(snapshots_module, "rpc_call", fake_rpc_call)

        result = CliRunner(env={"NEXUS_NO_AUTO_JSON": "1"}).invoke(snapshot, ["commit", "txn-1"])

        assert result.exit_code == 0, result.output
        assert calls == [("snapshot_commit", {"transaction_id": "txn-1"})]
        assert "Snapshot committed" in result.output

    def test_entries_calls_snapshot_list_entries(self, monkeypatch) -> None:
        import nexus.cli.commands.snapshots as snapshots_module

        calls: list[tuple[str, dict[str, Any]]] = []

        def fake_rpc_call(_url: str | None, _key: str | None, method: str, **kwargs: Any):
            calls.append((method, kwargs))
            return {"entries": [{"path": "/workspace/a.txt", "operation": "write"}], "count": 1}

        monkeypatch.setattr(snapshots_module, "rpc_call", fake_rpc_call)

        result = CliRunner(env={"NEXUS_NO_AUTO_JSON": "1"}).invoke(snapshot, ["entries", "txn-1"])

        assert result.exit_code == 0, result.output
        assert calls == [("snapshot_list_entries", {"transaction_id": "txn-1"})]
        assert "/workspace/a.txt" in result.output
