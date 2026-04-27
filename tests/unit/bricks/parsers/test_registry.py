from __future__ import annotations

from typing import Any

from nexus.bricks.parsers.base import Parser
from nexus.bricks.parsers.registry import ParserRegistry
from nexus.bricks.parsers.types import ParseResult


class _Parser(Parser):
    def __init__(self, name: str, priority: int) -> None:
        self._name = name
        self._priority = priority

    @property
    def name(self) -> str:
        return self._name

    @property
    def priority(self) -> int:
        return self._priority

    @property
    def supported_formats(self) -> list[str]:
        return [".pdf"]

    def can_parse(self, file_path: str, mime_type: str | None = None) -> bool:
        return file_path.endswith(".pdf")

    async def parse(self, content: bytes, metadata: dict[str, Any] | None = None) -> ParseResult:
        return ParseResult(text=self.name)


def test_register_overwrite_replaces_priority_indexes() -> None:
    registry = ParserRegistry()
    first = _Parser("same", priority=100)
    replacement = _Parser("same", priority=1)

    registry.register(first)
    registry.register(replacement)

    assert registry.get_parser("doc.pdf") is replacement
    assert registry.get_parsers() == [replacement]
