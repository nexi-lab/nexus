"""ESLint output parser.

Parses JSON output from `npx eslint --format json`.
"""

from __future__ import annotations

import json
import logging
import shlex
from typing import Literal

from nexus.validation.models import ValidationError, ValidatorConfig
from nexus.validation.parsers.base import Validator

logger = logging.getLogger(__name__)

_Severity = Literal["error", "warning", "info"]

# ESLint severity: 1=warning, 2=error
_ESLINT_SEVERITY_MAP: dict[int, _Severity] = {
    1: "warning",
    2: "error",
}


class ESLintValidator(Validator):
    """Parser for ESLint JSON output."""

    def __init__(self, config: ValidatorConfig | None = None) -> None:
        if config is None:
            config = ValidatorConfig(
                name="eslint",
                command="npx eslint --format json .",
                output_format="json",
            )
        super().__init__(config)

    def build_command(self, workspace_path: str = ".") -> str:
        return f"cd {shlex.quote(workspace_path)} && npx eslint --format json ."

    def parse_output(
        self,
        stdout: str,
        stderr: str,  # noqa: ARG002
        exit_code: int,  # noqa: ARG002
    ) -> list[ValidationError]:
        if not stdout.strip():
            return []

        try:
            data = json.loads(stdout)
        except json.JSONDecodeError as e:
            logger.warning("Failed to parse ESLint JSON output: %s", e)
            return []

        if not isinstance(data, list):
            return []

        errors: list[ValidationError] = []
        for file_result in data:
            if not isinstance(file_result, dict):
                continue

            file_path = file_result.get("filePath", "<unknown>")
            messages = file_result.get("messages", [])
            if not isinstance(messages, list):
                continue

            for msg in messages:
                if not isinstance(msg, dict):
                    continue

                severity_code = msg.get("severity", 2)
                severity = _ESLINT_SEVERITY_MAP.get(severity_code, "error")
                fix = msg.get("fix")

                errors.append(
                    ValidationError(
                        file=file_path,
                        line=msg.get("line", 0),
                        column=msg.get("column", 0),
                        severity=severity,
                        message=msg.get("message", ""),
                        rule=msg.get("ruleId"),
                        fix_available=fix is not None,
                    )
                )

        return errors
