"""Unit tests for validation output parsers (ruff, mypy, eslint, clippy).

Tests parse_output() for each validator with:
- Valid output
- Empty output
- Malformed JSON/text
- Partial/unexpected format
- Edge cases (missing fields, non-dict items)
"""

import json

import pytest

from nexus.parsers.validation.parsers.clippy import CargoClippyValidator
from nexus.parsers.validation.parsers.eslint import ESLintValidator
from nexus.parsers.validation.parsers.mypy import MypyValidator
from nexus.parsers.validation.parsers.ruff import RuffValidator

# ── Ruff Validator ────────────────────────────────────────────────


class TestRuffValidator:
    """Tests for RuffValidator.parse_output()."""

    def setup_method(self) -> None:
        self.validator = RuffValidator()

    def test_valid_json_output(self) -> None:
        stdout = json.dumps(
            [
                {
                    "filename": "src/main.py",
                    "location": {"row": 10, "column": 5},
                    "message": "Unused import",
                    "code": "F401",
                    "fix": {"edits": []},
                }
            ]
        )
        errors = self.validator.parse_output(stdout, "", 1)
        assert len(errors) == 1
        assert errors[0].file == "src/main.py"
        assert errors[0].line == 10
        assert errors[0].column == 5
        assert errors[0].severity == "error"
        assert errors[0].message == "Unused import"
        assert errors[0].rule == "F401"
        assert errors[0].fix_available is True

    def test_valid_no_fix(self) -> None:
        stdout = json.dumps(
            [
                {
                    "filename": "app.py",
                    "location": {"row": 1, "column": 1},
                    "message": "Missing docstring",
                    "code": "D100",
                    "fix": None,
                }
            ]
        )
        errors = self.validator.parse_output(stdout, "", 1)
        assert len(errors) == 1
        assert errors[0].fix_available is False

    def test_empty_output(self) -> None:
        assert self.validator.parse_output("", "", 0) == []

    def test_whitespace_only(self) -> None:
        assert self.validator.parse_output("   \n  ", "", 0) == []

    def test_malformed_json(self) -> None:
        assert self.validator.parse_output("{invalid json", "", 1) == []

    def test_json_not_a_list(self) -> None:
        assert self.validator.parse_output('{"key": "value"}', "", 1) == []

    def test_non_dict_items_skipped(self) -> None:
        stdout = json.dumps(["string_item", 42, None])
        assert self.validator.parse_output(stdout, "", 1) == []

    def test_missing_location(self) -> None:
        stdout = json.dumps([{"filename": "a.py", "message": "err", "code": "E1"}])
        errors = self.validator.parse_output(stdout, "", 1)
        assert len(errors) == 1
        assert errors[0].line == 0
        assert errors[0].column == 0

    def test_invalid_location_type(self) -> None:
        stdout = json.dumps([{"filename": "a.py", "location": "bad", "message": "err"}])
        errors = self.validator.parse_output(stdout, "", 1)
        assert len(errors) == 1
        assert errors[0].line == 0
        assert errors[0].column == 0

    def test_missing_fields_defaults(self) -> None:
        stdout = json.dumps([{}])
        errors = self.validator.parse_output(stdout, "", 1)
        assert len(errors) == 1
        assert errors[0].file == "<unknown>"
        assert errors[0].message == ""
        assert errors[0].rule is None

    def test_multiple_errors(self) -> None:
        items = [
            {
                "filename": f"f{i}.py",
                "location": {"row": i, "column": 1},
                "message": f"err{i}",
                "code": f"E{i}",
            }
            for i in range(5)
        ]
        errors = self.validator.parse_output(json.dumps(items), "", 1)
        assert len(errors) == 5

    def test_build_command(self) -> None:
        cmd = self.validator.build_command("/my/workspace")
        assert "ruff check" in cmd
        assert "/my/workspace" in cmd


