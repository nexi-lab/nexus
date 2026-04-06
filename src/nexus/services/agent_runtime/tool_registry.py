"""ToolRegistry — built-in tool dispatch for ManagedAgentLoop.

Registers kernel-level tools (Tier A) that bind to VFS syscalls:
read_file, write_file, edit_file, bash, grep, glob.

External CLI tools (Tier B) are discovered via VFS filesystem navigation
(ls + --help) and executed through the bash tool — no registry needed.

Each tool is self-describing: name, description, input_schema are
intrinsic properties. ToolRegistry collects schemas for LLM function-calling
and dispatches execution by name.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


@runtime_checkable
class Tool(Protocol):
    """Protocol for built-in agent tools."""

    @property
    def name(self) -> str: ...

    @property
    def description(self) -> str: ...

    @property
    def input_schema(self) -> dict[str, Any]: ...

    async def call(self, **kwargs: Any) -> str: ...

    def is_read_only(self) -> bool: ...

    def is_concurrent_safe(self) -> bool: ...


class ToolRegistry:
    """Registry for built-in agent tools.

    Handles:
    - Tool registration and lookup
    - Schema generation for LLM function-calling
    - Dispatch by name with concurrent/serial classification
    """

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        """Register a tool by name."""
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        """Look up a tool by name."""
        return self._tools.get(name)

    def schemas(self) -> list[dict[str, Any]]:
        """Generate OpenAI-compatible tool schemas for LLM function-calling."""
        return [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.input_schema,
                },
            }
            for tool in self._tools.values()
        ]

    async def execute_one(self, tool_call: dict[str, Any]) -> str:
        """Execute a single tool call, return result string."""
        func = tool_call.get("function", {})
        name = func.get("name", "")
        tool = self._tools.get(name)
        if tool is None:
            return json.dumps({"error": f"Unknown tool: {name}"})

        try:
            kwargs = json.loads(func.get("arguments", "{}"))
        except (json.JSONDecodeError, TypeError):
            return json.dumps({"error": f"Invalid arguments for tool {name}"})

        try:
            return await tool.call(**kwargs)
        except Exception as exc:
            logger.error("Tool %s failed: %s", name, exc)
            return json.dumps({"error": str(exc)})

    async def execute(self, tool_calls: list[dict[str, Any]]) -> list[str]:
        """Execute multiple tool calls with concurrency classification.

        Concurrent-safe tools run in parallel via asyncio.gather.
        Serial tools run sequentially.
        """
        if not tool_calls:
            return []

        # Classify into concurrent vs serial
        concurrent: list[tuple[int, dict[str, Any]]] = []
        serial: list[tuple[int, dict[str, Any]]] = []

        for i, tc in enumerate(tool_calls):
            name = tc.get("function", {}).get("name", "")
            tool = self._tools.get(name)
            if tool and tool.is_concurrent_safe():
                concurrent.append((i, tc))
            else:
                serial.append((i, tc))

        results: list[str] = [""] * len(tool_calls)

        # Run concurrent tools in parallel
        if concurrent:
            coros = [self.execute_one(tc) for _, tc in concurrent]
            concurrent_results = await asyncio.gather(*coros)
            for (i, _), result in zip(concurrent, concurrent_results, strict=True):
                results[i] = result

        # Run serial tools sequentially
        for i, tc in serial:
            results[i] = await self.execute_one(tc)

        return results
