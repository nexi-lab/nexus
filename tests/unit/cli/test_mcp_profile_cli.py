import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from click.testing import CliRunner

from nexus.cli.commands import mcp as mcp_commands
from nexus.cli.commands.mcp import mcp

PROFILE_YAML = """
profiles:
  minimal:
    description: Read-only access
    tools:
      - nexus_read_file
      - nexus_list_files
  coding:
    extends: minimal
    description: Coding access
    tools:
      - nexus_write_file
      - nexus_grep
default_profile: minimal
"""


class FakeReBAC:
    def __init__(self) -> None:
        self.writes: list[dict[str, Any]] = []
        self.objects_by_subject: dict[tuple[tuple[str, str], str | None], set[tuple[str, str]]] = {}
        self.fail_writes = False

    def rebac_write(
        self,
        *,
        subject: tuple[str, str],
        relation: str,
        object: tuple[str, str],
        zone_id: str | None = None,
    ) -> SimpleNamespace:
        if self.fail_writes:
            raise PermissionError("write denied")
        self.writes.append(
            {
                "subject": subject,
                "relation": relation,
                "object": object,
                "zone_id": zone_id,
            }
        )
        self.objects_by_subject.setdefault((subject, zone_id), set()).add(object)
        return SimpleNamespace(tuple_id=f"tuple-{len(self.writes)}", revision=len(self.writes))

    def rebac_list_objects(
        self,
        *,
        subject: tuple[str, str],
        permission: str,
        object_type: str = "file",
        zone_id: str | None = None,
        limit: int = 1000,
    ) -> list[tuple[str, str]]:
        assert permission == "read"
        objects = self.objects_by_subject.get((subject, zone_id), set())
        return [obj for obj in sorted(objects) if obj[0] == object_type][:limit]


class FakeNx:
    def __init__(self, rebac: Any) -> None:
        self.rebac = rebac
        self.closed = False

    def service(self, name: str) -> Any:
        if name == "rebac_manager":
            return self.rebac
        return None

    def close(self) -> None:
        self.closed = True


class FakeRemoteReBACProxy:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)

        def _call(**kwargs: Any) -> dict[str, Any]:
            self.calls.append({"method": name, "kwargs": kwargs})
            return {"tuple_id": f"remote-{len(self.calls)}"}

        return _call


def _write_profiles(tmp_path: Path) -> Path:
    path = tmp_path / "tool_profiles.yaml"
    path.write_text(PROFILE_YAML)
    return path


def test_mcp_profile_list_outputs_configured_profiles(tmp_path: Path) -> None:
    config_path = _write_profiles(tmp_path)

    result = CliRunner().invoke(
        mcp,
        ["profile", "list", "--config", str(config_path), "--format", "json"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["default_profile"] == "minimal"
    assert [profile["name"] for profile in payload["profiles"]] == ["coding", "minimal"]


def test_mcp_profile_show_outputs_resolved_inherited_profile(tmp_path: Path) -> None:
    config_path = _write_profiles(tmp_path)

    result = CliRunner().invoke(
        mcp,
        ["profile", "show", "coding", "--config", str(config_path), "--format", "json"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload == {
        "name": "coding",
        "description": "Coding access",
        "extends": "minimal",
        "tools": [
            "nexus_grep",
            "nexus_list_files",
            "nexus_read_file",
            "nexus_write_file",
        ],
    }


def test_mcp_profile_assign_materializes_inherited_tool_grants(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    config_path = _write_profiles(tmp_path)
    rebac = FakeReBAC()
    nx = FakeNx(rebac)

    async def fake_get_filesystem(*_args: Any, **_kwargs: Any) -> FakeNx:
        return nx

    monkeypatch.setattr(mcp_commands, "get_filesystem", fake_get_filesystem)

    result = CliRunner().invoke(
        mcp,
        [
            "profile",
            "assign",
            "agent",
            "alice",
            "coding",
            "--zone-id",
            "org_acme",
            "--config",
            str(config_path),
            "--format",
            "json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["profile"] == "coding"
    assert payload["subject"] == ["agent", "alice"]
    assert payload["zone_id"] == "org_acme"
    assert payload["tools"] == [
        "nexus_grep",
        "nexus_list_files",
        "nexus_read_file",
        "nexus_write_file",
    ]
    assert payload["tuple_ids"] == ["tuple-1", "tuple-2", "tuple-3", "tuple-4"]
    assert [write["object"] for write in rebac.writes] == [
        ("file", "/tools/nexus_grep"),
        ("file", "/tools/nexus_list_files"),
        ("file", "/tools/nexus_read_file"),
        ("file", "/tools/nexus_write_file"),
    ]
    assert nx.closed


def test_mcp_profile_inspect_reports_effective_tool_grants(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    config_path = _write_profiles(tmp_path)
    rebac = FakeReBAC()
    rebac.objects_by_subject[(("agent", "alice"), "org_acme")] = {
        ("file", "/tools/nexus_list_files"),
        ("file", "/tools/nexus_read_file"),
        ("file", "/tools/nexus_write_file"),
        ("file", "/workspace/not-a-tool"),
    }
    nx = FakeNx(rebac)

    async def fake_get_filesystem(*_args: Any, **_kwargs: Any) -> FakeNx:
        return nx

    monkeypatch.setattr(mcp_commands, "get_filesystem", fake_get_filesystem)

    result = CliRunner().invoke(
        mcp,
        [
            "profile",
            "inspect",
            "agent",
            "alice",
            "--zone-id",
            "org_acme",
            "--config",
            str(config_path),
            "--format",
            "json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["subject"] == ["agent", "alice"]
    assert payload["zone_id"] == "org_acme"
    assert payload["tools"] == ["nexus_list_files", "nexus_read_file", "nexus_write_file"]
    assert payload["matching_profiles"] == ["minimal"]
    assert nx.closed


def test_mcp_profile_assign_unknown_profile_fails(tmp_path: Path) -> None:
    config_path = _write_profiles(tmp_path)

    result = CliRunner().invoke(
        mcp,
        ["profile", "assign", "agent", "alice", "missing", "--config", str(config_path)],
    )

    assert result.exit_code != 0
    assert "Unknown MCP tool profile 'missing'" in result.output


def test_mcp_profile_assign_rebac_write_failure_is_nonzero(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    config_path = _write_profiles(tmp_path)
    rebac = FakeReBAC()
    rebac.fail_writes = True
    nx = FakeNx(rebac)

    async def fake_get_filesystem(*_args: Any, **_kwargs: Any) -> FakeNx:
        return nx

    monkeypatch.setattr(mcp_commands, "get_filesystem", fake_get_filesystem)

    result = CliRunner().invoke(
        mcp,
        ["profile", "assign", "agent", "alice", "minimal", "--config", str(config_path)],
    )

    assert result.exit_code != 0
    assert "write denied" in result.output
    assert nx.closed


def test_mcp_profile_assign_uses_rpc_create_for_proxy_surface(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    config_path = _write_profiles(tmp_path)
    rebac = FakeRemoteReBACProxy()
    nx = FakeNx(rebac)

    async def fake_get_filesystem(*_args: Any, **_kwargs: Any) -> FakeNx:
        return nx

    monkeypatch.setattr(mcp_commands, "get_filesystem", fake_get_filesystem)

    result = CliRunner().invoke(
        mcp,
        [
            "profile",
            "assign",
            "agent",
            "alice",
            "minimal",
            "--config",
            str(config_path),
            "--format",
            "json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert {call["method"] for call in rebac.calls} == {"rebac_create_sync"}