# ── Mypy Validator ────────────────────────────────────────────────


class TestMypyValidator:
    """Tests for MypyValidator.parse_output()."""

    def setup_method(self) -> None:
        self.validator = MypyValidator()

    def test_valid_error(self) -> None:
        stdout = "src/main.py:10:5: error: Incompatible return value type [return-value]"
        errors = self.validator.parse_output(stdout, "", 1)
        assert len(errors) == 1
        assert errors[0].file == "src/main.py"
        assert errors[0].line == 10
        assert errors[0].column == 5
        assert errors[0].severity == "error"
        assert errors[0].message == "Incompatible return value type"
        assert errors[0].rule == "return-value"

    def test_warning_severity(self) -> None:
        stdout = "app.py:1:1: warning: Unused variable [misc]"
        errors = self.validator.parse_output(stdout, "", 1)
        assert len(errors) == 1
        assert errors[0].severity == "warning"

    def test_note_maps_to_info(self) -> None:
        stdout = "app.py:5:3: note: See documentation [note]"
        errors = self.validator.parse_output(stdout, "", 1)
        assert len(errors) == 1
        assert errors[0].severity == "info"

    def test_no_error_code(self) -> None:
        stdout = "app.py:1:1: error: Some error without brackets"
        errors = self.validator.parse_output(stdout, "", 1)
        assert len(errors) == 1
        assert errors[0].rule is None
        assert errors[0].message == "Some error without brackets"

    def test_empty_output(self) -> None:
        assert self.validator.parse_output("", "", 0) == []

    def test_non_matching_lines_skipped(self) -> None:
        stdout = "Success: no issues found in 10 source files\n"
        assert self.validator.parse_output(stdout, "", 0) == []

    def test_multiple_lines(self) -> None:
        stdout = (
            "a.py:1:1: error: Error one [e1]\n"
            "b.py:2:3: warning: Warning two [w2]\n"
            "c.py:3:5: note: Note three [n3]\n"
        )
        errors = self.validator.parse_output(stdout, "", 1)
        assert len(errors) == 3
        assert [e.severity for e in errors] == ["error", "warning", "info"]

    def test_mixed_valid_invalid_lines(self) -> None:
        stdout = "Some preamble\na.py:1:1: error: Real error [e1]\nAnother non-matching line\n"
        errors = self.validator.parse_output(stdout, "", 1)
        assert len(errors) == 1
        assert errors[0].file == "a.py"

    def test_build_command(self) -> None:
        cmd = self.validator.build_command("/my/workspace")
        assert "mypy" in cmd
        assert "/my/workspace" in cmd


# ── ESLint Validator ──────────────────────────────────────────────


