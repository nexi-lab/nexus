"""Ruff output parser.

Parses JSON output from `ruff check --output-format json`.
"""

from __future__ import annotations

import json
import logging
import shlex

from nexus.validation.models import ValidationError, ValidatorConfig
from nexus.validation.parsers.base import Validator

logger = logging.getLogger(__name__)


class RuffValidator(Validator):
    """Parser for ruff check JSON output."""

    def __init__(self, config: ValidatorConfig | None = None) -> None:
        if config is None:
            config = ValidatorConfig(
                name="ruff",
                command="ruff check --output-format json .",
                output_format="json",
            )
        super().__init__(config)

    def build_command(self, workspace_path: str = ".") -> str:
        return f"cd {shlex.quote(workspace_path)} && ruff check --output-format json ."

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
            logger.warning("Failed to parse ruff JSON output: %s", e)
            return []

        if not isinstance(data, list):
            return []

        errors: list[ValidationError] = []
        for item in data:
            if not isinstance(item, dict):
                continue

            location = item.get("location", {})
            if not isinstance(location, dict):
                location = {}

            fix = item.get("fix")
            errors.append(
                ValidationError(
                    file=item.get("filename", "<unknown>"),
                    line=location.get("row", 0),
                    column=location.get("column", 0),
                    severity="error",
                    message=item.get("message", ""),
                    rule=item.get("code"),
                    fix_available=fix is not None,
                )
            )

        return errors
