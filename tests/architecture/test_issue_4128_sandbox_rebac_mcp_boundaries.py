"""Issue #4128 sandbox ReBAC, hub-zone, and MCP boundary coverage gate."""

from __future__ import annotations

import tomllib
from pathlib import Path

from scripts.surface_coverage.schema import PerfClass, ProfileStatus, load_yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_COVERAGE_YAML = _REPO_ROOT / "docs/architecture/api-rpc-surface-coverage.yaml"
_PYPROJECT = _REPO_ROOT / "pyproject.toml"

_REBAC_SANDBOX_ROWS = {
    "rebac.create",
    "rebac.check",
    "rebac.list_tuples",
    "rebac.explain",
}

_MCP_SECURITY_ROWS = {
    "discovery.get_tool_details",
    "discovery.list_servers",
    "discovery.load_tools",
    "discovery.search_tools",
    "mcp.list_tools",
}


def _operations_by_id():
    return {op.id: op for op in load_yaml(_COVERAGE_YAML).operations}


def _assert_supported_sandbox_story_row(op_id: str) -> None:
    op = _operations_by_id()[op_id]
    assert op.profiles["sandbox"] == ProfileStatus.SUPPORTED, op_id
    assert op.summary, op_id
    assert op.usage_example, op_id
    assert op.correctness_test, op_id
    assert op.perf_class is not None, op_id
    assert op.perf_link, op_id


def test_issue_4128_rebac_core_rows_are_available_in_sandbox() -> None:
    for op_id in sorted(_REBAC_SANDBOX_ROWS):
        _assert_supported_sandbox_story_row(op_id)


def test_issue_4128_mcp_security_rows_are_linked_to_profile_grant_tests() -> None:
    ops = _operations_by_id()

    for op_id in sorted(_MCP_SECURITY_ROWS):
        _assert_supported_sandbox_story_row(op_id)
        op = ops[op_id]
        assert op.owning_issue == 4128, op_id
        assert "test_tool_namespace_middleware.py" in op.correctness_test, op_id
        assert op.perf_class in {PerfClass.HOT, PerfClass.HOT_PATH}, op_id
        assert "bench_permission_hotpath.py" in op.perf_link, op_id


def test_issue_4128_remote_zone_readonly_write_denial_is_linked() -> None:
    ops = _operations_by_id()

    write = ops["filesystem.write"]
    assert write.profiles["sandbox"] == ProfileStatus.SUPPORTED
    assert "test_remote_zone.py" in write.correctness_test
    assert "test_sandbox_federation_e2e.py" in write.correctness_test
    assert "bench_permission_hotpath.py" in write.perf_link

    whoami = ops["federation.client_whoami"]
    assert whoami.profiles["sandbox"] == ProfileStatus.SUPPORTED
    assert whoami.owning_issue in {4128, 4130}
    assert whoami.usage_example
    assert "test_sandbox_federation_e2e.py" in whoami.correctness_test
    assert whoami.perf_class == PerfClass.CONTROL
    assert whoami.perf_link


def test_issue_4128_tool_profile_assignment_cli_is_supported() -> None:
    op = _operations_by_id()["mcp.tool_profile_assign"]
    assert op.owning_issue == 4128
    assert op.gap_issue is None
    assert all(status == ProfileStatus.SUPPORTED for status in op.profiles.values())
    assert op.transports["cli"].name == "nexus mcp profile assign"
    assert "tests/unit/cli/test_mcp_profile_cli.py" in op.correctness_test
    assert op.perf_class == PerfClass.SETUP
    assert "bench_permission_hotpath.py" in op.perf_link


def test_issue_4128_default_mcp_tool_profiles_are_packaged() -> None:
    pyproject = tomllib.loads(_PYPROJECT.read_text())
    package_data = pyproject["tool"]["setuptools"]["package-data"]

    assert "config/*.yaml" in package_data["nexus"]
