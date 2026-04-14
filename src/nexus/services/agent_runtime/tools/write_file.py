"""WriteFileTool — write file contents via VFS sys_write."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any


class WriteFileTool:
    """Create or overwrite a file with the given content."""

    name = "write_file"
    description = (
        "Write content to a file. Creates the file if it does not exist, overwrites if it does."
    )
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Absolute file path to write"},
            "content": {"type": "string", "description": "Content to write to the file"},
        },
        "required": ["path", "content"],
    }

    def __init__(self, sys_write: Callable[[str, bytes], Any]) -> None:
        self._sys_write = sys_write

    def call(self, *, path: str, content: str, **_: Any) -> str:
        self._sys_write(path, content.encode("utf-8"))
        return json.dumps({"status": "ok", "path": path, "bytes_written": len(content)})

    def is_read_only(self) -> bool:
        return False

    def is_concurrent_safe(self) -> bool:
        return False
