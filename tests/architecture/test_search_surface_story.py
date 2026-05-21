"""Coverage contract tests for the full-profile search/parsing story (#4135)."""

from __future__ import annotations

from pathlib import Path

from scripts.surface_coverage.schema import ProfileStatus, load_yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_COVERAGE_YAML = _REPO_ROOT / "docs/architecture/api-rpc-surface-coverage.yaml"
_USER_GUIDE = _REPO_ROOT / "docs/guides/user-guide.md"

OWNING_ISSUE = 4135
PREEXISTING_SANDBOX_SEARCH_ISSUE = 4129

SUPPORTED_ROWS = {
    "filesystem.path_context",
    "initialize.semantic_search",
    "path_contexts.api_v2",
    "search.cli",
    "search.expand",
    "search.glob",
    "search.grep",
    "search.health",
    "search.index",
    "search.index_directory",
    "search.indexed_dirs",
    "search.indexing_mode",
    "search.locate",
    "search.purge_unscoped",
    "search.query",
    "search.query_batch",
    "search.refresh",
    "search.reindex",
    "search.stats",
    "semantic.search",
    "semantic.search_index",
    "semantic.search_stats",
}

MISSING_ROWS = {
    "parsers.list",
    "parsers.run_parse",
    "search.grep_section",
}

PREEXISTING_SANDBOX_SEARCH_ROWS = {
    "initialize.semantic_search",
    "search.cli",
    "search.glob",
    "search.grep",
    "semantic.search",
    "semantic.search_index",
    "semantic.search_stats",
}

OWNED_ROWS = (SUPPORTED_ROWS | MISSING_ROWS) - PREEXISTING_SANDBOX_SEARCH_ROWS


def test_search_story_rows_have_owner_tests_perf_and_gap_state() -> None:
    coverage = load_yaml(_COVERAGE_YAML)
    by_id = {op.id: op for op in coverage.operations}

    for op_id in sorted(SUPPORTED_ROWS | MISSING_ROWS):
        assert op_id in by_id
        op = by_id[op_id]
        if op_id in OWNED_ROWS:
            assert op.owning_issue == OWNING_ISSUE, op_id
        else:
            assert op.owning_issue == PREEXISTING_SANDBOX_SEARCH_ISSUE, op_id
        assert op.summary, op_id
        assert op.usage_example, op_id
        assert op.correctness_test, op_id
        assert op.perf_class is not None, op_id
        assert op.perf_link, op_id

        if op_id in MISSING_ROWS:
            assert op.transports == {}, op_id
            assert all(status == ProfileStatus.MISSING_NEEDED for status in op.profiles.values()), (
                op_id
            )
            assert op.gap_issue is not None, op_id
        else:
            assert op.profiles["full"] == ProfileStatus.SUPPORTED, op_id
            assert op.gap_issue is None, op_id


def test_search_story_mcp_tools_link_to_story_rows() -> None:
    coverage = load_yaml(_COVERAGE_YAML)
    by_id = {op.id: op for op in coverage.operations}

    assert by_id["search.glob"].transports["mcp"].name == "nexus_glob"
    assert by_id["search.grep"].transports["mcp"].name == "nexus_grep"
    assert by_id["semantic.search"].transports["mcp"].name == "nexus_semantic_search"


def test_user_guide_contains_search_story_inventory_and_quality_claims() -> None:
    text = _USER_GUIDE.read_text(encoding="utf-8")

    for needle in [
        "Search surface coverage matrix",
        "/api/v2/search/query",
        "/api/v2/search/grep",
        "nexus path-context",
        "RRF",
        "docs/benchmarks/2026-04-18-sandbox-vs-gbrain.md",
        "section-aware grep",
        "parser introspection",
    ]:
        assert needle in text
