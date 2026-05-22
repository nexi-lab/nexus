"""Coverage contract checks for issue #4131.

The MCP tool profile story is complete only when the shared surface model,
default profile matrix, enforcement tests, and user guide agree.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from scripts.surface_coverage.schema import ProfileStatus, load_yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_COVERAGE_YAML = _REPO_ROOT / "docs/architecture/api-rpc-surface-coverage.yaml"
_TOOL_PROFILES = _REPO_ROOT / "src/nexus/config/tool_profiles.yaml"
_USER_GUIDE = _REPO_ROOT / "docs/guides/user-guide.md"

_ISSUE_4131_OWNED_ROWS = {
    "delete.file",
    "edit.file",
    "execute.workflow",
    "file.info",
    "hub.admin",
    "list.files",
    "list.workflows",
    "read.file",
    "rename.file",
    "sandbox.create",
    "sandbox.list",
    "sandbox.stop",
    "write.file",
}

_RELATED_PROFILE_ROWS = {
    *_ISSUE_4131_OWNED_ROWS,
    "discovery.get_tool_details",
    "discovery.list_servers",
    "discovery.load_tools",
    "discovery.search_tools",
    "mcp.tool_profile_assign",
    "search.glob",
    "search.grep",
    "semantic.search",
}

_EXPECTED_PROFILE_TABLE_ROWS = {
    "minimal": ["nexus_read_file", "nexus_list_files", "nexus_file_info", "nexus_glob"],
    "coding": ["nexus_write_file", "nexus_edit_file", "nexus_delete_file", "nexus_grep"],
    "search": ["nexus_grep", "nexus_semantic_search"],
    "execution": [
        "nexus_python",
        "nexus_bash",
        "nexus_sandbox_create",
        "nexus_sandbox_list",
        "nexus_sandbox_stop",
    ],
    "full": [
        "nexus_discovery_search_tools",
        "nexus_list_workflows",
        "nexus_execute_workflow",
        "nexus_hub_admin",
    ],
}


def _operations_by_id():
    return {op.id: op for op in load_yaml(_COVERAGE_YAML).operations}


def _assert_linked_paths_exist(link_field: str) -> None:
    for part in link_field.split(";"):
        target = part.strip()
        if not target or target.startswith("N/A") or target.startswith("setup/"):
            continue
        path_part = target.split("::", 1)[0].strip()
        assert (_REPO_ROOT / path_part).exists(), f"missing linked path: {path_part}"


def test_issue_4131_owned_mcp_rows_have_story_contract() -> None:
    ops = _operations_by_id()
    missing = sorted(_ISSUE_4131_OWNED_ROWS - set(ops))
    assert not missing, f"missing #4131 operation rows: {missing}"

    for op_id in sorted(_ISSUE_4131_OWNED_ROWS):
        op = ops[op_id]
        assert op.owning_issue == 4131, op_id
        assert op.summary.strip(), op_id
        assert op.usage_example and "MCP:" in op.usage_example, op_id
        assert op.correctness_test, op_id
        _assert_linked_paths_exist(op.correctness_test)
        assert "test_issue_4131_mcp_tool_profile_story.py" in op.correctness_test, op_id
        assert op.perf_class is not None, op_id
        assert op.perf_link and op.perf_link.strip(), op_id
        assert op.gap_issue is None, op_id


def test_issue_4131_related_mcp_rows_have_profile_story_metadata() -> None:
    ops = _operations_by_id()
    for op_id in sorted(_RELATED_PROFILE_ROWS):
        op = ops[op_id]
        assert op.summary.strip(), op_id
        assert op.usage_example, op_id
        assert op.correctness_test, op_id
        assert op.perf_class is not None, op_id
        assert op.perf_link, op_id
        assert any(status == ProfileStatus.SUPPORTED for status in op.profiles.values()), op_id


def test_default_profile_matrix_is_documented_in_user_guide() -> None:
    guide = _USER_GUIDE.read_text(encoding="utf-8")
    raw = yaml.safe_load(_TOOL_PROFILES.read_text(encoding="utf-8"))

    assert "Shape An Agent's MCP Tool Profile" in guide
    assert "nexus mcp profile list" in guide
    assert "nexus mcp profile assign agent demo-agent coding" in guide
    assert "nexus mcp profile inspect agent demo-agent" in guide
    assert '"method": "tools/list"' in guide
    assert '"method": "tools/call"' in guide
    assert "Correctness assertion:" in guide
    assert "Performance classification:" in guide
    assert "file/search/ReBAC stories" in guide

    for profile_name, direct_tools in _EXPECTED_PROFILE_TABLE_ROWS.items():
        assert profile_name in raw["profiles"], profile_name
        assert f"| `{profile_name}` |" in guide
        for tool_name in direct_tools:
            assert tool_name in guide, f"{profile_name} missing {tool_name}"
