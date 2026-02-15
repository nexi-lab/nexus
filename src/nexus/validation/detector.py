"""Heuristic project type detection for sandbox workspaces.

Detects which validators to run based on project files present in
the workspace. Uses a single batch `ls` command for efficiency.
"""

from __future__ import annotations

import logging
import shlex
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nexus.sandbox.sandbox_provider import SandboxProvider

logger = logging.getLogger(__name__)

# Detection rules: (marker_files, validator_names)
# If ANY marker file is found, the associated validators are suggested.
# Pre-compute frozensets for O(1) intersection.
DETECTION_RULES: list[tuple[frozenset[str], list[str]]] = [
    (
        frozenset(["pyproject.toml", "setup.py", "ruff.toml", ".ruff.toml"]),
        ["ruff", "mypy"],
    ),
    (
        frozenset(["package.json", ".eslintrc", ".eslintrc.js", ".eslintrc.json", ".eslintrc.yml"]),
        ["eslint"],
    ),
    (frozenset(["Cargo.toml"]), ["cargo-clippy"]),
]

# All marker files flattened for debug logging
_ALL_MARKER_FILES: frozenset[str] = frozenset(
    f for rule_files, _ in DETECTION_RULES for f in rule_files
)


async def detect_project_validators(
    sandbox_id: str,
    provider: SandboxProvider,
    workspace_path: str = "/workspace",
) -> list[str]:
    """Detect applicable validators by checking workspace files.

    Runs a single `ls` command in the sandbox to check for marker files,
    then matches against detection rules.

    Args:
        sandbox_id: Sandbox identifier.
        provider: Sandbox provider for executing commands.
        workspace_path: Path to the workspace root in the sandbox.

    Returns:
        List of validator names (e.g., ["ruff", "mypy"]).
    """
    try:
        result = await provider.run_code(
            sandbox_id,
            "bash",
            f"ls -1 {shlex.quote(workspace_path + '/')} 2>/dev/null",
            timeout=10,
        )
    except Exception as e:
        logger.warning("Failed to list workspace files for detection: %s", e)
        return []

    if result.exit_code != 0:
        logger.debug("ls command returned non-zero exit code: %d", result.exit_code)
        return []

    found_files = frozenset(result.stdout.strip().splitlines())
    seen: dict[str, None] = {}

    for marker_files, validator_names in DETECTION_RULES:
        if found_files & marker_files:
            for name in validator_names:
                seen.setdefault(name, None)

    validators = list(seen.keys())

    logger.debug(
        "Detected validators %s from workspace files %s",
        validators,
        found_files & _ALL_MARKER_FILES,
    )
    return validators
