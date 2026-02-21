"""Unit tests for template variable resolution (Issue #2130, #9A).

Covers:
- Whitelist enforcement (unknown variable → ValueError)
- Missing value for allowed variable → ValueError
- Empty template string → passthrough
- Multiple variables in one template
- Double-substitution prevention
- Partial template syntax (no closing braces)
- All allowed variables resolve correctly
"""

import pytest

from nexus.bricks.context_manifest.template import ALLOWED_VARIABLES, resolve_template


class TestResolveTemplateSuccess:
    """Happy path: valid templates resolve correctly."""

    def test_no_variables(self) -> None:
        assert resolve_template("hello world", {}) == "hello world"

    def test_empty_string(self) -> None:
        assert resolve_template("", {}) == ""

    def test_single_variable(self) -> None:
        result = resolve_template("task is {{task.id}}", {"task.id": "t-123"})
        assert result == "task is t-123"

    def test_multiple_variables(self) -> None:
        result = resolve_template(
            "{{agent.id}} in {{agent.zone_id}}",
            {"agent.id": "a1", "agent.zone_id": "z1"},
        )
        assert result == "a1 in z1"

    def test_same_variable_twice(self) -> None:
        result = resolve_template(
            "{{task.id}} and {{task.id}}",
            {"task.id": "t-1"},
        )
        assert result == "t-1 and t-1"

    def test_all_allowed_variables(self) -> None:
        variables = {var: f"val_{var}" for var in ALLOWED_VARIABLES}
        for var in ALLOWED_VARIABLES:
            result = resolve_template(f"{{{{{var}}}}}", variables)
            assert result == f"val_{var}"


class TestResolveTemplateErrors:
    """Error cases: invalid or missing variables raise ValueError."""

    def test_unknown_variable_raises(self) -> None:
        with pytest.raises(ValueError, match="not allowed"):
            resolve_template("{{unknown.var}}", {})

    def test_missing_value_for_allowed_variable_raises(self) -> None:
        with pytest.raises(ValueError, match="not provided"):
            resolve_template("{{task.id}}", {})

    def test_partially_unknown_raises(self) -> None:
        with pytest.raises(ValueError, match="not allowed"):
            resolve_template(
                "{{task.id}} {{evil.var}}",
                {"task.id": "t-1"},
            )


class TestDoubleSubstitutionPrevention:
    """Security: substituted values must NOT be re-scanned."""

    def test_value_containing_template_syntax_not_expanded(self) -> None:
        result = resolve_template(
            "{{task.id}}",
            {"task.id": "{{agent.id}}"},
        )
        assert result == "{{agent.id}}"

    def test_value_containing_braces_preserved(self) -> None:
        result = resolve_template(
            "result: {{task.description}}",
            {"task.description": "fix {{bug}} in code"},
        )
        assert result == "result: fix {{bug}} in code"


class TestEdgeCases:
    """Edge cases for template parsing."""

    def test_no_closing_braces_passthrough(self) -> None:
        result = resolve_template("{{task.id", {})
        assert result == "{{task.id"

    def test_empty_braces_passthrough(self) -> None:
        result = resolve_template("{{}}", {})
        assert result == "{{}}"

    def test_single_brace_passthrough(self) -> None:
        result = resolve_template("{task.id}", {})
        assert result == "{task.id}"

    def test_whitespace_in_variable_name(self) -> None:
        with pytest.raises(ValueError, match="not allowed"):
            resolve_template("{{ task.id }}", {})


class TestAllowedVariables:
    """Verify the whitelist is complete and frozen."""

    def test_allowed_variables_is_frozenset(self) -> None:
        assert isinstance(ALLOWED_VARIABLES, frozenset)

    def test_expected_variables_present(self) -> None:
        expected = {
            "task.description",
            "task.id",
            "workspace.root",
            "workspace.id",
            "agent.id",
            "agent.zone_id",
            "agent.owner_id",
        }
        assert expected == ALLOWED_VARIABLES