class TestESLintValidator:
    """Tests for ESLintValidator.parse_output()."""

    def setup_method(self) -> None:
        self.validator = ESLintValidator()

    def test_valid_json_output(self) -> None:
        stdout = json.dumps(
            [
                {
                    "filePath": "/src/app.js",
                    "messages": [
                        {
                            "line": 5,
                            "column": 10,
                            "severity": 2,
                            "message": "Unexpected var",
                            "ruleId": "no-var",
                            "fix": {"range": [0, 3], "text": "let"},
                        }
                    ],
                }
            ]
        )
        errors = self.validator.parse_output(stdout, "", 1)
        assert len(errors) == 1
        assert errors[0].file == "/src/app.js"
        assert errors[0].line == 5
        assert errors[0].column == 10
        assert errors[0].severity == "error"
        assert errors[0].message == "Unexpected var"
        assert errors[0].rule == "no-var"
        assert errors[0].fix_available is True

    def test_warning_severity(self) -> None:
        stdout = json.dumps(
            [
                {
                    "filePath": "a.js",
                    "messages": [
                        {"line": 1, "column": 1, "severity": 1, "message": "warn", "ruleId": "r1"}
                    ],
                }
            ]
        )
        errors = self.validator.parse_output(stdout, "", 1)
        assert len(errors) == 1
        assert errors[0].severity == "warning"

    def test_unknown_severity_defaults_to_error(self) -> None:
        stdout = json.dumps(
            [
                {
                    "filePath": "a.js",
                    "messages": [{"line": 1, "column": 1, "severity": 99, "message": "unknown"}],
                }
            ]
        )
        errors = self.validator.parse_output(stdout, "", 1)
        assert errors[0].severity == "error"

    def test_empty_output(self) -> None:
        assert self.validator.parse_output("", "", 0) == []

    def test_malformed_json(self) -> None:
        assert self.validator.parse_output("not json", "", 1) == []

    def test_json_not_a_list(self) -> None:
        assert self.validator.parse_output('{"key": "val"}', "", 1) == []

    def test_non_dict_file_results_skipped(self) -> None:
        stdout = json.dumps(["string_item"])
        assert self.validator.parse_output(stdout, "", 1) == []

    def test_non_list_messages_skipped(self) -> None:
        stdout = json.dumps([{"filePath": "a.js", "messages": "not-a-list"}])
        assert self.validator.parse_output(stdout, "", 1) == []

    def test_non_dict_messages_skipped(self) -> None:
        stdout = json.dumps([{"filePath": "a.js", "messages": ["string_msg"]}])
        assert self.validator.parse_output(stdout, "", 1) == []

    def test_multiple_files_multiple_messages(self) -> None:
        stdout = json.dumps(
            [
                {
                    "filePath": "a.js",
                    "messages": [
                        {"line": 1, "column": 1, "severity": 2, "message": "e1"},
                        {"line": 2, "column": 1, "severity": 1, "message": "e2"},
                    ],
                },
                {
                    "filePath": "b.js",
                    "messages": [{"line": 3, "column": 1, "severity": 2, "message": "e3"}],
                },
            ]
        )
        errors = self.validator.parse_output(stdout, "", 1)
        assert len(errors) == 3

    def test_missing_fields_defaults(self) -> None:
        stdout = json.dumps([{"filePath": "a.js", "messages": [{}]}])
        errors = self.validator.parse_output(stdout, "", 1)
        assert len(errors) == 1
        assert errors[0].line == 0
        assert errors[0].column == 0
        assert errors[0].rule is None

    def test_build_command(self) -> None:
        cmd = self.validator.build_command("/my/workspace")
        assert "eslint" in cmd
        assert "/my/workspace" in cmd


# ── Clippy Validator ──────────────────────────────────────────────


