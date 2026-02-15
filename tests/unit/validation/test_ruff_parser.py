"""Tests for ruff output parser."""

from __future__ import annotations

from nexus.validation.parsers.ruff import RuffValidator


class TestRuffValidator:
    def test_parse_errors(self, ruff_errors_json):
        parser = RuffValidator()
        errors = parser.parse_output(ruff_errors_json, "", 1)
        assert len(errors) == 2
        assert errors[0].file == "app.py"
        assert errors[0].rule == "F401"
        assert errors[0].fix_available is True
        assert errors[0].line == 1
        assert errors[0].column == 8
        assert errors[1].file == "utils.py"
        assert errors[1].rule == "E501"
        assert errors[1].fix_available is False

    def test_parse_clean(self, ruff_clean_json):
        parser = RuffValidator()
        errors = parser.parse_output(ruff_clean_json, "", 0)
        assert errors == []

    def test_parse_empty_output(self):
        parser = RuffValidator()
        errors = parser.parse_output("", "", 0)
        assert errors == []

    def test_parse_invalid_json(self):
        parser = RuffValidator()
        errors = parser.parse_output("not json at all", "", 1)
        assert errors == []

    def test_parse_non_array_json(self):
        parser = RuffValidator()
        errors = parser.parse_output('{"key": "value"}', "", 1)
        assert errors == []

    def test_build_command(self):
        parser = RuffValidator()
        cmd = parser.build_command("/workspace")
        assert "ruff check" in cmd
        assert "/workspace" in cmd

    def test_missing_location_fields(self):
        """Handles entries without location gracefully."""
        parser = RuffValidator()
        json_str = '[{"filename": "x.py", "message": "bad", "code": "E1"}]'
        errors = parser.parse_output(json_str, "", 1)
        assert len(errors) == 1
        assert errors[0].line == 0
        assert errors[0].column == 0
