"""EditFileTool — surgical search/replace edits via nx.edit() Tier-2 syscall."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any


class EditFileTool:
    """Apply surgical search/replace edits to a file.

    Wraps NexusFS.edit() which supports:
    - Exact match (fast path)
    - Whitespace-normalized match
    - Fuzzy match (Levenshtein similarity)
    - Unified diff output
    - Optimistic concurrency control via if_match
    """

    name = "edit_file"
    description = (
        "Apply surgical search/replace edits to an existing file. "
        "More reliable than write_file for partial changes — supports fuzzy matching "
        "and returns a diff of changes applied."
    )
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Absolute file path to edit"},
            "edits": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "old_str": {"type": "string", "description": "Text to find"},
                        "new_str": {"type": "string", "description": "Replacement text"},
                    },
                    "required": ["old_str", "new_str"],
                },
                "description": "List of search/replace pairs",
            },
        },
        "required": ["path", "edits"],
    }

    def __init__(self, edit_fn: Callable[..., dict[str, Any]]) -> None:
        self._edit_fn = edit_fn

    def call(self, *, path: str, edits: list[dict[str, str]], **_: Any) -> str:
        edit_pairs = [(e["old_str"], e["new_str"]) for e in edits]
        result = self._edit_fn(path, edit_pairs)
        return json.dumps(result, indent=2)

    def is_read_only(self) -> bool:
        return False

    def is_concurrent_safe(self) -> bool:
        return False
