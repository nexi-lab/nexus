"""Click profile commands against real ReBAC grants and revocations."""

import json
from types import SimpleNamespace

import pytest
from click.testing import CliRunner
from sqlalchemy import create_engine

from nexus.bricks.mcp.profiles import load_profiles
from nexus.bricks.rebac.consistency.metastore_namespace_store import MetastoreNamespaceStore
from nexus.bricks.rebac.consistency.metastore_version_store import MetastoreVersionStore
from nexus.bricks.rebac.manager import EnhancedReBACManager
from nexus.cli.commands import mcp as mcp_commands
from nexus.storage.models import Base
from scripts.surface_coverage.paths import REPO_ROOT
from tests.testkit.metadata import InMemoryNexusFS


def test_assign_inspect_and_revoke_profile_grants(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    rebac = EnhancedReBACManager(
        engine=engine,
        version_store=MetastoreVersionStore(InMemoryNexusFS()),
        namespace_store=MetastoreNamespaceStore(InMemoryNexusFS()),
    )

    async def get_filesystem(*_args):
        return SimpleNamespace(service=lambda name: rebac if name == "rebac_manager" else None)

    monkeypatch.setattr(mcp_commands, "get_filesystem", get_filesystem)
    runner = CliRunner()

    def command(*args):
        result = runner.invoke(mcp_commands.mcp, ["profile", *args, "--format", "json"])
        assert result.exit_code == 0, result.output
        return json.loads(result.output)

    try:
        expected = sorted(
            load_profiles(REPO_ROOT / "src/nexus/config/tool_profiles.yaml")
            .get_profile("minimal")
            .tools
        )
        before = command("inspect", "agent", "demo-agent")
        assert before["tools"] == []
        assigned = command("assign", "agent", "demo-agent", "minimal")
        assert assigned["subject"] == ["agent", "demo-agent"]
        assert assigned["tools"] == expected
        assert len(assigned["tuple_ids"]) == len(expected)
        inspected = command("inspect", "agent", "demo-agent")
        assert inspected["tools"] == expected
        assert "minimal" in inspected["matching_profiles"]
        assert command("inspect", "agent", "another-agent")["tools"] == []
        for tuple_id in assigned["tuple_ids"]:
            assert rebac.rebac_delete(tuple_id)
        assert command("inspect", "agent", "demo-agent")["tools"] == []
        invalid = runner.invoke(
            mcp_commands.mcp, ["profile", "assign", "agent", "demo-agent", "unknown"]
        )
        assert invalid.exit_code != 0
        assert "Unknown MCP tool profile" in invalid.output
    finally:
        rebac.close()
        engine.dispose()
