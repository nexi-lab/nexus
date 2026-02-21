"""Unit tests for ManifestEvaluator (Issue #1754).

Pure function tests — no I/O, no mocks.
Tests first-match-wins semantics, glob matching, and case normalization.
"""

import pytest

from nexus.bricks.access_manifest.evaluator import ManifestEvaluator
from nexus.contracts.access_manifest_types import ManifestEntry, ToolPermission

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def evaluator() -> ManifestEvaluator:
    return ManifestEvaluator()


# ---------------------------------------------------------------------------
# Tests: Single evaluation
# ---------------------------------------------------------------------------


class TestEvaluate:
    def test_exact_match_allow(self) -> None:
        entries = (ManifestEntry(tool_pattern="nexus_read_file", permission=ToolPermission.ALLOW),)
        assert ManifestEvaluator.evaluate(entries, "nexus_read_file") == ToolPermission.ALLOW

    def test_exact_match_deny(self) -> None:
        entries = (ManifestEntry(tool_pattern="nexus_delete", permission=ToolPermission.DENY),)
        assert ManifestEvaluator.evaluate(entries, "nexus_delete") == ToolPermission.DENY

    def test_glob_wildcard(self) -> None:
        entries = (ManifestEntry(tool_pattern="nexus_*", permission=ToolPermission.ALLOW),)
        assert ManifestEvaluator.evaluate(entries, "nexus_read_file") == ToolPermission.ALLOW
        assert ManifestEvaluator.evaluate(entries, "nexus_search") == ToolPermission.ALLOW
        assert ManifestEvaluator.evaluate(entries, "other_tool") == ToolPermission.DENY

    def test_glob_star(self) -> None:
        entries = (ManifestEntry(tool_pattern="*", permission=ToolPermission.ALLOW),)
        assert ManifestEvaluator.evaluate(entries, "anything") == ToolPermission.ALLOW

    def test_glob_question_mark(self) -> None:
        entries = (ManifestEntry(tool_pattern="tool_?", permission=ToolPermission.ALLOW),)
        assert ManifestEvaluator.evaluate(entries, "tool_a") == ToolPermission.ALLOW
        assert ManifestEvaluator.evaluate(entries, "tool_ab") == ToolPermission.DENY

    def test_no_match_defaults_to_deny(self) -> None:
        entries = (ManifestEntry(tool_pattern="specific_tool", permission=ToolPermission.ALLOW),)
        assert ManifestEvaluator.evaluate(entries, "other_tool") == ToolPermission.DENY

    def test_empty_entries_defaults_to_deny(self) -> None:
        assert ManifestEvaluator.evaluate((), "any_tool") == ToolPermission.DENY

    def test_first_match_wins_allow_before_deny(self) -> None:
        entries = (
            ManifestEntry(tool_pattern="nexus_*", permission=ToolPermission.ALLOW),
            ManifestEntry(tool_pattern="*", permission=ToolPermission.DENY),
        )
        assert ManifestEvaluator.evaluate(entries, "nexus_read") == ToolPermission.ALLOW
        assert ManifestEvaluator.evaluate(entries, "other") == ToolPermission.DENY

    def test_first_match_wins_deny_before_allow(self) -> None:
        entries = (
            ManifestEntry(tool_pattern="nexus_delete", permission=ToolPermission.DENY),
            ManifestEntry(tool_pattern="nexus_*", permission=ToolPermission.ALLOW),
        )
        assert ManifestEvaluator.evaluate(entries, "nexus_delete") == ToolPermission.DENY
        assert ManifestEvaluator.evaluate(entries, "nexus_read") == ToolPermission.ALLOW

    def test_case_insensitive_tool_name(self) -> None:
        entries = (ManifestEntry(tool_pattern="nexus_read", permission=ToolPermission.ALLOW),)
        assert ManifestEvaluator.evaluate(entries, "NEXUS_READ") == ToolPermission.ALLOW
        assert ManifestEvaluator.evaluate(entries, "Nexus_Read") == ToolPermission.ALLOW

    def test_case_insensitive_pattern(self) -> None:
        entries = (ManifestEntry(tool_pattern="NEXUS_*", permission=ToolPermission.ALLOW),)
        assert ManifestEvaluator.evaluate(entries, "nexus_read") == ToolPermission.ALLOW

    def test_mixed_case_both(self) -> None:
        entries = (ManifestEntry(tool_pattern="Nexus_Read_*", permission=ToolPermission.ALLOW),)
        assert ManifestEvaluator.evaluate(entries, "nexus_read_file") == ToolPermission.ALLOW

    def test_rate_limit_field_preserved(self) -> None:
        entries = (
            ManifestEntry(
                tool_pattern="nexus_*",
                permission=ToolPermission.ALLOW,
                max_calls_per_minute=10,
            ),
        )
        # Evaluator doesn't enforce rate limits, just returns permission
        assert ManifestEvaluator.evaluate(entries, "nexus_read") == ToolPermission.ALLOW


# ---------------------------------------------------------------------------
# Tests: Batch filter
# ---------------------------------------------------------------------------


class TestFilterTools:
    def test_filter_mixed(self) -> None:
        entries = (
            ManifestEntry(tool_pattern="nexus_*", permission=ToolPermission.ALLOW),
            ManifestEntry(tool_pattern="*", permission=ToolPermission.DENY),
        )
        tools = frozenset({"nexus_read", "nexus_write", "other_tool", "admin_tool"})
        allowed = ManifestEvaluator.filter_tools(entries, tools)
        assert allowed == frozenset({"nexus_read", "nexus_write"})

    def test_filter_all_allowed(self) -> None:
        entries = (ManifestEntry(tool_pattern="*", permission=ToolPermission.ALLOW),)
        tools = frozenset({"a", "b", "c"})
        allowed = ManifestEvaluator.filter_tools(entries, tools)
        assert allowed == tools

    def test_filter_all_denied(self) -> None:
        entries = (ManifestEntry(tool_pattern="*", permission=ToolPermission.DENY),)
        tools = frozenset({"a", "b", "c"})
        allowed = ManifestEvaluator.filter_tools(entries, tools)
        assert allowed == frozenset()

    def test_filter_empty_tools(self) -> None:
        entries = (ManifestEntry(tool_pattern="*", permission=ToolPermission.ALLOW),)
        allowed = ManifestEvaluator.filter_tools(entries, frozenset())
        assert allowed == frozenset()

    def test_filter_empty_entries(self) -> None:
        tools = frozenset({"a", "b"})
        allowed = ManifestEvaluator.filter_tools((), tools)
        assert allowed == frozenset()


# ---------------------------------------------------------------------------
# Tests: Access manifest types immutability
# ---------------------------------------------------------------------------


class TestAccessManifestTypes:
    def test_manifest_entry_frozen(self) -> None:
        entry = ManifestEntry(tool_pattern="test", permission=ToolPermission.ALLOW)
        with pytest.raises(AttributeError):
            entry.tool_pattern = "changed"

    def test_tool_permission_values(self) -> None:
        assert ToolPermission.ALLOW == "allow"
        assert ToolPermission.DENY == "deny"
