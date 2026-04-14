"""GlobTool — find files by glob pattern via SearchService (Rust-accelerated)."""

from __future__ import annotations

import json
from typing import Any

_RESULT_LIMIT = 50_000


class GlobTool:
    """Find files matching a glob pattern (Rust-accelerated).

    Wraps SearchService.glob() which uses nexus_kernel Rust glob primitives
    for 10-20x speedup over Python. Available in all deployment profiles.
    Automatically excludes gitignore-style patterns.
    """

    name = "glob"
    description = (
        "Find files matching a glob pattern. Supports *, **, ?, [...] patterns. "
        "Results sorted by modification time (newest first). "
        "Automatically excludes gitignore-style patterns."
    )
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "Glob pattern (e.g. '**/*.py', 'src/**/*.ts', 'data/*.csv')",
            },
            "path": {
                "type": "string",
                "description": "Base directory to search from (default: /)",
                "default": "/",
            },
        },
        "required": ["pattern"],
    }

    def __init__(self, search_service: Any) -> None:
        self._search = search_service

    def call(self, *, pattern: str, path: str = "/", **_: Any) -> str:
        results = self._search.glob(pattern, path=path)
        output = json.dumps(results, indent=2)
        return output[:_RESULT_LIMIT]

    def is_read_only(self) -> bool:
        return True

    def is_concurrent_safe(self) -> bool:
        return True
