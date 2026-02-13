"""Tests for context manifest template variable resolution (Issue #1341).

TDD Phase 2 — RED: Write tests before implementation.
"""

from __future__ import annotations

import pytest

from nexus.services.context_manifest.template import ALLOWED_VARIABLES, resolve_template

# ===========================================================================
# Basic substitution
# ===========================================================================


class TestResolveTemplateBasic:
    """Tests for basic template variable substitution."""

    def test_single_variable(self) -> None:
        result = resolve_template(
            "search for {{task.description}}",
            {"task.description": "auth bugs"},
        )
        assert result == "search for auth bugs"

    def test_multiple_variables(self) -> None:
        result = resolve_template(
            "{{agent.id}} in {{agent.zone_id}}",
            {"agent.id": "agent-42", "agent.zone_id": "zone-1"},
        )
        assert result == "agent-42 in zone-1"

    def test_same_variable_twice(self) -> None:
        result = resolve_template(
            "{{task.id}}-{{task.id}}",
            {"task.id": "t1"},
        )
        assert result == "t1-t1"

    def test_no_variables_returns_unchanged(self) -> None:
        result = resolve_template("plain string", {})
        assert result == "plain string"

    def test_empty_template_returns_empty(self) -> None:
        result = resolve_template("", {})
        assert result == ""

    def test_all_allowed_variables(self) -> None:
        """Every variable in ALLOWED_VARIABLES should be substitutable."""
        variables = {var: f"val_{var}" for var in ALLOWED_VARIABLES}
        for var in ALLOWED_VARIABLES:
            result = resolve_template(f"{{{{{var}}}}}", variables)
            assert result == f"val_{var}"


# ===========================================================================
# Error handling
# ===========================================================================


class TestResolveTemplateErrors:
    """Tests for error handling in template resolution."""

    def test_undefined_variable_raises(self) -> None:
        """A variable referenced in template but not in variables dict raises."""
        with pytest.raises(ValueError, match="task.description"):
            resolve_template(
                "{{task.description}}",
                {},  # variable not provided
            )

    def test_non_whitelisted_variable_raises(self) -> None:
        """A variable not in ALLOWED_VARIABLES raises even if provided."""
        with pytest.raises(ValueError, match="task.secret"):
            resolve_template(
                "{{task.secret}}",
                {"task.secret": "should_not_work"},
            )

    def test_injection_attempt_dunder_raises(self) -> None:
        """Template injection via __class__ is blocked by whitelist."""
        with pytest.raises(ValueError, match="task.__class__"):
            resolve_template(
                "{{task.__class__}}",
                {"task.__class__": "injected"},
            )

    def test_injection_attempt_import_raises(self) -> None:
        """Template injection via __import__ is blocked."""
        with pytest.raises(ValueError, match="__import__"):
            resolve_template(
                "{{__import__}}",
                {"__import__": "os"},
            )


# ===========================================================================
# Unicode and special characters
# ===========================================================================


class TestResolveTemplateUnicode:
    """Tests for unicode handling in template resolution."""

    def test_unicode_value(self) -> None:
        result = resolve_template(
            "query: {{task.description}}",
            {"task.description": "日本語テスト"},
        )
        assert result == "query: 日本語テスト"

    def test_unicode_in_surrounding_text(self) -> None:
        result = resolve_template(
            "検索: {{task.description}} を探す",
            {"task.description": "auth"},
        )
        assert result == "検索: auth を探す"

    def test_emoji_value(self) -> None:
        result = resolve_template(
            "status: {{task.description}}",
            {"task.description": "done"},
        )
        assert "done" in result


# ===========================================================================
# Safety: no double-substitution
# ===========================================================================


class TestResolveTemplateNoDoubleSubstitution:
    """Ensure substituted values are not re-processed for variables."""

    def test_value_containing_template_syntax_is_literal(self) -> None:
        """If a variable's value contains {{...}}, it should NOT be expanded."""
        result = resolve_template(
            "{{task.description}}",
            {"task.description": "{{agent.id}}"},
        )
        # The literal string "{{agent.id}}" should appear, not any agent ID
        assert result == "{{agent.id}}"


# ===========================================================================
# ALLOWED_VARIABLES constant
# ===========================================================================


class TestAllowedVariables:
    """Tests for the ALLOWED_VARIABLES frozenset."""

    def test_is_frozenset(self) -> None:
        assert isinstance(ALLOWED_VARIABLES, frozenset)

    def test_contains_expected_variables(self) -> None:
        expected = {
            "task.description",
            "task.id",
            "workspace.root",
            "workspace.id",
            "agent.id",
            "agent.zone_id",
            "agent.owner_id",
        }
        assert expected.issubset(ALLOWED_VARIABLES)

    def test_immutable(self) -> None:
        with pytest.raises(AttributeError):
            ALLOWED_VARIABLES.add("hacked")  # type: ignore[attr-defined]
