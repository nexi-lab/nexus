"""Tests for mypy output parser."""

from __future__ import annotations

from nexus.validation.parsers.mypy import MypyValidator


class TestMypyValidator:
    def test_parse_errors(self, mypy_errors_txt):
        parser = MypyValidator()
        errors = parser.parse_output(mypy_errors_txt, "", 1)
        assert len(errors) == 4
        # First error
        assert errors[0].file == "app.py"
        assert errors[0].line == 10
        assert errors[0].column == 5
        assert errors[0].severity == "error"
        assert errors[0].rule == "assignment"
        # Second error
        assert errors[1].file == "app.py"
        assert errors[1].line == 15
        assert errors[1].severity == "error"
        assert errors[1].rule == "name-defined"
        # Warning
        assert errors[2].severity == "warning"
        assert errors[2].rule == "unused-ignore"
        # Note mapped to info
        assert errors[3].severity == "info"

    def test_parse_clean(self, mypy_clean_txt):
        parser = MypyValidator()
        errors = parser.parse_output(mypy_clean_txt, "", 0)
        assert errors == []

    def test_parse_empty_output(self):
        parser = MypyValidator()
        errors = parser.parse_output("", "", 0)
        assert errors == []

    def test_parse_non_matching_lines(self):
        parser = MypyValidator()
        errors = parser.parse_output("Some random output\nAnother line\n", "", 0)
        assert errors == []

    def test_build_command(self):
        parser = MypyValidator()
        cmd = parser.build_command("/workspace")
        assert "mypy" in cmd
        assert "/workspace" in cmd
