"""E2E tests for agent-first CLI features (--dry-run, --if-not-exists, export-tools).

These tests exercise real CLI commands through Click's CliRunner.

--dry-run tests do NOT need a real backend (they return before connecting).
--if-not-exists tests require a running Nexus server and are skipped by default.
"""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_runner() -> CliRunner:
    return CliRunner()


def _get_cli():
    """Import and return the real nexus CLI root group."""
    from nexus.cli.main import main

    return main


# ---------------------------------------------------------------------------
# export-tools — no filesystem needed
# ---------------------------------------------------------------------------


class TestExportToolsE2E:
    """End-to-end tests for `nexus mcp export-tools`."""

    def test_outputs_valid_json(self) -> None:
        runner = _make_runner()
        result = runner.invoke(_get_cli(), ["mcp", "export-tools"])
        assert result.exit_code == 0, f"output: {result.output}"
        tools = json.loads(result.output)
        assert isinstance(tools, list)
        assert len(tools) > 10

    def test_every_tool_has_required_fields(self) -> None:
        runner = _make_runner()
        result = runner.invoke(_get_cli(), ["mcp", "export-tools"])
        assert result.exit_code == 0
        tools = json.loads(result.output)
        for tool in tools:
            assert "name" in tool, f"Missing name: {tool}"
            assert "description" in tool, f"Missing description: {tool}"
            assert "inputSchema" in tool, f"Missing inputSchema: {tool}"
            schema = tool["inputSchema"]
            assert schema["type"] == "object"
            assert "properties" in schema

    def test_no_infrastructure_params_leaked(self) -> None:
        runner = _make_runner()
        result = runner.invoke(_get_cli(), ["mcp", "export-tools"])
        assert result.exit_code == 0
        tools = json.loads(result.output)
        infra = {
            "remote_url",
            "remote_api_key",
            "subject",
            "zone_id",
            "is_admin",
            "is_system",
            "admin_capabilities",
            "json_output",
            "quiet",
            "verbosity",
            "fields",
        }
        for tool in tools:
            props = set(tool["inputSchema"]["properties"].keys())
            leaked = props & infra
            assert not leaked, f"{tool['name']} leaks: {leaked}"

    def test_known_commands_present(self) -> None:
        runner = _make_runner()
        result = runner.invoke(_get_cli(), ["mcp", "export-tools"])
        assert result.exit_code == 0
        tools = json.loads(result.output)
        names = {t["name"] for t in tools}
        for expected in [
            "nexus_ls",
            "nexus_cat",
            "nexus_write",
            "nexus_mkdir",
            "nexus_rm",
            "nexus_agent_register",
            "nexus_mcp_serve",
        ]:
            assert expected in names, f"Missing tool: {expected}"

    def test_nested_agent_commands(self) -> None:
        runner = _make_runner()
        result = runner.invoke(_get_cli(), ["mcp", "export-tools"])
        assert result.exit_code == 0
        tools = json.loads(result.output)
        names = {t["name"] for t in tools}
        for expected in [
            "nexus_agent_register",
            "nexus_agent_list",
            "nexus_agent_info",
            "nexus_agent_delete",
        ]:
            assert expected in names, f"Missing agent tool: {expected}"

    def test_dry_run_param_preserved(self) -> None:
        runner = _make_runner()
        result = runner.invoke(_get_cli(), ["mcp", "export-tools"])
        assert result.exit_code == 0
        tools = json.loads(result.output)
        write_tool = next(t for t in tools if t["name"] == "nexus_write")
        assert "dry_run" in write_tool["inputSchema"]["properties"]

    def test_output_is_json_round_trippable(self) -> None:
        runner = _make_runner()
        result = runner.invoke(_get_cli(), ["mcp", "export-tools"])
        assert result.exit_code == 0
        tools = json.loads(result.output)
        serialized = json.dumps(tools)
        reparsed = json.loads(serialized)
        assert reparsed == tools


# ---------------------------------------------------------------------------
# --dry-run E2E — no filesystem needed (early return before connect)
# ---------------------------------------------------------------------------


class TestDryRunE2E:
    """E2E: --dry-run previews mutations without connecting to backend."""

    def test_write_dry_run(self) -> None:
        runner = _make_runner()
        result = runner.invoke(
            _get_cli(),
            ["write", "/test.txt", "hello", "--dry-run"],
        )
        assert result.exit_code == 0, f"output: {result.output}"
        assert "DRY RUN" in result.output

    def test_mkdir_dry_run(self) -> None:
        runner = _make_runner()
        result = runner.invoke(
            _get_cli(),
            ["mkdir", "/mydir", "--dry-run"],
        )
        assert result.exit_code == 0, f"output: {result.output}"
        assert "DRY RUN" in result.output

    def test_rm_dry_run(self) -> None:
        runner = _make_runner()
        result = runner.invoke(
            _get_cli(),
            ["rm", "/to-delete.txt", "--dry-run"],
        )
        assert result.exit_code == 0, f"output: {result.output}"
        assert "DRY RUN" in result.output

    def test_cp_dry_run(self) -> None:
        runner = _make_runner()
        result = runner.invoke(
            _get_cli(),
            ["cp", "/src.txt", "/dst.txt", "--dry-run"],
        )
        assert result.exit_code == 0
        assert "DRY RUN" in result.output

    def test_move_dry_run(self) -> None:
        runner = _make_runner()
        result = runner.invoke(
            _get_cli(),
            ["move", "/a.txt", "/b.txt", "--dry-run"],
        )
        assert result.exit_code == 0
        assert "DRY RUN" in result.output

    def test_rmdir_dry_run(self) -> None:
        runner = _make_runner()
        result = runner.invoke(
            _get_cli(),
            ["rmdir", "/somedir", "--dry-run"],
        )
        assert result.exit_code == 0
        assert "DRY RUN" in result.output


# ---------------------------------------------------------------------------
# --if-not-exists E2E — requires running Nexus server
# ---------------------------------------------------------------------------


@pytest.mark.skip(reason="Requires running Nexus server (remote-only CLI)")
class TestIfNotExistsE2E:
    """E2E: --if-not-exists provides idempotent creates.

    These tests need a live Nexus server. Run with:
        NEXUS_URL=http://localhost:2026 pytest -k TestIfNotExistsE2E -s
    """

    def test_mkdir_if_not_exists_idempotent(self) -> None:
        runner = _make_runner()
        # First create
        result = runner.invoke(_get_cli(), ["mkdir", "/e2e-test-dir"])
        assert result.exit_code == 0

        # Second create with --if-not-exists should succeed
        r2 = runner.invoke(
            _get_cli(),
            ["mkdir", "/e2e-test-dir", "--if-not-exists"],
        )
        assert r2.exit_code == 0, f"output: {r2.output}"
        assert "exists" in r2.output.lower() or "✓" in r2.output

    def test_mkdir_if_not_exists_creates_when_missing(self) -> None:
        runner = _make_runner()
        result = runner.invoke(
            _get_cli(),
            ["mkdir", "/e2e-new-dir", "--if-not-exists"],
        )
        assert result.exit_code == 0
        assert "✓" in result.output
