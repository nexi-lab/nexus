"""Coverage contract checks for issue #4136.

The full-profile MCP/mount/OAuth story is complete only when the shared
surface model points users to real usage examples, correctness coverage,
and performance classification for the externally visible surfaces.
"""

from __future__ import annotations

from pathlib import Path

from scripts.surface_coverage.schema import load_yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_COVERAGE_YAML = _REPO_ROOT / "docs/surface-coverage/api-rpc-surface-coverage.yaml"
_USER_GUIDE = _REPO_ROOT / "docs/guides/user-guide.md"
_REAL_E2E_TEST = "tests/e2e/server/test_issue_4136_api_surface_e2e.py"

_ISSUE_4136_ROWS = {
    "add.mount",
    "auth.oauth",
    "connectors.api_v2",
    "connectors.auth_init",
    "connectors.auth_status",
    "connectors.available",
    "connectors.mount",
    "connectors.mounts",
    "connectors.unmount",
    "delete.saved_mount",
    "list.connectors",
    "list.mounts",
    "load.mount",
    "mcp.connect",
    "mcp.list_mounts",
    "mcp.list_tools",
    "mcp.mount",
    "mcp.mounts",
    "mcp.sync",
    "mcp.unmount",
    "mount.connectors",
    "mount.mounts",
    "oauth.exchange_code",
    "oauth.get_auth_url",
    "oauth.list_credentials",
    "oauth.list_providers",
    "oauth.revoke_credential",
    "oauth.test_credential",
    "reauth.mount",
    "remove.mount",
    "save.mount",
    "update.mount",
}


def _coverage_ops():
    coverage = load_yaml(_COVERAGE_YAML)
    return {op.id: op for op in coverage.operations}


def _assert_test_links_exist(link_field: str) -> None:
    for part in link_field.split(";"):
        target = part.strip()
        if not target or target.startswith("N/A") or target.startswith("setup/"):
            continue
        path_part = target.split("::", 1)[0].strip()
        assert (_REPO_ROOT / path_part).exists(), f"missing linked test path: {path_part}"


def test_issue_4136_rows_have_full_story_contract() -> None:
    ops = _coverage_ops()
    missing = sorted(_ISSUE_4136_ROWS - set(ops))
    assert not missing, f"missing #4136 operation rows: {missing}"

    for op_id in sorted(_ISSUE_4136_ROWS):
        op = ops[op_id]
        assert op.profiles["full"] == "supported", op_id
        assert op.summary.strip(), op_id
        assert op.usage_example and "CLI:" in op.usage_example and "RPC:" in op.usage_example, op_id
        assert op.correctness_test, op_id
        _assert_test_links_exist(op.correctness_test)
        assert _REAL_E2E_TEST in op.correctness_test, op_id
        assert op.perf_class is not None, op_id
        assert op.perf_link and op.perf_link.strip(), op_id
        assert op.owning_issue in {4128, 4136}, op_id


def test_user_guide_documents_mount_to_tool_story() -> None:
    guide = _USER_GUIDE.read_text(encoding="utf-8")

    required_phrases = [
        "Mount External Data Source, List Tools, And Use Them",
        "nexus connectors list",
        "nexus mounts add /sources/hn hn",
        "mcp_list_tools",
        "oauth_get_auth_url",
        "SSRF",
        "Correctness assertion:",
        "Performance classification:",
    ]
    for phrase in required_phrases:
        assert phrase in guide
