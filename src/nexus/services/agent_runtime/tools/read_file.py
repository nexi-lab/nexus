"""ReadFileTool — read file contents via VFS sys_read."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

_RESULT_LIMIT = 50_000


class ReadFileTool:
    """Read file contents. Returns UTF-8 text."""

    name = "read_file"
    description = (
        "Read file contents from the filesystem. Returns UTF-8 text. "
        "Use limit to restrict the number of lines returned."
    )
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Absolute file path to read"},
            "limit": {
                "type": "integer",
                "description": "Max number of lines to return (omit for all)",
            },
        },
        "required": ["path"],
    }

    def __init__(self, sys_read: Callable[[str], bytes]) -> None:
        self._sys_read = sys_read

    def call(self, *, path: str, limit: int | None = None, **_: Any) -> str:
        data = self._sys_read(path)
        text = data.decode("utf-8", errors="replace")
        if limit is not None:
            lines = text.splitlines(keepends=True)
            if len(lines) > limit:
                text = "".join(lines[:limit]) + f"\n... ({len(lines) - limit} more lines)"
        return text[:_RESULT_LIMIT]

    def is_read_only(self) -> bool:
        return True

    def is_concurrent_safe(self) -> bool:
        return True
