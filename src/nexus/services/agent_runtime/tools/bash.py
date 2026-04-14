"""BashTool — execute shell commands via subprocess.

Output is truncated to 50K chars (matching CC's limit).
Uses sys_stat to resolve VFS paths (e.g. /workspace/foo.txt) to
host OS physical paths before executing commands.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable
from typing import Any

_OUTPUT_LIMIT = 50_000
_DEFAULT_TIMEOUT = 120

# Match absolute paths starting with / that look like VFS mount paths.
# Excludes common host OS paths (/usr, /bin, /etc, /tmp, /var, /dev, /proc, /sys, /home).
_VFS_PATH_RE = re.compile(
    r"(?<!\w)(/(?!usr/|bin/|etc/|tmp/|var/|dev/|proc/|sys/|home/|Users/|Library/|opt/)\w[\w./*?{}\[\]-]*)"
)


class BashTool:
    """Execute a shell command and return its output.

    When sys_stat is injected, resolves VFS paths in the command to
    host OS physical paths before execution. This allows commands like
    `cat /workspace/foo.txt` to work correctly when /workspace is a
    LocalConnector mount.
    """

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

    def __init__(
        self,
        *,
        cwd: str | None = None,
        sys_stat: Callable[..., dict[str, Any] | None] | None = None,
    ) -> None:
        self._cwd = cwd
        self._sys_stat = sys_stat

    def _resolve_vfs_paths(self, command: str) -> str:
        """Replace VFS paths with host OS physical paths via sys_stat.

        For paths under VFS mounts (e.g. /workspace/foo.txt), uses
        sys_stat to get physical_path. Falls back to mount-point
        resolution: stat the mount root, get its backend info, and
        use cwd as the physical root.
        """
        sys_stat = self._sys_stat
        if sys_stat is None:
            return command

        # Cache: mount_point -> physical_root
        mount_cache: dict[str, str | None] = {}

        def _resolve_mount_root(vfs_path: str) -> str | None:
            """Find physical root for a VFS mount point via sys_stat.

            backend_name format: "type:physical_root" (e.g. "local_connector:/Users/...")
            """
            parts = vfs_path.strip("/").split("/")
            for i in range(1, len(parts) + 1):
                candidate = "/" + "/".join(parts[:i])
                if candidate in mount_cache:
                    return mount_cache[candidate]
                try:
                    stat = sys_stat(candidate)
                    bn = stat.get("backend_name", "") if stat else ""
                    if ":" in bn:
                        phys_root = bn.split(":", 1)[1]
                        mount_cache[candidate] = phys_root
                        return phys_root
                except Exception:
                    pass
            return None

        def _replace(match: re.Match[str]) -> str:
            vfs_path: str = match.group(1)
            # 1. Try direct sys_stat (works for files already in metastore)
            try:
                stat = sys_stat(vfs_path)
                if stat and stat.get("physical_path"):
                    phys = str(stat["physical_path"])
                    if not phys.startswith("/"):
                        pass  # CAS hash, not a real path — fall through
                    else:
                        return phys
            except Exception:
                pass

            # 2. Resolve via mount root (for glob patterns, new files, etc.)
            parts = vfs_path.strip("/").split("/")
            if len(parts) >= 2:
                mount_root = _resolve_mount_root(vfs_path)
                if mount_root:
                    relative = "/".join(parts[1:])  # strip mount name
                    return str(mount_root) + "/" + relative

            return str(vfs_path)

        return _VFS_PATH_RE.sub(_replace, command)

    def call(self, *, command: str, timeout: int = _DEFAULT_TIMEOUT, **_: Any) -> str:
        command = self._resolve_vfs_paths(command)
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
