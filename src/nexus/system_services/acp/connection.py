"""AcpConnection — JSON-RPC 2.0 client over subprocess stdio.

Implements the ACP (Agent Communication Protocol) as a bidirectional
JSON-RPC 2.0 channel over a subprocess's stdin/stdout.  The connection
handles the full lifecycle: spawn → initialize → session/new →
session/prompt → disconnect.

Incoming requests from the agent (permission requests, file reads/writes)
are auto-handled.  Session update notifications are accumulated for
metadata extraction.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class AcpPromptResult:
    """Structured result from a single ACP session/prompt call."""

    text: str
    stop_reason: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    session_id: str | None = None
    model: str | None = None
    accumulated_usage: dict[str, Any] = field(default_factory=dict)


class AcpConnection:
    """JSON-RPC 2.0 client over subprocess stdin/stdout.

    Transport:
        Write: ``json.dumps(msg, separators=(",",":")) + "\\n"`` → stdin
        Read:  async line-by-line from stdout, ``json.loads()`` per line
        Matching: auto-incrementing int IDs + ``dict[int, asyncio.Future]``
    """

    def __init__(self) -> None:
        self._proc: asyncio.subprocess.Process | None = None
        self._next_id: int = 1
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._session_id: str | None = None
        self._cwd: str | None = None
        self._accumulated_usage: dict[str, Any] = {}
        self._accumulated_text: list[str] = []
        self._num_turns: int = 0
        self._stderr_lines: list[str] = []
        self._model_name: str | None = None
        self._load_session: bool = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def spawn(
        self,
        cmd: list[str],
        cwd: str | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        """Spawn the agent subprocess and start reader loops."""
        self._cwd = cwd or os.getcwd()
        self._proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self._cwd,
            env=env,
        )
        self._reader_task = asyncio.create_task(self._reader_loop(), name="acp-reader")
        self._stderr_task = asyncio.create_task(self._stderr_collector(), name="acp-stderr")

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
        models = result.get("models", {})
        current_id = models.get("currentModelId")
        if current_id:
            for m in models.get("availableModels", []):
                if m.get("modelId") == current_id:
                    # Use description (e.g. "Opus 4.6 · Most capable...") or name
                    desc = m.get("description", "")
                    # Extract model name before " · "
                    self._model_name = (
                        desc.split(" · ")[0] if " · " in desc else m.get("name", current_id)
                    )
                    break
            else:
                self._model_name = current_id
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
        return self._session_id or ""

    async def send_prompt(self, prompt: str, timeout: float = 300.0) -> AcpPromptResult:
        """Send ``session/prompt`` and return the structured result.

        The response text is accumulated from ``agent_message_chunk``
        notifications that arrive *during* the prompt (the prompt response
        itself only contains ``stopReason`` and ``usage``).
        """
        # Reset per-prompt accumulators
        self._accumulated_text.clear()

        result = await self._request(
            "session/prompt",
            {
                "sessionId": self._session_id,
                "prompt": [{"type": "text", "text": prompt}],
            },
            timeout=timeout,
        )

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

    async def disconnect(self) -> None:
        """Cancel reader tasks, close stdin, kill the subprocess."""
        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
        if self._stderr_task and not self._stderr_task.done():
            self._stderr_task.cancel()

        if self._proc is not None:
            if self._proc.stdin is not None:
                with contextlib.suppress(Exception):
                    self._proc.stdin.close()

            if self._proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    self._proc.kill()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._proc.wait(), timeout=5.0)

        # Fail all pending futures
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(ConnectionError("ACP connection closed"))
        self._pending.clear()

    @property
    def returncode(self) -> int | None:
        """Return subprocess exit code (None if still running)."""
        return self._proc.returncode if self._proc else None

    @property
    def stderr_output(self) -> str:
        """Collected stderr output."""
        return "\n".join(self._stderr_lines)

    @property
    def num_turns(self) -> int:
        """Number of tool_call turns observed via session/update."""
        return self._num_turns

    # ------------------------------------------------------------------
    # JSON-RPC transport
    # ------------------------------------------------------------------

    def _write(self, msg: dict[str, Any]) -> None:
        """Write a JSON-RPC message to subprocess stdin."""
        assert self._proc is not None and self._proc.stdin is not None
        line = json.dumps(msg, separators=(",", ":")) + "\n"
        self._proc.stdin.write(line.encode())

    async def _request(self, method: str, params: dict[str, Any], *, timeout: float = 30.0) -> Any:
        """Send a JSON-RPC request and await the response."""
        msg_id = self._next_id
        self._next_id += 1

        msg = {
            "jsonrpc": "2.0",
            "id": msg_id,
            "method": method,
            "params": params,
        }

        loop = asyncio.get_running_loop()
        fut: asyncio.Future[Any] = loop.create_future()
        self._pending[msg_id] = fut

        self._write(msg)
        assert self._proc is not None and self._proc.stdin is not None
        await self._proc.stdin.drain()

        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except TimeoutError:
            self._pending.pop(msg_id, None)
            raise

    def _respond(self, msg_id: int | str, result: Any) -> None:
        """Send a JSON-RPC response to an incoming request."""
        msg = {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": result,
        }
        self._write(msg)
        # Best-effort drain — fire and forget for synchronous callers
        if (
            self._proc is not None
            and self._proc.stdin is not None
            and not self._proc.stdin.is_closing()
        ):
            asyncio.ensure_future(self._proc.stdin.drain())

    # ------------------------------------------------------------------
    # Reader loop
    # ------------------------------------------------------------------

    async def _reader_loop(self) -> None:
        """Read JSON-RPC messages from stdout line-by-line."""
        assert self._proc is not None and self._proc.stdout is not None
        try:
            while True:
                line = await self._proc.stdout.readline()
                if not line:
                    break  # EOF

                line_str = line.decode("utf-8", errors="replace").strip()
                if not line_str:
                    continue

                try:
                    msg = json.loads(line_str)
                except json.JSONDecodeError:
                    logger.debug("ACP: non-JSON line from stdout: %s", line_str[:200])
                    continue

                self._dispatch(msg)

        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.debug("ACP reader loop error: %s", exc)
        finally:
            # Fail any remaining pending futures
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(ConnectionError("ACP reader loop ended"))

    async def _stderr_collector(self) -> None:
        """Collect stderr output for diagnostics."""
        assert self._proc is not None and self._proc.stderr is not None
        try:
            while True:
                line = await self._proc.stderr.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").rstrip()
                if text:
                    self._stderr_lines.append(text)
                    logger.debug("ACP stderr: %s", text[:500])
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Message dispatch
    # ------------------------------------------------------------------

    def _dispatch(self, msg: dict[str, Any]) -> None:
        """Route an incoming JSON-RPC message."""
        # Response to our request
        if "id" in msg and ("result" in msg or "error" in msg):
            msg_id = msg["id"]
            fut = self._pending.pop(msg_id, None)
            if fut is None or fut.done():
                return
            if "error" in msg:
                fut.set_exception(
                    AcpRpcError(
                        msg["error"].get("message", "Unknown RPC error"),
                        code=msg["error"].get("code", -1),
                        data=msg["error"].get("data"),
                    )
                )
            else:
                fut.set_result(msg.get("result"))
            return

        # Notification (no id) or request from agent (has id, has method)
        method = msg.get("method")
        if method is None:
            return

        if "id" in msg:
            # Incoming request from agent → handle and respond
            self._handle_agent_request(msg)
        else:
            # Notification
            self._handle_notification(msg)

    def _handle_agent_request(self, msg: dict[str, Any]) -> None:
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
        elif method == "fs/read_text_file":
            self._handle_fs_read(msg_id, params)
        elif method == "fs/write_text_file":
            self._handle_fs_write(msg_id, params)
        else:
            logger.debug("ACP: unhandled agent request: %s", method)
            # Respond with method-not-found
            error_msg = {
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {"code": -32601, "message": f"Method not found: {method}"},
            }
            self._write(error_msg)
            if (
                self._proc is not None
                and self._proc.stdin is not None
                and not self._proc.stdin.is_closing()
            ):
                asyncio.ensure_future(self._proc.stdin.drain())

    def _handle_fs_read(self, msg_id: int | str, params: dict[str, Any]) -> None:
        """Handle fs/read_text_file: read file from cwd and respond."""
        file_path = params.get("path", "")
        try:
            # Resolve relative to cwd
            if not os.path.isabs(file_path) and self._cwd:
                file_path = os.path.join(self._cwd, file_path)
            with open(file_path, encoding="utf-8", errors="replace") as f:
                content = f.read()
            self._respond(msg_id, {"content": content})
        except Exception as exc:
            error_msg = {
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {"code": -32000, "message": str(exc)},
            }
            self._write(error_msg)
            if (
                self._proc is not None
                and self._proc.stdin is not None
                and not self._proc.stdin.is_closing()
            ):
                asyncio.ensure_future(self._proc.stdin.drain())

    def _handle_fs_write(self, msg_id: int | str, params: dict[str, Any]) -> None:
        """Handle fs/write_text_file: write file and respond with null."""
        file_path = params.get("path", "")
        content = params.get("content", "")
        try:
            if not os.path.isabs(file_path) and self._cwd:
                file_path = os.path.join(self._cwd, file_path)
            os.makedirs(os.path.dirname(file_path), exist_ok=True)
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(content)
            self._respond(msg_id, None)
        except Exception as exc:
            error_msg = {
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {"code": -32000, "message": str(exc)},
            }
            self._write(error_msg)
            if (
                self._proc is not None
                and self._proc.stdin is not None
                and not self._proc.stdin.is_closing()
            ):
                asyncio.ensure_future(self._proc.stdin.drain())

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

            elif update_type == "agent_message_chunk":
                # Accumulate text chunks — final text comes from here
                content = update.get("content", {})
                if content.get("type") == "text":
                    self._accumulated_text.append(content.get("text", ""))

            else:
                logger.debug("ACP: session/update type=%s (ignored)", update_type)

        else:
            logger.debug("ACP: unhandled notification: %s", method)


class AcpRpcError(Exception):
    """Error returned by the agent via JSON-RPC error response."""

    def __init__(self, message: str, code: int = -1, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.data = data
