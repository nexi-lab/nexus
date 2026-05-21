"""Coverage contract tests for the sandbox search story (#4129)."""

from __future__ import annotations

from pathlib import Path

from scripts.surface_coverage.schema import ProfileStatus, load_yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_COVERAGE_YAML = _REPO_ROOT / "docs/architecture/api-rpc-surface-coverage.yaml"
_USER_GUIDE = _REPO_ROOT / "docs/guides/user-guide.md"

OWNING_ISSUE = 4129

SANDBOX_SEARCH_ROWS = {
    "glob.batch",
    "initialize.semantic_search",
    "search.cli",
    "search.glob",
    "search.grep",
    "semantic.search",
    "semantic.search_index",
    "semantic.search_stats",
}


def test_sandbox_search_rows_have_owner_tests_and_perf_classification() -> None:
    coverage = load_yaml(_COVERAGE_YAML)
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
    coverage = load_yaml(_COVERAGE_YAML)
    by_id = {op.id: op for op in coverage.operations}

    assert by_id["search.glob"].transports["mcp"].name == "nexus_glob"
    assert by_id["search.grep"].transports["mcp"].name == "nexus_grep"
    assert by_id["semantic.search"].transports["mcp"].name == "nexus_semantic_search"


def test_user_guide_contains_sandbox_search_degraded_and_source_story() -> None:
    text = _USER_GUIDE.read_text(encoding="utf-8")

    for needle in [
        "Sandbox search workflow",
        "semantic_degraded",
        'nexus search query "auth flow" --mode hybrid --json',
        'nx.service("search").semantic_search(',
        "nexus_semantic_search",
        "keyword_score",
        "vector_score",
        "docs/benchmarks/2026-04-18-sandbox-vs-gbrain.md",
        "Missing-surface gate verdict",
    ]:
        assert needle in text
