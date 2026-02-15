"""Combined bash script builder for validation pipeline.

Builds a single bash script that runs all validators with delimiter-based
output, capturing per-tool exit code, stdout, and stderr. This avoids
N+1 sandbox exec calls by running everything in one shot.
"""

from __future__ import annotations

import json
import uuid

from nexus.validation.models import ValidationError, ValidatorConfig
from nexus.validation.parsers import BUILTIN_VALIDATORS, Validator
from nexus.validation.parsers.base import Validator as BaseValidator


class _GenericValidator(BaseValidator):
    """Fallback validator for unknown tools — returns no parsed errors."""

    def parse_output(
        self,
        stdout: str,  # noqa: ARG002
        stderr: str,  # noqa: ARG002
        exit_code: int,  # noqa: ARG002
    ) -> list[ValidationError]:
        return []


def _get_validator_instance(config: ValidatorConfig) -> Validator:
    """Get a Validator instance for the given config.

    Uses the builtin registry if available, otherwise creates a
    generic validator using the base config command.
    """
    cls = BUILTIN_VALIDATORS.get(config.name)
    if cls is not None:
        return cls(config)
    return _GenericValidator(config)


def build_simple_validation_script(
    configs: list[ValidatorConfig],
    workspace_path: str = "/workspace",
) -> str:
    """Build a validation script using separator-based output.

    Uses clear delimiters between validator outputs for reliable parsing.
    Each invocation uses a unique run ID to prevent temp file collisions
    when multiple pipelines run concurrently in the same sandbox.

    The output format per validator is:
    ===VALIDATOR_START===<name>===
    <stdout>
    ===VALIDATOR_STDERR===
    <stderr>
    ===VALIDATOR_EXIT===<exit_code>===
    ===VALIDATOR_END===

    Args:
        configs: List of validator configurations to run.
        workspace_path: Path to the workspace root.

    Returns:
        Bash script string.
    """
    if not configs:
        return "echo 'NO_VALIDATORS'"

    run_id = uuid.uuid4().hex[:8]

    parts: list[str] = [
        "#!/bin/bash",
        "set +e",
        "",
    ]

    for i, config in enumerate(configs):
        validator = _get_validator_instance(config)
        cmd = validator.build_command(workspace_path)
        timeout_val = config.timeout
        stderr_file = f"/tmp/_val_stderr_{run_id}_{i}"

        parts.append(f"echo '===VALIDATOR_START==={config.name}==='")
        parts.append(f"timeout {timeout_val} bash -c {json.dumps(cmd)} 2>{stderr_file}")
        parts.append("_EXIT=$?")
        parts.append("echo '===VALIDATOR_STDERR==='")
        parts.append(f"cat {stderr_file} 2>/dev/null")
        parts.append('echo "===VALIDATOR_EXIT===${_EXIT}==="')
        parts.append("echo '===VALIDATOR_END==='")
        parts.append(f"rm -f {stderr_file}")
        parts.append("")

    return "\n".join(parts)


def parse_simple_script_output(
    stdout: str,
) -> list[dict[str, str | int]]:
    """Parse the output from build_simple_validation_script.

    Args:
        stdout: Combined script output.

    Returns:
        List of dicts with name, stdout, stderr, exit_code per validator.
    """
    results: list[dict[str, str | int]] = []
    current: dict[str, str | int] | None = None
    section = "stdout"
    lines: list[str] = []

    for line in stdout.splitlines():
        if line.startswith("===VALIDATOR_START==="):
            name = line.replace("===VALIDATOR_START===", "").rstrip("=")
            current = {"name": name, "stdout": "", "stderr": "", "exit_code": 1}
            section = "stdout"
            lines = []
        elif line == "===VALIDATOR_STDERR===" and current is not None:
            current["stdout"] = "\n".join(lines)
            section = "stderr"
            lines = []
        elif line.startswith("===VALIDATOR_EXIT===") and current is not None:
            if section == "stderr":
                current["stderr"] = "\n".join(lines)
            exit_str = line.replace("===VALIDATOR_EXIT===", "").rstrip("=")
            try:
                current["exit_code"] = int(exit_str)
            except ValueError:
                current["exit_code"] = 1
            lines = []
        elif line == "===VALIDATOR_END===" and current is not None:
            results.append(current)
            current = None
            section = "stdout"
            lines = []
        else:
            lines.append(line)

    return results
