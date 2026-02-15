"""Validator abstract base class.

Defines the interface that all validator parsers must implement:
- parse_output: convert tool stdout/stderr into ValidationError list
- build_command: produce the shell command to run the tool
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from nexus.validation.models import ValidationError, ValidatorConfig


class Validator(ABC):
    """Abstract base for linter/checker output parsers."""

    def __init__(self, config: ValidatorConfig) -> None:
        self.config = config

    @abstractmethod
    def parse_output(
        self, stdout: str, stderr: str, exit_code: int
    ) -> list[ValidationError]:
        """Parse tool output into structured errors.

        Args:
            stdout: Standard output from the tool.
            stderr: Standard error from the tool.
            exit_code: Process exit code.

        Returns:
            List of structured validation errors.
        """

    def build_command(self, workspace_path: str = ".") -> str:  # noqa: ARG002
        """Build the shell command to run this validator.

        Default implementation uses config.command directly.
        Subclasses may override to customize per-workspace behavior.

        Args:
            workspace_path: Path to the workspace root.

        Returns:
            Shell command string.
        """
        return self.config.command
