"""Cargo Clippy output parser.

Parses JSON-lines output from `cargo clippy --message-format json`.
"""

from __future__ import annotations

import json
import logging
import shlex

from nexus.validation.models import ValidationError, ValidatorConfig
from nexus.validation.parsers.base import Validator

logger = logging.getLogger(__name__)

_CLIPPY_LEVEL_MAP = {
    "error": "error",
    "warning": "warning",
    "note": "info",
    "help": "info",
}


class CargoClippyValidator(Validator):
    """Parser for cargo clippy JSON output."""

    def __init__(self, config: ValidatorConfig | None = None) -> None:
        if config is None:
            config = ValidatorConfig(
                name="cargo-clippy",
                command="cargo clippy --message-format json 2>&1",
                output_format="json",
            )
        super().__init__(config)

    def build_command(self, workspace_path: str = ".") -> str:
        return f"cd {shlex.quote(workspace_path)} && cargo clippy --message-format json 2>&1"

    def parse_output(
        self, stdout: str, stderr: str, exit_code: int
    ) -> list[ValidationError]:
        errors: list[ValidationError] = []

        for line in stdout.splitlines():
            line = line.strip()
            if not line:
                continue

            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue

            if not isinstance(data, dict):
                continue

            # Only process compiler-message entries
            if data.get("reason") != "compiler-message":
                continue

            message = data.get("message", {})
            if not isinstance(message, dict):
                continue

            level = message.get("level", "warning")
            severity = _CLIPPY_LEVEL_MAP.get(level, "warning")
            msg_text = message.get("message", "")
            code = message.get("code")
            rule = code.get("code") if isinstance(code, dict) else None

            # Extract primary span
            spans = message.get("spans", [])
            if not isinstance(spans, list):
                spans = []

            primary_span = None
            for span in spans:
                if isinstance(span, dict) and span.get("is_primary", False):
                    primary_span = span
                    break

            if primary_span is None and spans:
                primary_span = spans[0] if isinstance(spans[0], dict) else None

            file_name = primary_span.get("file_name", "<unknown>") if primary_span else "<unknown>"
            line_no = primary_span.get("line_start", 0) if primary_span else 0
            column = primary_span.get("column_start", 0) if primary_span else 0
            suggestion = primary_span.get("suggested_replacement") if primary_span else None

            errors.append(
                ValidationError(
                    file=file_name,
                    line=line_no,
                    column=column,
                    severity=severity,  # type: ignore[arg-type]
                    message=msg_text,
                    rule=rule,
                    fix_available=suggestion is not None,
                )
            )

        return errors
