"""AcpConnection — ACP JSON-RPC 2.0 protocol adapter.

Extends ``AgentLoop`` (generic JSON-RPC over PipeBackend) with ACP-specific
request/notification dispatch: permission auto-grant, VFS-backed file I/O,
session management, and usage/chunk accumulation.

AcpConnection is a pure protocol adapter — it owns no subprocess.
Subprocess lifecycle is managed by ``AcpService``.

File I/O routing (``everything is a file``):
    When *fs_read* / *fs_write* callables are provided (backed by
    ``NexusFS.sys_read`` / ``sys_write``), all ``fs/read_text_file``
    and ``fs/write_text_file`` requests from the agent are routed
    through the VFS syscall layer, enabling ReBAC enforcement, audit
    logging, and federation-aware reads.  When no callables are
    supplied, the connection falls back to host-native ``open()``
    for backward compatibility.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from nexus.system_services.agent_loop import AgentLoop, AgentRpcError

logger = logging.getLogger(__name__)

# Type aliases for VFS-backed file I/O callables.
FsReadFn = Callable[[str], Awaitable[str]]
FsWriteFn = Callable[[str, str], Awaitable[None]]

# Backwards-compat alias — service.py imports AcpRpcError from here.
AcpRpcError = AgentRpcError


@dataclass
class AcpPromptResult:
    """Structured result from a single ACP session/prompt call."""

    text: str
    stop_reason: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    session_id: str | None = None
    model: str | None = None
    accumulated_usage: dict[str, Any] = field(default_factory=dict)


class AcpConnection(AgentLoop):
    """ACP JSON-RPC 2.0 protocol adapter over PipeBackend.

    Extends AgentLoop with ACP-specific dispatch:
    - ``session/request_permission`` → auto-grant
    - ``fs/read_text_file`` / ``fs/write_text_file`` → VFS or host fallback
    - ``session/update`` notifications → usage/chunk accumulation
    """

    def __init__(
        self,
        *,
        stdin_pipe: Any,
        stdout_pipe: Any,
        stderr_reader: asyncio.StreamReader | None = None,
        cwd: str | None = None,
        fs_read: FsReadFn | None = None,
        fs_write: FsWriteFn | None = None,
    ) -> None:
        super().__init__(
            stdin_pipe=stdin_pipe,
            stdout_pipe=stdout_pipe,
            stderr_reader=stderr_reader,
            cwd=cwd,
        )
        self._session_id: str | None = None
        self._accumulated_usage: dict[str, Any] = {}
        self._accumulated_text: list[str] = []
        self._num_turns: int = 0
        self._model_name: str | None = None
        self._load_session: bool = False
        self._prompt_active: bool = False

        # VFS-backed file I/O callables (``everything is a file``).
        self._fs_read = fs_read
        self._fs_write = fs_write

    # ------------------------------------------------------------------
    # ACP lifecycle
    # ------------------------------------------------------------------

    async def initialize(self, timeout: float = 30.0) -> dict[str, Any]:
        """Send ``initialize`` request (protocolVersion 1)."""
        result = await self._request(
            "initialize",
            {
                "protocolVersion": 1,
                "clientCapabilities": {
                    "fs": {
                        "readTextFile": True,
                        "writeTextFile": True,
                    },
                },
            },
            timeout=timeout,
        )
        # Cache capabilities for later checks
        caps = result.get("agentCapabilities", {})
        self._load_session = bool(caps.get("loadSession"))
        return dict(result)

    @property
    def supports_load_session(self) -> bool:
        """Whether the agent advertised ``loadSession`` capability."""
        return self._load_session

    async def session_new(self, cwd: str | None = None, timeout: float = 30.0) -> str:
        """Send ``session/new`` and store the returned sessionId."""
        resolved_cwd = os.path.abspath(cwd or self._cwd or os.getcwd())
        result = await self._request(
            "session/new",
            {
                "cwd": resolved_cwd,
                "mcpServers": [],
            },
            timeout=timeout,
        )
        self._session_id = result.get("sessionId")

        # Extract model name from session/new response
        self._extract_model(result)
        return self._session_id or ""

    async def session_load(
        self, session_id: str, cwd: str | None = None, timeout: float = 30.0
    ) -> str:
        """Send ``session/load`` to resume a previous session.

        This is the stable ACP spec method.  The agent replays prior
        conversation history via ``session/update`` notifications before
        returning.  Requires ``loadSession`` capability from ``initialize``.
        """
        resolved_cwd = os.path.abspath(cwd or self._cwd or os.getcwd())
        result = await self._request(
            "session/load",
            {"sessionId": session_id, "cwd": resolved_cwd, "mcpServers": []},
            timeout=timeout,
        )
        # session/load returns null per spec; keep the requested ID
        if isinstance(result, dict):
            self._session_id = result.get("sessionId") or session_id
        else:
            self._session_id = session_id

        # Extract model name (same logic as session_new)
        if isinstance(result, dict):
            self._extract_model(result)

        # Drain buffered replay notifications: session/load may return its
        # JSON-RPC result before all replay notifications have been read from
        # stdout.  Yielding to the event loop lets the reader task process
        # any lines already buffered, plus a brief sleep allows the subprocess
        # to flush remaining replay output.
        await asyncio.sleep(0.2)

        # Clear text/usage accumulated from history replay notifications
        # so send_prompt starts with a clean slate.
        self._accumulated_text.clear()
        self._accumulated_usage.clear()
        self._num_turns = 0

        return self._session_id or ""

    async def send_prompt(self, prompt: str, timeout: float = 300.0) -> AcpPromptResult:
        """Send ``session/prompt`` and return the structured result.

        The response text is accumulated from ``agent_message_chunk``
        notifications that arrive *during* the prompt (the prompt response
        itself only contains ``stopReason`` and ``usage``).
        """
        # Reset per-prompt accumulators and enable chunk accumulation.
        # The _prompt_active gate ensures only chunks from this prompt
        # are collected — late replay notifications from session/load
        # are silently discarded.
        self._accumulated_text.clear()
        self._prompt_active = True

        try:
            result = await self._request(
                "session/prompt",
                {
                    "sessionId": self._session_id,
                    "prompt": [{"type": "text", "text": prompt}],
                },
                timeout=timeout,
            )
        finally:
            self._prompt_active = False

        # Text comes from agent_message_chunk notifications
        text = "".join(self._accumulated_text)

        # Model: prompt result > accumulated usage > session/new
        model = (
            result.get("model") or self._accumulated_usage.pop("model", None) or self._model_name
        )

        return AcpPromptResult(
            text=text,
            stop_reason=result.get("stopReason"),
            usage=result.get("usage", {}),
            session_id=self._session_id,
            model=model,
            accumulated_usage=dict(self._accumulated_usage),
        )

    @property
    def num_turns(self) -> int:
        """Number of tool_call turns observed via session/update."""
        return self._num_turns

    # ------------------------------------------------------------------
    # AgentLoop abstract — ACP dispatch
    # ------------------------------------------------------------------

    def _handle_request(self, msg: dict[str, Any]) -> None:
        """Handle incoming requests from the agent subprocess."""
        method = msg["method"]
        msg_id = msg["id"]
        params = msg.get("params", {})

        if method == "session/request_permission":
            self._respond(
                msg_id,
                {
                    "outcome": {"outcome": "selected", "optionId": "allow_once"},
                },
            )
        elif method in ("fs/read_text_file", "fs/write_text_file"):
            # File I/O may involve async VFS syscalls — dispatch as task.
            asyncio.create_task(
                self._handle_fs_request(method, msg_id, params),
                name=f"acp-fs-{method}",
            )
        else:
            logger.debug("ACP: unhandled agent request: %s", method)
            self._respond_error(msg_id, f"Method not found: {method}", code=-32601)

    def _handle_notification(self, msg: dict[str, Any]) -> None:
        """Handle incoming notifications (no response needed)."""
        method = msg.get("method")
        params = msg.get("params", {})

        if method == "session/update":
            update = params.get("update", {})
            update_type = update.get("sessionUpdate")

            if update_type == "usage_update":
                # Accumulate usage data
                usage = update.get("usage", {})
                for key, val in usage.items():
                    if isinstance(val, (int, float)):
                        self._accumulated_usage[key] = self._accumulated_usage.get(key, 0) + val
                    else:
                        self._accumulated_usage[key] = val

            elif update_type == "tool_call":
                self._num_turns += 1

            elif update_type == "user_message_chunk":
                # A user_message_chunk during an active prompt means history
                # replay is still in progress (the agent is echoing prior
                # conversation turns).  Clear accumulators so only text
                # from the actual model response survives.
                if self._prompt_active:
                    self._accumulated_text.clear()

            elif update_type == "agent_message_chunk":
                # Only accumulate chunks during an active prompt — discard
                # replay notifications from session/load.
                if self._prompt_active:
                    content = update.get("content", {})
                    if content.get("type") == "text":
                        self._accumulated_text.append(content.get("text", ""))

            else:
                logger.debug("ACP: session/update type=%s (ignored)", update_type)

        else:
            logger.debug("ACP: unhandled notification: %s", method)

    # ------------------------------------------------------------------
    # File I/O — routes through VFS when callables are available
    # ------------------------------------------------------------------

    def _resolve_path(self, file_path: str) -> str:
        """Resolve a file path relative to cwd."""
        if not os.path.isabs(file_path) and self._cwd:
            file_path = os.path.join(self._cwd, file_path)
        return file_path

    async def _handle_fs_request(
        self,
        method: str,
        msg_id: int | str,
        params: dict[str, Any],
    ) -> None:
        """Handle fs/read_text_file and fs/write_text_file.

        Routes through VFS syscalls when ``fs_read`` / ``fs_write``
        callables are provided; falls back to host-native ``open()``
        otherwise.
        """
        try:
            if method == "fs/read_text_file":
                path = self._resolve_path(params.get("path", ""))
                if self._fs_read is not None:
                    content = await self._fs_read(path)
                else:
                    with open(path, encoding="utf-8", errors="replace") as f:
                        content = f.read()
                self._respond(msg_id, {"content": content})

            elif method == "fs/write_text_file":
                path = self._resolve_path(params.get("path", ""))
                content = params.get("content", "")
                if self._fs_write is not None:
                    await self._fs_write(path, content)
                else:
                    os.makedirs(os.path.dirname(path), exist_ok=True)
                    with open(path, "w", encoding="utf-8") as f:
                        f.write(content)
                self._respond(msg_id, None)

        except Exception as exc:
            self._respond_error(msg_id, str(exc))

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _extract_model(self, result: dict[str, Any]) -> None:
        """Extract model name from session/new or session/load response."""
        models = result.get("models", {})
        current_id = models.get("currentModelId")
        if current_id:
            for m in models.get("availableModels", []):
                if m.get("modelId") == current_id:
                    desc = m.get("description", "")
                    self._model_name = (
                        desc.split(" · ")[0] if " · " in desc else m.get("name", current_id)
                    )
                    break
            else:
                self._model_name = current_id