class TestCargoClippyValidator:
    """Tests for CargoClippyValidator.parse_output()."""

    def setup_method(self) -> None:
        self.validator = CargoClippyValidator()

    def _make_compiler_message(
        self,
        *,
        level: str = "warning",
        message: str = "unused variable",
        code: str | None = "unused_variables",
        file_name: str = "src/main.rs",
        line_start: int = 5,
        column_start: int = 9,
        is_primary: bool = True,
        suggested_replacement: str | None = None,
    ) -> str:
        span = {
            "file_name": file_name,
            "line_start": line_start,
            "column_start": column_start,
            "is_primary": is_primary,
        }
        if suggested_replacement is not None:
            span["suggested_replacement"] = suggested_replacement

        data = {
            "reason": "compiler-message",
            "message": {
                "level": level,
                "message": message,
                "code": {"code": code} if code else None,
                "spans": [span],
            },
        }
        return json.dumps(data)

    def test_valid_warning(self) -> None:
        stdout = self._make_compiler_message()
        errors = self.validator.parse_output(stdout, "", 1)
        assert len(errors) == 1
        assert errors[0].file == "src/main.rs"
        assert errors[0].line == 5
        assert errors[0].column == 9
        assert errors[0].severity == "warning"
        assert errors[0].message == "unused variable"
        assert errors[0].rule == "unused_variables"
        assert errors[0].fix_available is False

    def test_error_severity(self) -> None:
        stdout = self._make_compiler_message(level="error")
        errors = self.validator.parse_output(stdout, "", 1)
        assert errors[0].severity == "error"

    def test_note_maps_to_info(self) -> None:
        stdout = self._make_compiler_message(level="note")
        errors = self.validator.parse_output(stdout, "", 1)
        assert errors[0].severity == "info"

    def test_help_maps_to_info(self) -> None:
        stdout = self._make_compiler_message(level="help")
        errors = self.validator.parse_output(stdout, "", 1)
        assert errors[0].severity == "info"

    def test_with_suggestion(self) -> None:
        stdout = self._make_compiler_message(suggested_replacement="_x")
        errors = self.validator.parse_output(stdout, "", 1)
        assert errors[0].fix_available is True

    def test_no_code(self) -> None:
        stdout = self._make_compiler_message(code=None)
        errors = self.validator.parse_output(stdout, "", 1)
        assert errors[0].rule is None

    def test_empty_output(self) -> None:
        assert self.validator.parse_output("", "", 0) == []

    def test_non_compiler_message_skipped(self) -> None:
        line = json.dumps({"reason": "build-script-executed", "data": {}})
        assert self.validator.parse_output(line, "", 0) == []

    def test_malformed_json_lines_skipped(self) -> None:
        stdout = "not json\n" + self._make_compiler_message()
        errors = self.validator.parse_output(stdout, "", 1)
        assert len(errors) == 1  # only the valid line parsed

    def test_multiple_json_lines(self) -> None:
        lines = [
            self._make_compiler_message(file_name="a.rs", line_start=1),
            json.dumps({"reason": "build-finished", "success": True}),
            self._make_compiler_message(file_name="b.rs", line_start=2),
        ]
        stdout = "\n".join(lines)
        errors = self.validator.parse_output(stdout, "", 1)
        assert len(errors) == 2

    def test_no_spans_uses_defaults(self) -> None:
        data = {
            "reason": "compiler-message",
            "message": {
                "level": "warning",
                "message": "no spans",
                "code": None,
                "spans": [],
            },
        }
        stdout = json.dumps(data)
        errors = self.validator.parse_output(stdout, "", 1)
        assert len(errors) == 1
        assert errors[0].file == "<unknown>"
        assert errors[0].line == 0

    def test_non_primary_span_used_as_fallback(self) -> None:
        data = {
            "reason": "compiler-message",
            "message": {
                "level": "warning",
                "message": "test",
                "code": None,
                "spans": [
                    {
                        "file_name": "fallback.rs",
                        "line_start": 42,
                        "column_start": 7,
                        "is_primary": False,
                    }
                ],
            },
        }
        stdout = json.dumps(data)
        errors = self.validator.parse_output(stdout, "", 1)
        assert len(errors) == 1
        assert errors[0].file == "fallback.rs"
        assert errors[0].line == 42

    def test_build_command(self) -> None:
        cmd = self.validator.build_command("/my/workspace")
        assert "cargo clippy" in cmd
        assert "/my/workspace" in cmd


# ── Cross-validator tests ─────────────────────────────────────────


class TestAllValidatorsDefaultConfig:
    """Verify all validators can be constructed with default config."""

    @pytest.mark.parametrize(
        "validator_cls",
        [RuffValidator, MypyValidator, ESLintValidator, CargoClippyValidator],
    )
    def test_default_construction(self, validator_cls: type) -> None:
        v = validator_cls()
        assert v.config is not None
        assert v.config.name
        assert v.config.command

    @pytest.mark.parametrize(
        "validator_cls",
        [RuffValidator, MypyValidator, ESLintValidator, CargoClippyValidator],
    )
    def test_empty_output_returns_empty_list(self, validator_cls: type) -> None:
        v = validator_cls()
        assert v.parse_output("", "", 0) == []
