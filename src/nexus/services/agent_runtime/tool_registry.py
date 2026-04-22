"""ToolRegistry — built-in tool dispatch for ManagedAgentLoop.

Registers kernel-level tools (Tier A) that bind to VFS syscalls:
read_file, write_file, edit_file, bash, grep, glob.

External CLI tools (Tier B) are discovered via VFS filesystem navigation
(ls + --help) and executed through the bash tool — no registry needed.

Each tool is self-describing: name, description, input_schema are
intrinsic properties. ToolRegistry collects schemas for LLM function-calling
and dispatches execution by name.

§2.1 Tool Protocol extension — CC-compatible fields:
    max_result_size_chars, validate_input, check_permissions,
    is_destructive, should_defer.

§2.3 ConcurrencyPolicy — pluggable execution strategy:
    ExclusiveLockPolicy (CC-equivalent): concurrent-safe gather, serial exclusive.

§2.4 Tool result handling — two-tier truncation + spill to VFS:
    TruncationStrategy, ToolResultStorage, MessageBudgetPolicy.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants (CC-compatible defaults, overridable via config)
# ---------------------------------------------------------------------------

DEFAULT_MAX_RESULT_SIZE_CHARS = 50_000
"""Per-tool result cap (CC: toolLimits.ts:4)."""

MAX_TOOL_RESULTS_PER_MESSAGE_CHARS = 200_000
"""Per-message aggregate budget (CC: toolLimits.ts:39)."""

PREVIEW_SIZE_BYTES = 2_000
"""Preview length for truncated results (CC: toolLimits.ts:49)."""


# ---------------------------------------------------------------------------
# §2.1 Tool Protocol (extended)
# ---------------------------------------------------------------------------


@runtime_checkable
class Tool(Protocol):
    """Protocol for built-in agent tools.

    §2.1 extended fields have defaults — existing tools keep working
    without changes. Only override what you need.
    """

    @property
    def name(self) -> str: ...

    @property
    def description(self) -> str: ...

    @property
    def input_schema(self) -> dict[str, Any]: ...

    async def call(self, **kwargs: Any) -> str: ...

    def is_read_only(self) -> bool: ...

    def is_concurrent_safe(self) -> bool: ...

    # §2.1 extensions (all optional with defaults) ---

    @property
    def max_result_size_chars(self) -> int:
        """Per-tool result cap. CC: Tool.ts:466. Default 50K."""
        return DEFAULT_MAX_RESULT_SIZE_CHARS

    def is_destructive(self) -> bool:
        """Marks irreversible ops. CC: Tool.ts:407."""
        return not self.is_read_only()

    @property
    def should_defer(self) -> bool:
        """Lazy schema loading — schema omitted until ToolSearch. CC: Tool.ts:438."""
        return False


# ---------------------------------------------------------------------------
# §2.4 Tool Result — structured result with truncation metadata
# ---------------------------------------------------------------------------


class ToolResult:
    """Structured tool result with truncation metadata."""

    __slots__ = ("tool_call_id", "tool_name", "content", "truncated", "persisted_path")

    def __init__(
        self,
        tool_call_id: str,
        tool_name: str,
        content: str,
        *,
        truncated: bool = False,
        persisted_path: str | None = None,
    ) -> None:
        self.tool_call_id = tool_call_id
        self.tool_name = tool_name
        self.content = content
        self.truncated = truncated
        self.persisted_path = persisted_path


# ---------------------------------------------------------------------------
# §2.4 TruncationStrategy — pluggable preview generation
# ---------------------------------------------------------------------------


class TruncationStrategy(Protocol):
    """Pluggable preview generation for oversized tool results."""

    def generate_preview(self, content: str, max_bytes: int) -> tuple[str, bool]:
        """Return (preview_text, was_truncated).

        Default: head-only, truncated at last newline in [max_bytes*0.5, max_bytes].
        CC: toolResultStorage.ts:339-356.
        """
        ...


class HeadTruncation:
    """CC-compatible head-only truncation at newline boundary."""

    def generate_preview(self, content: str, max_bytes: int) -> tuple[str, bool]:
        if len(content) <= max_bytes:
            return content, False
        # Find last newline in [max_bytes*0.5, max_bytes]
        half = max_bytes // 2
        cut_point = content.rfind("\n", half, max_bytes)
        if cut_point == -1:
            cut_point = max_bytes
        return content[:cut_point], True


# ---------------------------------------------------------------------------
# §2.4 ToolResultStorage — pluggable spill-to-VFS
# ---------------------------------------------------------------------------


class ToolResultStorage(Protocol):
    """Pluggable storage for oversized tool results."""

    async def persist(self, content: str, tool_use_id: str) -> str:
        """Persist full content, return VFS path for read-with-offset."""
        ...


class VFSToolResultStorage:
    """Default: persist to DT_STREAM for read-with-offset.

    Better than CC's separate file: LLM reads same path with offset,
    no need to remember a different path.
    """

    def __init__(self, sys_write: Any) -> None:
        self._sys_write = sys_write

    async def persist(self, content: str, tool_use_id: str) -> str:
        path = f"/root/proc/tool-results/{tool_use_id}"
        await self._sys_write(path, content.encode("utf-8"))
        return path


# ---------------------------------------------------------------------------
# §2.4 MessageBudgetPolicy — per-message aggregate enforcement
# ---------------------------------------------------------------------------


class MessageBudgetPolicy(Protocol):
    """Pluggable per-message budget enforcement."""

    async def enforce(self, results: list[ToolResult], budget: int) -> list[ToolResult]:
        """Ensure total chars <= budget. Truncate largest first."""
        ...


class DefaultMessageBudget:
    """CC-compatible: persist largest results until under budget.

    CC: toolResultStorage.ts:189-356. If N parallel results together
    exceed MAX_TOOL_RESULTS_PER_MESSAGE_CHARS, persist largest until
    under budget.
    """

    def __init__(
        self,
        truncation: TruncationStrategy | None = None,
        storage: ToolResultStorage | None = None,
    ) -> None:
        self._truncation = truncation or HeadTruncation()
        self._storage = storage

    async def enforce(self, results: list[ToolResult], budget: int) -> list[ToolResult]:
        total = sum(len(r.content) for r in results)
        if total <= budget:
            return results

        # Sort indices by content length (descending) — truncate largest first
        indexed = sorted(enumerate(results), key=lambda x: len(x[1].content), reverse=True)

        for idx, result in indexed:
            if total <= budget:
                break
            if len(result.content) <= PREVIEW_SIZE_BYTES:
                continue  # too small to truncate

            preview, was_truncated = self._truncation.generate_preview(
                result.content, PREVIEW_SIZE_BYTES
            )
            if was_truncated:
                saved = len(result.content) - len(preview)
                total -= saved

                persisted_path = ""
                if self._storage:
                    persisted_path = await self._storage.persist(
                        result.content, result.tool_call_id
                    )

                size_kb = len(result.content) / 1024
                marker = f"<persisted-output>\nOutput too large ({size_kb:.1f} KB)."
                if persisted_path:
                    marker += f" Full output saved to: {persisted_path}"
                marker += (
                    f"\n\nPreview (first {PREVIEW_SIZE_BYTES / 1024:.1f} KB):\n"
                    f"{preview}\n...\n"
                    f"</persisted-output>"
                )

                results[idx] = ToolResult(
                    tool_call_id=result.tool_call_id,
                    tool_name=result.tool_name,
                    content=marker,
                    truncated=True,
                    persisted_path=persisted_path,
                )

        return results


# ---------------------------------------------------------------------------
# §2.3 ConcurrencyPolicy — pluggable execution strategy
# ---------------------------------------------------------------------------


class ConcurrencyPolicy(Protocol):
    """Pluggable concurrency control for tool execution."""

    async def execute_batch(
        self, tool_calls: list[dict[str, Any]], registry: "ToolRegistry"
    ) -> list[ToolResult]:
        """Execute a batch of tool calls with concurrency control."""
        ...


class ExclusiveLockPolicy:
    """CC-equivalent: concurrent-safe gather, non-safe exclusive.

    CC: StreamingToolExecutor.ts:129-151. Algorithm:
    - Concurrent-safe tools run in parallel (asyncio.gather)
    - Non-concurrent-safe tools require exclusive access — block ALL others
    - Classify → gather concurrent → serial non-concurrent
    """

    def __init__(
        self,
        truncation: TruncationStrategy | None = None,
        permission_service: Any | None = None,
        bash_validator: Any | None = None,
    ) -> None:
        self._truncation = truncation or HeadTruncation()
        self._permission_service = permission_service
        self._bash_validator = bash_validator

    async def execute_batch(
        self, tool_calls: list[dict[str, Any]], registry: "ToolRegistry"
    ) -> list[ToolResult]:
        if not tool_calls:
            return []

        # Classify into concurrent vs serial
        concurrent: list[tuple[int, dict[str, Any]]] = []
        serial: list[tuple[int, dict[str, Any]]] = []

        for i, tc in enumerate(tool_calls):
            name = tc.get("function", {}).get("name", "")
            tool = registry.get(name)
            if tool and tool.is_concurrent_safe():
                concurrent.append((i, tc))
            else:
                serial.append((i, tc))

        results: list[ToolResult] = [ToolResult(tool_call_id="", tool_name="", content="")] * len(
            tool_calls
        )

        # Run concurrent-safe tools in parallel
        if concurrent:
            coros = [self._execute_one(tc, registry) for _, tc in concurrent]
            concurrent_results = await asyncio.gather(*coros)
            for (i, _), result in zip(concurrent, concurrent_results, strict=True):
                results[i] = result

        # Run serial tools sequentially (exclusive lock)
        for i, tc in serial:
            results[i] = await self._execute_one(tc, registry)

        return results

    async def _execute_one(self, tool_call: dict[str, Any], registry: "ToolRegistry") -> ToolResult:
        """Execute a single tool call with per-tool truncation."""
        func = tool_call.get("function", {})
        name = func.get("name", "")
        tool_call_id = tool_call.get("id", "")
        tool = registry.get(name)

        if tool is None:
            return ToolResult(
                tool_call_id=tool_call_id,
                tool_name=name,
                content=json.dumps({"error": f"Unknown tool: {name}"}),
            )

        try:
            kwargs = json.loads(func.get("arguments", "{}"))
        except (json.JSONDecodeError, TypeError):
            return ToolResult(
                tool_call_id=tool_call_id,
                tool_name=name,
                content=json.dumps({"error": f"Invalid arguments for tool {name}"}),
            )

        # §3.1 Permission check (three-checkpoint pipeline, checkpoint 3)
        if self._permission_service is not None:
            perm = self._permission_service.check(name, kwargs)
            if not perm.allowed:
                return ToolResult(
                    tool_call_id=tool_call_id,
                    tool_name=name,
                    content=json.dumps({"error": f"Permission denied: {perm.reason}"}),
                )

        # §3.3 Bash security check (23-category validator)
        if self._bash_validator is not None and name == "bash":
            command = kwargs.get("command", "")
            sec = self._bash_validator.validate(command)
            if not sec.safe:
                return ToolResult(
                    tool_call_id=tool_call_id,
                    tool_name=name,
                    content=json.dumps(
                        {
                            "error": f"Bash security: {sec.message}",
                            "category": sec.category,
                        }
                    ),
                )

        try:
            raw_result = await tool.call(**kwargs)
        except Exception as exc:
            logger.error("Tool %s failed: %s", name, exc)
            return ToolResult(
                tool_call_id=tool_call_id,
                tool_name=name,
                content=json.dumps({"error": str(exc)}),
            )

        # Empty result handling (CC: toolResultStorage.ts:272-296)
        if not raw_result:
            raw_result = f"({name} completed with no output)"

        # Per-tool truncation (§2.4)
        max_chars = getattr(tool, "max_result_size_chars", DEFAULT_MAX_RESULT_SIZE_CHARS)
        preview, was_truncated = self._truncation.generate_preview(raw_result, max_chars)

        return ToolResult(
            tool_call_id=tool_call_id,
            tool_name=name,
            content=preview,
            truncated=was_truncated,
        )


# ---------------------------------------------------------------------------
# ToolRegistry
# ---------------------------------------------------------------------------


class ToolRegistry:
    """Registry for built-in agent tools.

    Handles:
    - Tool registration and lookup
    - Schema generation for LLM function-calling
    - Dispatch by name with concurrent/serial classification
    """

    def __init__(self) -> None:
        self._tools: dict[str, Any] = {}

    def register(self, tool: Any) -> None:
        """Register a tool by name.

        Accepts any object with ``name``, ``call``, ``is_read_only``,
        ``is_concurrent_safe``, ``description``, and ``input_schema``.
        """
        self._tools[tool.name] = tool

    def get(self, name: str) -> Any | None:
        """Look up a tool by name."""
        return self._tools.get(name)

    def schemas(self) -> list[dict[str, Any]]:
        """Generate OpenAI-compatible tool schemas for LLM function-calling.

        §2.5: Tools with should_defer=True are excluded from schemas.
        """
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
            if not getattr(tool, "should_defer", False)
        ]

    def deferred_tool_names(self) -> list[str]:
        """Return names of deferred tools (for system prompt ToolSearch hint)."""
        return [tool.name for tool in self._tools.values() if getattr(tool, "should_defer", False)]

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
            result = await tool.call(**kwargs)
        except Exception as exc:
            logger.error("Tool %s failed: %s", name, exc)
            return json.dumps({"error": str(exc)})

        # Empty result handling (CC: toolResultStorage.ts:272-296)
        if not result:
            result = f"({name} completed with no output)"

        return result

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
