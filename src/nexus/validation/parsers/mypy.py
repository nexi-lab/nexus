"""Mypy output parser.

Parses text output from `mypy --no-error-summary`.
"""

from __future__ import annotations

import logging
import re
import shlex
from typing import Literal

from nexus.validation.models import ValidationError, ValidatorConfig
from nexus.validation.parsers.base import Validator

logger = logging.getLogger(__name__)

_Severity = Literal["error", "warning", "info"]

# mypy output: file.py:line:col: severity: message  [error-code]
_MYPY_LINE_PATTERN = re.compile(
    r"^(.+):(\d+):(\d+): (error|warning|note): (.+?)(?:\s+\[(.+)\])?$"
)

_SEVERITY_MAP: dict[str, _Severity] = {
    "error": "error",
    "warning": "warning",
    "note": "info",
}


class MypyValidator(Validator):
    """Parser for mypy text output."""

    def __init__(self, config: ValidatorConfig | None = None) -> None:
        if config is None:
            config = ValidatorConfig(
                name="mypy",
                command="mypy --no-error-summary .",
                output_format="text",
            )
        super().__init__(config)

    def build_command(self, workspace_path: str = ".") -> str:
        return f"cd {shlex.quote(workspace_path)} && mypy --no-error-summary ."

    def parse_output(
        self, stdout: str, stderr: str, exit_code: int  # noqa: ARG002
    ) -> list[ValidationError]:
        errors: list[ValidationError] = []

        for line in stdout.splitlines():
            match = _MYPY_LINE_PATTERN.match(line.strip())
            if not match:
                continue

            file_path, line_no, col, severity_raw, message, rule = match.groups()
            severity = _SEVERITY_MAP.get(severity_raw, "info")

            errors.append(
                ValidationError(
                    file=file_path,
                    line=int(line_no),
                    column=int(col),
                    severity=severity,
                    message=message.strip(),
                    rule=rule,
                )
            )

        return errors
