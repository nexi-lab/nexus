"""GrepTool — search file contents via SearchService (Rust-accelerated)."""

from __future__ import annotations

import json
from typing import Any, Protocol

_RESULT_LIMIT = 50_000


class _SearchService(Protocol):
    """Minimal protocol for SearchService grep."""

    def grep(
        self,
        pattern: str,
        path: str = "/",
        file_pattern: str | None = None,
        ignore_case: bool = False,
        max_results: int = 1000,
        search_mode: str = "auto",
        context: Any = None,
        before_context: int = 0,
        after_context: int = 0,
        invert_match: bool = False,
    ) -> list[dict[str, Any]]: ...


class GrepTool:
    """Search file contents using regex patterns (Rust-accelerated).

    Wraps SearchService.grep() which uses nexus_kernel Rust grep primitives
    for 50-100x speedup over Python. Available in all deployment profiles.
    """

    name = "grep"
    description = (
        "Search file contents using regex patterns. Returns matching lines "
        "with file path, line number, and content. Supports case-insensitive "
        "search and file pattern filtering."
    )
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Regex pattern to search for"},
            "path": {
                "type": "string",
                "description": "Directory to search in (default: /)",
                "default": "/",
            },
            "file_pattern": {
                "type": "string",
                "description": "Glob pattern to filter files (e.g. '*.py', '*.ts')",
            },
            "ignore_case": {
                "type": "boolean",
                "description": "Case-insensitive search",
                "default": False,
            },
            "max_results": {
                "type": "integer",
                "description": "Maximum results to return (default: 100)",
                "default": 100,
            },
        },
        "required": ["pattern"],
    }

    def __init__(self, search_service: Any) -> None:
        self._search = search_service

    def call(
        self,
        *,
        pattern: str,
        path: str = "/",
        file_pattern: str | None = None,
        ignore_case: bool = False,
        max_results: int = 100,
        **_: Any,
    ) -> str:
        results = self._search.grep(
            pattern,
            path=path,
            file_pattern=file_pattern,
            ignore_case=ignore_case,
            max_results=max_results,
        )
        output = json.dumps(results, indent=2)
        return output[:_RESULT_LIMIT]

    def is_read_only(self) -> bool:
        return True

    def is_concurrent_safe(self) -> bool:
        return True
