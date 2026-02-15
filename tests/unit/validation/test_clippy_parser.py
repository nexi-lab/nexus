"""Tests for Cargo Clippy output parser."""

from __future__ import annotations

from nexus.validation.parsers.clippy import CargoClippyValidator


class TestCargoClippyValidator:
    def test_parse_errors(self, clippy_errors_jsonl):
        parser = CargoClippyValidator()
        errors = parser.parse_output(clippy_errors_jsonl, "", 1)
        assert len(errors) == 2
        # Warning
        assert errors[0].file == "src/lib.rs"
        assert errors[0].line == 5
        assert errors[0].column == 9
        assert errors[0].severity == "warning"
        assert errors[0].rule == "unused_variables"
        assert errors[0].message == "unused variable: `x`"
        # Error
        assert errors[1].file == "src/main.rs"
        assert errors[1].line == 10
        assert errors[1].severity == "error"
        assert errors[1].rule == "E0308"
        assert errors[1].fix_available is True  # has suggested_replacement

    def test_parse_clean(self, clippy_clean_jsonl):
        parser = CargoClippyValidator()
        errors = parser.parse_output(clippy_clean_jsonl, "", 0)
        assert errors == []

    def test_parse_empty_output(self):
        parser = CargoClippyValidator()
        errors = parser.parse_output("", "", 0)
        assert errors == []

    def test_parse_invalid_json_lines(self):
        parser = CargoClippyValidator()
        errors = parser.parse_output("not json\nalso not json\n", "", 1)
        assert errors == []

    def test_skips_non_compiler_messages(self):
        parser = CargoClippyValidator()
        stdout = '{"reason":"compiler-artifact","package_id":"test"}\n'
        errors = parser.parse_output(stdout, "", 0)
        assert errors == []

    def test_build_command(self):
        parser = CargoClippyValidator()
        cmd = parser.build_command("/workspace")
        assert "cargo clippy" in cmd
        assert "/workspace" in cmd
