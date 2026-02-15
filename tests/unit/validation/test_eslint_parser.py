"""Tests for ESLint output parser."""

from __future__ import annotations

from nexus.validation.parsers.eslint import ESLintValidator


class TestESLintValidator:
    def test_parse_errors(self, eslint_errors_json):
        parser = ESLintValidator()
        errors = parser.parse_output(eslint_errors_json, "", 1)
        assert len(errors) == 2
        # Error
        assert errors[0].file == "/workspace/src/index.js"
        assert errors[0].line == 3
        assert errors[0].column == 7
        assert errors[0].severity == "error"
        assert errors[0].rule == "no-unused-vars"
        assert errors[0].fix_available is False
        # Warning with fix
        assert errors[1].severity == "warning"
        assert errors[1].rule == "semi"
        assert errors[1].fix_available is True

    def test_parse_clean(self, eslint_clean_json):
        parser = ESLintValidator()
        errors = parser.parse_output(eslint_clean_json, "", 0)
        assert errors == []

    def test_parse_empty_output(self):
        parser = ESLintValidator()
        errors = parser.parse_output("", "", 0)
        assert errors == []

    def test_parse_invalid_json(self):
        parser = ESLintValidator()
        errors = parser.parse_output("{broken", "", 1)
        assert errors == []

    def test_build_command(self):
        parser = ESLintValidator()
        cmd = parser.build_command("/workspace")
        assert "eslint" in cmd
        assert "/workspace" in cmd
