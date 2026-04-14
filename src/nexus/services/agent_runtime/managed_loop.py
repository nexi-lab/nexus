"""ManagedAgentLoop — kernel-managed LLM reasoning loop (everything-is-a-file).

1st-party agent where ALL I/O routes through kernel VFS syscalls:

    LLM call:       llm_backend.generate_streaming(request) → CC-format frames
    Conversation:   sys_write(conv_path, messages) → CAS persistence
    System prompt:  sys_read(agent_path/SYSTEM.md) → VFS-backed config
    Tools config:   sys_read(agent_path/tools.json) → VFS-backed tool defs
    Tool execution: sys_read / sys_write → VFS syscalls
    Session result: sys_write(proc/{pid}/result) → VFS persistence

Transport yields CC-format content block frames (text, thinking, tool_use,
usage, stop). ManagedAgentLoop iterates the generator directly — no DT_STREAM,
no queue, no background thread.

Reuses AgentObserver for notification accumulation (shared with
3rd-party AcpConnection).

DI dependencies (kernel syscall callables):
    - sys_read:  NexusFS.sys_read wrapper
    - sys_write: NexusFS.sys_write wrapper
    - llm_backend: CAS backend with generate_streaming()
    - agent_path: VFS path for agent config (SYSTEM.md, tools.json)
    - llm_path: VFS mount path for LLM backend
    - conv_path: VFS path for conversation persistence (CAS)
    - proc_path: VFS path for process state (/{zone}/proc/{pid})

References:
    - Task #1510: AgentService (Tier 1)
    - Task #1589: LLM backend driver design
    - system_services/acp/connection.py — AcpConnection pattern
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from nexus.contracts.exceptions import BackendError
from nexus.services.agent_runtime.compaction import (
    CompactionStrategy,
    DefaultCompactionStrategy,
)
from nexus.services.agent_runtime.observer import AgentObserver, AgentTurnResult
from nexus.services.agent_runtime.permissions import (
    BashCommandValidator,
    PermissionService,
)
from nexus.services.agent_runtime.tool_registry import (
    MAX_TOOL_RESULTS_PER_MESSAGE_CHARS,
    ConcurrencyPolicy,
    DefaultMessageBudget,
    ExclusiveLockPolicy,
    MessageBudgetPolicy,
    ToolRegistry,
)

if TYPE_CHECKING:
    from nexus.backends.compute.openai_compatible import CASOpenAIBackend

logger = logging.getLogger(__name__)

# Default retry settings (matching CC's withRetry.ts pattern)
_DEFAULT_MAX_RETRIES = 5
_DEFAULT_BASE_DELAY = 1.0  # seconds

# Kernel syscall callables (injected from NexusFS — all sync after PR #3717).
SysReadFn = Callable[[str], bytes]
SysWriteFn = Callable[[str, bytes], Any]

# Maximum reasoning turns before forced stop (prevent infinite loops).
_MAX_TURNS = 50


class ManagedAgentLoop:
    """Kernel-managed LLM reasoning loop — everything-is-a-file.

    Every I/O operation goes through kernel VFS syscalls:

    - LLM calls via ``generate_streaming()`` (direct generator iteration)
    - Conversation via ``sys_write`` to CAS-addressed VFS path
    - Config (system prompt, tools) via ``sys_read`` from VFS
    - Tool execution via ``sys_read`` / ``sys_write`` (VFS)
    - Results via ``sys_write`` to proc filesystem

    Shares ``AgentObserver`` with AcpConnection for identical
    notification handling — external consumers see the same format.
    """

    def __init__(
        self,
        *,
        sys_read: SysReadFn,
        sys_write: SysWriteFn,
        llm_backend: "CASOpenAIBackend",
        agent_path: str,
        llm_path: str,
        conv_path: str,
        proc_path: str,
        model: str | None = None,
        max_turns: int = _MAX_TURNS,
        tool_registry: ToolRegistry | None = None,
        max_retries: int = _DEFAULT_MAX_RETRIES,
        compactor: CompactionStrategy | None = None,
        concurrency_policy: ConcurrencyPolicy | None = None,
        message_budget: MessageBudgetPolicy | None = None,
        permission_service: PermissionService | None = None,
        bash_validator: BashCommandValidator | None = None,
        cwd: str = "",
    ) -> None:
        self._sys_read = sys_read
        self._sys_write = sys_write
        self._llm_backend = llm_backend
        self._agent_path = agent_path  # /{zone}/agents/{id}
        self._llm_path = llm_path  # /{zone}/llm/openai
        self._conv_path = conv_path  # /{zone}/agents/{id}/conversation
        self._proc_path = proc_path  # /{zone}/proc/{pid}
        self._model = model
        self._max_turns = max_turns
        self._max_retries = max_retries
        self._session_id = str(uuid.uuid4())
        self._tool_registry = tool_registry
        self._cwd = cwd
        self._compactor: CompactionStrategy = compactor or DefaultCompactionStrategy(
            sys_write=sys_write,
            agent_path=agent_path,
        )
        # §3: Permission + bash security (injected into ConcurrencyPolicy)
        _perm = permission_service
        _bash = bash_validator or BashCommandValidator()
        self._concurrency_policy: ConcurrencyPolicy = concurrency_policy or ExclusiveLockPolicy(
            permission_service=_perm,
            bash_validator=_bash,
        )
        self._message_budget: MessageBudgetPolicy = message_budget or DefaultMessageBudget()

        # Shared observer (same logic as AcpConnection)
        self._observer = AgentObserver()

        # Conversation state — persisted to VFS after each mutation.
        self._messages: list[dict[str, Any]] = []

    @property
    def observer(self) -> AgentObserver:
        return self._observer

    @property
    def messages(self) -> list[dict[str, Any]]:
        return list(self._messages)

    @property
    def session_id(self) -> str:
        return self._session_id

    # ------------------------------------------------------------------
    # Lifecycle — load config from VFS
    # ------------------------------------------------------------------

    async def initialize(self) -> None:
        """Load agent config from VFS (system prompt, tools).

        System prompt assembled from multiple VFS sources (§4.2):
            {agent_path}/SYSTEM.md → identity + guidelines
            Runtime environment → platform, model, git status
            {agent_path}/prompts/*.md → optional fragments
            {cwd}/.nexus/agent.md → project context
        """
        from nexus.services.agent_runtime.system_prompt import assemble_system_prompt

        # Extract zone_id and agent_id from agent_path (/{zone}/agents/{id})
        parts = self._agent_path.strip("/").split("/")
        zone_id = parts[0] if len(parts) >= 3 else "root"
        agent_id = parts[2] if len(parts) >= 3 else ""

        system_prompt = assemble_system_prompt(
            sys_read=self._sys_read,
            zone_id=zone_id,
            agent_id=agent_id,
            cwd=self._cwd,
            model=self._model,
        )
        if system_prompt:
            self._messages.append({"role": "system", "content": system_prompt})

        # Tool definitions: prefer ToolRegistry schemas, fall back to VFS config.
        self._tools: list[dict[str, Any]] = []
        if self._tool_registry:
            self._tools = self._tool_registry.schemas()
        if not self._tools:
            try:
                tools_bytes = self._sys_read(f"{self._agent_path}/tools.json")
                self._tools = json.loads(tools_bytes)
            except Exception:
                logger.debug("No tools config at %s/tools.json", self._agent_path)

    # ------------------------------------------------------------------
    # Main reasoning loop
    # ------------------------------------------------------------------

    async def run(self, prompt: str) -> AgentTurnResult:
        """Run the reasoning loop for a single user prompt.

        1. Append user message → persist conversation (sys_write)
        2. Call LLM via generate_streaming() → iterate CC-format frames
        3. Parse response: tool_calls? → execute via VFS → loop
                           text? → persist result → return
        """
        self._messages.append({"role": "user", "content": prompt})
        self._persist_conversation()
        self._observer.reset_turn()

        turns = 0
        while turns < self._max_turns:
            turns += 1

            # Context compaction before LLM call (§4.1)
            self._compactor.micro_compact(self._messages)
            if self._compactor.should_auto_compact(self._messages):
                self._messages = await self._compactor.auto_compact(self._messages)
                self._persist_conversation()

            # Call LLM with retry — direct generator iteration
            response_text, tool_calls, meta = self._call_llm_with_retry()

            # Emit model name from metadata
            if meta:
                self._observer.model_name = meta.get("model")

            # Append assistant message → persist conversation (sys_write)
            assistant_msg: dict[str, Any] = {
                "role": "assistant",
                "content": response_text,
            }
            if tool_calls:
                assistant_msg["tool_calls"] = tool_calls
            self._messages.append(assistant_msg)
            self._persist_conversation()

            # No tool calls → done
            if not tool_calls:
                result = self._observer.finish_turn(
                    stop_reason=meta.get("stop_reason", "stop") if meta else "stop"
                )
                self._persist_result(result)
                return result

            # Execute tool calls via ConcurrencyPolicy (§2.3)
            for tc in tool_calls:
                self._observer.observe_update("tool_call", tc)

            if self._tool_registry:
                # §2.3: ConcurrencyPolicy — parallel/serial by tool classification
                tool_results = await self._concurrency_policy.execute_batch(
                    tool_calls, self._tool_registry
                )
                # §2.4: MessageBudgetPolicy — enforce per-message aggregate cap
                tool_results = await self._message_budget.enforce(
                    tool_results, MAX_TOOL_RESULTS_PER_MESSAGE_CHARS
                )
                for tr in tool_results:
                    # §4A.4: emit tool_call completion status for ACP UI
                    if tr.truncated or tr.content.startswith('{"error"'):
                        self._observer.observe_update(
                            "tool_call_failed",
                            {"tool_call_id": tr.tool_call_id, "error": tr.content},
                        )
                    else:
                        self._observer.observe_update(
                            "tool_call_complete",
                            {"tool_call_id": tr.tool_call_id, "content": tr.content[:200]},
                        )
                    self._messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tr.tool_call_id,
                            "content": tr.content,
                        }
                    )
            else:
                # Legacy fallback (no ToolRegistry)
                for tc in tool_calls:
                    tool_result = await self._execute_tool(tc)
                    self._messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc["id"],
                            "content": tool_result,
                        }
                    )
            self._persist_conversation()

        # Max turns reached
        logger.warning("ManagedAgentLoop: max turns (%d) reached", self._max_turns)
        result = self._observer.finish_turn(stop_reason="max_turns")
        self._persist_result(result)
        return result

    # ------------------------------------------------------------------
    # LLM call with retry (exponential backoff)
    # ------------------------------------------------------------------

    def _call_llm_with_retry(
        self,
    ) -> tuple[str, list[dict[str, Any]], dict[str, Any] | None]:
        """Call LLM with exponential backoff retry on transient failures.

        Retry policy (matching CC's withRetry.ts):
        - Rate limit (429) / server error (5xx): retry with exponential backoff
        - Auth error: fail immediately (no retry)
        - Network error: retry with exponential backoff
        - Other errors: fail immediately
        """
        last_exc: Exception | None = None
        for attempt in range(self._max_retries):
            try:
                return self._call_llm()
            except BackendError as exc:
                err_msg = str(exc).lower()
                # Auth errors: fail immediately
                if "auth" in err_msg or "api key" in err_msg or "unauthorized" in err_msg:
                    raise
                # Transient errors: retry with backoff
                last_exc = exc
                delay = _DEFAULT_BASE_DELAY * (2**attempt)
                logger.warning(
                    "LLM call failed (attempt %d/%d), retrying in %.1fs: %s",
                    attempt + 1,
                    self._max_retries,
                    delay,
                    exc,
                )
                time.sleep(delay)
            except (TimeoutError, OSError) as exc:
                # Network / timeout errors: retry
                last_exc = exc
                delay = _DEFAULT_BASE_DELAY * (2**attempt)
                logger.warning(
                    "LLM call failed (attempt %d/%d), retrying in %.1fs: %s",
                    attempt + 1,
                    self._max_retries,
                    delay,
                    exc,
                )
                time.sleep(delay)

        raise BackendError(
            f"LLM call failed after {self._max_retries} retries: {last_exc}",
            backend="llm",
        )

    # ------------------------------------------------------------------
    # LLM call — direct generator iteration (no DT_STREAM)
    # ------------------------------------------------------------------

    def _call_llm(
        self,
    ) -> tuple[str, list[dict[str, Any]], dict[str, Any] | None]:
        """Call LLM and iterate CC-format frames. Fully sync.

        Iterates generate_streaming() directly — no DT_STREAM, no queue.
        Observer callbacks fire in real-time as tokens arrive.

        Returns:
            (response_text, tool_calls_in_openai_format, metadata_dict)
        """
        request: dict[str, Any] = {"messages": self._messages}
        if self._model:
            request["model"] = self._model
        if self._tools:
            request["tools"] = self._tools

        tokens: list[str] = []
        thinking_parts: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        usage: dict[str, Any] = {}
        stop_reason = "stop"
        model: str | None = None

        for frame in self._llm_backend.generate_streaming(request):
            t = frame["type"]

            if t == "text":
                tokens.append(frame["text"])
                self._observer.observe_update(
                    "agent_message_chunk",
                    {"content": {"type": "text", "text": frame["text"]}},
                )
            elif t == "thinking":
                thinking_parts.append(frame["thinking"])
                self._observer.observe_update(
                    "thinking",
                    {"content": frame["thinking"]},
                )
            elif t == "tool_use":
                # Convert CC tool_use frame to OpenAI tool_call format
                # for internal message storage and tool execution.
                tool_calls.append(
                    {
                        "id": frame["id"],
                        "type": "function",
                        "function": {
                            "name": frame["name"],
                            "arguments": json.dumps(frame["input"]),
                        },
                    }
                )
            elif t == "usage":
                usage = frame["usage"]
                self._observer.observe_update("usage_update", {"usage": usage})
            elif t == "stop":
                stop_reason = frame["stop_reason"]
            elif t == "error":
                raise BackendError(frame.get("message", "LLM error"), backend="llm")

        response_text = "".join(tokens)
        thinking = "".join(thinking_parts) if thinking_parts else None

        return (
            response_text,
            tool_calls,
            {
                "usage": usage,
                "stop_reason": stop_reason,
                "thinking": thinking,
                "model": model,
            },
        )

    # ------------------------------------------------------------------
    # Tool execution via ToolRegistry (VFS syscalls under the hood)
    # ------------------------------------------------------------------

    async def _execute_tool(self, tool_call: dict[str, Any]) -> str:
        """Execute a tool call via ToolRegistry.

        ALL built-in tool I/O goes through VFS syscalls — observable
        via kernel dispatch (PRE → INTERCEPT → OBSERVE).

        Falls back to legacy hardcoded dispatch when no ToolRegistry is set
        (backward compatibility with existing callers).
        """
        if self._tool_registry:
            return await self._tool_registry.execute_one(tool_call)

        # Legacy fallback: hardcoded read_file / write_file
        func = tool_call.get("function", {})
        name = func.get("name", "")
        try:
            args = json.loads(func.get("arguments", "{}"))
        except (json.JSONDecodeError, TypeError):
            return json.dumps({"error": f"Invalid arguments for tool {name}"})

        try:
            if name == "read_file":
                path = args.get("path", "")
                data = self._sys_read(path)
                return data.decode("utf-8", errors="replace")

            elif name == "write_file":
                path = args.get("path", "")
                content = args.get("content", "")
                self._sys_write(path, content.encode("utf-8"))
                return json.dumps({"status": "ok", "path": path})

            else:
                return json.dumps({"error": f"Unknown tool: {name}"})

        except Exception as exc:
            logger.error("Tool execution failed: %s(%s): %s", name, args, exc)
            return json.dumps({"error": str(exc)})

    # ------------------------------------------------------------------
    # Conversation persistence via VFS (CAS-addressed)
    # ------------------------------------------------------------------

    def _persist_conversation(self) -> None:
        """Persist current conversation to VFS (CAS-addressed)."""
        conv_bytes = json.dumps(self._messages, separators=(",", ":"), ensure_ascii=False).encode(
            "utf-8"
        )
        self._sys_write(self._conv_path, conv_bytes)

    def _persist_result(self, result: AgentTurnResult) -> None:
        """Persist turn result to proc filesystem via VFS."""
        result_data = {
            "text": result.text,
            "stop_reason": result.stop_reason,
            "model": result.model,
            "usage": result.usage,
            "num_turns": result.num_turns,
            "session_id": self._session_id,
        }
        result_bytes = json.dumps(result_data, separators=(",", ":")).encode("utf-8")
        try:
            self._sys_write(f"{self._proc_path}/result", result_bytes)
        except Exception:
            logger.debug("Could not persist result to %s/result", self._proc_path)

    async def load_conversation(self) -> None:
        """Resume conversation from VFS (CAS-addressed)."""
        try:
            conv_bytes = self._sys_read(self._conv_path)
            self._messages = json.loads(conv_bytes)
        except Exception:
            logger.debug("No conversation to resume at %s", self._conv_path)

    # ------------------------------------------------------------------
    # Session management
    # ------------------------------------------------------------------

    async def reset(self) -> None:
        """Reset conversation — re-initialize from VFS config."""
        self._messages.clear()
        self._observer = AgentObserver()
        self._session_id = str(uuid.uuid4())
        await self.initialize()
