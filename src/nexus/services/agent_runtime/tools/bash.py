"""BashTool — execute shell commands via subprocess.

Output is truncated to 50K chars (matching CC's limit).
Future: full DT_PIPE integration via SubprocessRunner for kernel observability.
"""

from __future__ import annotations

import json
import subprocess
from typing import Any

_OUTPUT_LIMIT = 50_000
_DEFAULT_TIMEOUT = 120


class BashTool:
    """Execute a shell command and return its output."""

    name = "bash"
    description = (
        "Run a shell command and return combined stdout+stderr. "
        "Commands run in the working directory. Timeout defaults to 120 seconds."
    )
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "Shell command to execute"},
            "timeout": {
                "type": "integer",
                "description": "Timeout in seconds (default: 120)",
            },
        },
        "required": ["command"],
    }

    def __init__(self, *, cwd: str | None = None) -> None:
        self._cwd = cwd

    def call(self, *, command: str, timeout: int = _DEFAULT_TIMEOUT, **_: Any) -> str:
        try:
            result = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                cwd=self._cwd,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return json.dumps({"error": f"Command timed out after {timeout}s", "command": command})
        except Exception as exc:
            return json.dumps({"error": str(exc), "command": command})

        output = (result.stdout + result.stderr).decode("utf-8", errors="replace").strip()
        if not output:
            output = "(no output)"

        exit_code = result.returncode or 0
        if len(output) > _OUTPUT_LIMIT:
            output = output[:_OUTPUT_LIMIT] + f"\n... (truncated, {len(output)} total chars)"

        if exit_code != 0:
            return json.dumps({"exit_code": exit_code, "output": output})
        return output

    def is_read_only(self) -> bool:
        return False

    def is_concurrent_safe(self) -> bool:
        return False
