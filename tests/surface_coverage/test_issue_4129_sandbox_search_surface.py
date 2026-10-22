"""Coverage contract tests for the sandbox search story (#4129)."""

from __future__ import annotations

from scripts.surface_coverage.paths import COVERAGE_YAML, REPO_ROOT
from scripts.surface_coverage.schema import ProfileStatus, load_yaml

_USER_GUIDE = REPO_ROOT / "docs/guides/user-guide.md"

OWNING_ISSUE = 4129

SANDBOX_SEARCH_ROWS = {
    "search.cli",
    "search.glob",
    "search.grep",
    "semantic.search",
    "semantic.search_stats",
}


def test_sandbox_search_rows_have_owner_tests_and_perf_classification() -> None:
    coverage = load_yaml(COVERAGE_YAML)
    by_id = {op.id: op for op in coverage.operations}

    for op_id in sorted(SANDBOX_SEARCH_ROWS):
        assert op_id in by_id
        op = by_id[op_id]
        assert op.owning_issue == OWNING_ISSUE, op_id
        assert op.profiles["sandbox"] == ProfileStatus.SUPPORTED, op_id
        assert op.summary, op_id
        assert op.usage_example, op_id
        assert op.correctness_test, op_id
        assert op.perf_class is not None, op_id
        assert op.perf_link, op_id
        assert op.gap_issue is None, op_id


def test_sandbox_search_mcp_tools_link_to_search_rows() -> None:
    coverage = load_yaml(COVERAGE_YAML)
    by_id = {op.id: op for op in coverage.operations}

    assert by_id["search.glob"].transports["mcp"].name == "nexus_glob"
    assert by_id["search.grep"].transports["mcp"].name == "nexus_grep"
    assert by_id["semantic.search"].transports["mcp"].name == "nexus_semantic_search"


def test_user_guide_describes_native_query_contract() -> None:
    text = _USER_GUIDE.read_text(encoding="utf-8")

    for needle in [
        "Search and discovery",
        'nexus search query "find the project plan" --mode hybrid --json',
        "nx.semantic_search(",
        "native caller credential",
        "Indexing and statistics require native administration rights",
        "scripts/test_search_local.py",
    ]:
        assert needle in text
