"""AgentLoop — JSON-RPC 2.0 over PipeBackend base class.

Generic agent subprocess communication loop:  read JSON-RPC from stdout
pipe → dispatch → respond via stdin pipe.  Subclasses implement request
and notification routing; the base class handles response matching,
transport, and lifecycle.

Placement rationale (KERNEL-ARCHITECTURE.md §6 decision tree):
    Single-layer usage (services only) → stays in services layer.
    Imports ``core.pipe`` (kernel primitive) → cannot be ``lib/``.
    Has implementation logic → cannot be ``contracts/``.

    core/pipe.py                  = kernel (PipeBackend protocol)
    core/stdio_pipe.py            = kernel (PipeBackend over OS pipes)
    system_services/agent_loop.py = services (JSON-RPC protocol on top)

Usage::

    class MyAgentConnection(AgentLoop):
        def _handle_request(self, msg): ...
        def _handle_notification(self, msg): ...

See: system_services/acp/connection.py for AcpConnection (first consumer).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from abc import ABC, abstractmethod
from typing import Any

from nexus.core.pipe import PipeBackend, PipeClosedError

logger = logging.getLogger(__name__)


class AgentRpcError(Exception):
    """Error returned by an agent via JSON-RPC error response."""

    def __init__(self, message: str, code: int = -1, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.data = data


class AgentLoop(ABC):
    """JSON-RPC 2.0 client over PipeBackend (stdin/stdout).

    Transport:
        Write: ``json.dumps(msg, separators=(",",":")) + "\\n"`` → stdin pipe
        Read:  ``PipeBackend.read()`` from stdout pipe, one JSON line per message
        Matching: auto-incrementing int IDs + ``dict[int, asyncio.Future]``
    """

    def __init__(
        self,
        *,
        stdin_pipe: PipeBackend,
        stdout_pipe: PipeBackend,
        stderr_pipe: PipeBackend | None = None,
        cwd: str | None = None,
    ) -> None:
        self._stdin = stdin_pipe
        self._stdout = stdout_pipe
        self._stderr = stderr_pipe
        self._cwd = cwd
        self._next_id: int = 1
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._stderr_lines: list[str] = []

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Launch reader and stderr collector tasks."""
        self._reader_task = asyncio.create_task(self._reader_loop(), name="agent-reader")
        if self._stderr is not None:
            self._stderr_task = asyncio.create_task(self._stderr_collector(), name="agent-stderr")

    async def disconnect(self) -> None:
        """Cancel reader tasks and close pipes."""
        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
        if self._stderr_task and not self._stderr_task.done():
            self._stderr_task.cancel()

        with contextlib.suppress(Exception):
            self._stdin.close()
        with contextlib.suppress(Exception):
            self._stdout.close()
        if self._stderr is not None:
            with contextlib.suppress(Exception):
                self._stderr.close()

        # Fail all pending futures
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(ConnectionError("Agent connection closed"))
        self._pending.clear()

    @property
    def stderr_output(self) -> str:
        """Collected stderr output."""
        return "\n".join(self._stderr_lines)

    # ------------------------------------------------------------------
    # JSON-RPC transport
    # ------------------------------------------------------------------

    def _write(self, msg: dict[str, Any]) -> None:
        """Write a JSON-RPC message to stdin pipe (fire-and-forget)."""
        line = json.dumps(msg, separators=(",", ":")) + "\n"
        self._stdin.write_nowait(line.encode())

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

        # Write with drain (blocking=True flushes OS buffer).
        line = json.dumps(msg, separators=(",", ":")) + "\n"
        await self._stdin.write(line.encode())

        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except TimeoutError:
            self._pending.pop(msg_id, None)
            raise

    def _respond(self, msg_id: int | str, result: Any) -> None:
        """Send a JSON-RPC response to an incoming request."""
        self._write({"jsonrpc": "2.0", "id": msg_id, "result": result})

    def _respond_error(self, msg_id: int | str, message: str, code: int = -32000) -> None:
        """Send a JSON-RPC error response."""
        self._write(
            {
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {"code": code, "message": message},
            }
        )

    # ------------------------------------------------------------------
    # Reader loop
    # ------------------------------------------------------------------

    async def _reader_loop(self) -> None:
        """Read JSON-RPC messages from stdout pipe."""
        try:
            while True:
                data = await self._stdout.read()

                line_str = data.decode("utf-8", errors="replace").strip()
                if not line_str:
                    continue

                try:
                    msg = json.loads(line_str)
                except json.JSONDecodeError:
                    logger.debug("AgentLoop: non-JSON line from stdout: %s", line_str[:200])
                    continue

                self._dispatch(msg)

        except PipeClosedError:
            pass
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.debug("AgentLoop reader loop error: %s", exc)
        finally:
            # Fail any remaining pending futures
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(ConnectionError("Agent reader loop ended"))

    async def _stderr_collector(self) -> None:
        """Collect stderr output via PipeBackend (DT_PIPE at fd/2)."""
        assert self._stderr is not None
        try:
            while True:
                data = await self._stderr.read()
                text = data.decode("utf-8", errors="replace").rstrip()
                if text:
                    self._stderr_lines.append(text)
                    logger.debug("Agent stderr: %s", text[:500])
        except PipeClosedError:
            pass
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Message dispatch
    # ------------------------------------------------------------------

    def _dispatch(self, msg: dict[str, Any]) -> None:
        """Route an incoming JSON-RPC message.

        Response matching is handled generically.  Requests and
        notifications are delegated to subclass abstract methods.
        """
        # Response to our request
        if "id" in msg and ("result" in msg or "error" in msg):
            msg_id = msg["id"]
            fut = self._pending.pop(msg_id, None)
            if fut is None or fut.done():
                return
            if "error" in msg:
                fut.set_exception(
                    AgentRpcError(
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
            self._handle_request(msg)
        else:
            self._handle_notification(msg)

    # ------------------------------------------------------------------
    # Abstract — subclass routing
    # ------------------------------------------------------------------

    @abstractmethod
    def _handle_request(self, msg: dict[str, Any]) -> None:
        """Handle an incoming JSON-RPC request from the agent.

        The subclass must route by ``msg["method"]`` and call
        ``_respond()`` or ``_respond_error()`` to reply.
        """

    @abstractmethod
    def _handle_notification(self, msg: dict[str, Any]) -> None:
        """Handle an incoming JSON-RPC notification (no response)."""
