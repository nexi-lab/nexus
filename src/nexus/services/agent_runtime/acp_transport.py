"""ACP JSON-RPC transport over stdin/stdout (§4A.1).

Newline-delimited JSON-RPC 2.0 — same protocol as Claude Code
(--experimental-acp), Codex (--acp), Goose (acp), etc.

Each message is one JSON object per line on stdin/stdout.
Requests have numeric `id` fields; notifications do not.

References:
    - sudowork: src/agent/acp/AcpConnection.ts
    - sudowork: src/agent/acp/utils.ts (writeJsonRpcMessage)
    - JSON-RPC 2.0 spec: https://www.jsonrpc.org/specification
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from typing import Any

logger = logging.getLogger(__name__)

JSONRPC_VERSION = "2.0"


class AcpTransport:
    """ACP JSON-RPC transport over stdin/stdout.

    Read from stdin (async readline), write to stdout (sync, newline-delimited).
    Thread-safe for writes (stdout.write is atomic for small messages).
    """

    def __init__(self) -> None:
        self._reader: asyncio.StreamReader | None = None
        self._next_request_id = 0
        self._pending_requests: dict[int, asyncio.Future[dict[str, Any]]] = {}

    async def start(self) -> None:
        """Initialize async stdin reader."""
        loop = asyncio.get_running_loop()
        reader = asyncio.StreamReader()
        self._reader = reader
        await loop.connect_read_pipe(
            lambda: asyncio.StreamReaderProtocol(reader),
            sys.stdin,
        )

    async def read_message(self) -> dict[str, Any] | None:
        """Read one JSON-RPC message from stdin.

        Returns None on EOF.
        """
        assert self._reader is not None, "call start() first"
        try:
            line = await self._reader.readline()
        except (asyncio.CancelledError, asyncio.IncompleteReadError):
            return None
        if not line:
            return None
        try:
            result: dict[str, Any] = json.loads(line)
            return result
        except json.JSONDecodeError:
            logger.warning("ACP: invalid JSON on stdin: %s", line[:200])
            return None

    def write_message(self, msg: dict[str, Any]) -> None:
        """Write one JSON-RPC message to stdout (newline-delimited)."""
        data = json.dumps(msg, separators=(",", ":"), ensure_ascii=False)
        sys.stdout.write(data + "\n")
        sys.stdout.flush()

    def send_response(
        self, request_id: int, result: Any = None, error: dict[str, Any] | None = None
    ) -> None:
        """Send a JSON-RPC response to a request."""
        msg: dict[str, Any] = {"jsonrpc": JSONRPC_VERSION, "id": request_id}
        if error is not None:
            msg["error"] = error
        else:
            msg["result"] = result
        self.write_message(msg)

    def send_notification(self, method: str, params: dict[str, Any] | None = None) -> None:
        """Send a JSON-RPC notification (no response expected)."""
        msg: dict[str, Any] = {"jsonrpc": JSONRPC_VERSION, "method": method}
        if params is not None:
            msg["params"] = params
        self.write_message(msg)

    async def send_request(
        self, method: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Send a JSON-RPC request and await response.

        Used for bidirectional requests like session/request_permission
        where nexus asks sudowork for user approval.
        """
        request_id = self._next_request_id
        self._next_request_id += 1

        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending_requests[request_id] = future

        msg: dict[str, Any] = {
            "jsonrpc": JSONRPC_VERSION,
            "id": request_id,
            "method": method,
        }
        if params is not None:
            msg["params"] = params
        self.write_message(msg)

        return await future

    def handle_response(self, msg: dict[str, Any]) -> bool:
        """Handle an incoming response to a previous request.

        Returns True if the message was a response (consumed), False otherwise.
        """
        msg_id = msg.get("id")
        if msg_id is not None and msg_id in self._pending_requests:
            future = self._pending_requests.pop(msg_id)
            if "error" in msg:
                future.set_exception(RuntimeError(msg["error"].get("message", "ACP error")))
            else:
                future.set_result(msg.get("result", {}))
            return True
        return False

    # ------------------------------------------------------------------
    # session/update helpers (§4A.3)
    # ------------------------------------------------------------------

    def emit_session_update(self, session_id: str, update: dict[str, Any]) -> None:
        """Emit a session/update notification."""
        self.send_notification(
            "session/update",
            {"sessionId": session_id, "update": update},
        )

    def emit_agent_message_chunk(self, session_id: str, text: str) -> None:
        """Emit a text token chunk."""
        self.emit_session_update(
            session_id,
            {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": text},
            },
        )

    def emit_tool_call(
        self,
        session_id: str,
        tool_call_id: str,
        title: str,
        status: str = "pending",
        kind: str = "execute",
        raw_input: dict[str, Any] | None = None,
    ) -> None:
        """Emit a tool_call status update."""
        update: dict[str, Any] = {
            "sessionUpdate": "tool_call",
            "toolCallId": tool_call_id,
            "status": status,
            "title": title,
            "kind": kind,
        }
        if raw_input is not None:
            update["rawInput"] = raw_input
        self.emit_session_update(session_id, update)

    def emit_tool_call_update(
        self,
        session_id: str,
        tool_call_id: str,
        status: str,
        content: str | None = None,
    ) -> None:
        """Emit a tool_call_update (status transition)."""
        update: dict[str, Any] = {
            "sessionUpdate": "tool_call_update",
            "toolCallId": tool_call_id,
            "status": status,
        }
        if content is not None:
            update["content"] = [{"type": "content", "content": {"type": "text", "text": content}}]
        self.emit_session_update(session_id, update)

    def emit_usage_update(
        self,
        session_id: str,
        used: int = 0,
        size: int = 0,
    ) -> None:
        """Emit a usage_update notification."""
        self.emit_session_update(
            session_id,
            {
                "sessionUpdate": "usage_update",
                "used": used,
                "size": size,
            },
        )
