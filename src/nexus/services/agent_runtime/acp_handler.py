"""ACP Protocol Handler — bridges sudowork ↔ ManagedAgentLoop (§4A.2).

Handles JSON-RPC methods from sudowork (initialize, session/new,
session/prompt) and drives ManagedAgentLoop accordingly. Streams
tool calls and text chunks back as session/update notifications.

Usage:
    handler = AcpProtocolHandler(transport=transport, **loop_kwargs)
    await handler.run()  # blocks until stdin EOF or shutdown

References:
    - §4A.1: AcpTransport (JSON-RPC I/O)
    - §4A.3: Push-mode observer bridge
    - sudowork: src/agent/acp/AcpConnection.ts
"""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING, Any

from nexus.services.agent_runtime.acp_transport import AcpTransport
from nexus.services.agent_runtime.observer import AgentObserver

if TYPE_CHECKING:
    from nexus.services.agent_runtime.managed_loop import ManagedAgentLoop

logger = logging.getLogger(__name__)

PROTOCOL_VERSION = 1


class AcpProtocolHandler:
    """ACP protocol handler — bridges sudowork JSON-RPC ↔ ManagedAgentLoop.

    Lifecycle:
        1. sudowork spawns `nexus chat --acp`
        2. sudowork sends `initialize` → nexus responds with capabilities
        3. sudowork sends `session/new` → nexus creates ManagedAgentLoop
        4. sudowork sends `session/prompt` → nexus runs loop, streams updates
        5. Repeat step 4 for multi-turn conversation
        6. stdin EOF → nexus exits
    """

    def __init__(
        self,
        transport: AcpTransport,
        *,
        loop_factory: Any,
    ) -> None:
        self._transport = transport
        self._loop_factory = loop_factory
        self._loop: ManagedAgentLoop | None = None
        self._session_id: str = ""
        self._initialized = False

    async def run(self) -> None:
        """Main message loop — read JSON-RPC from stdin, dispatch handlers."""
        await self._transport.start()

        while True:
            msg = await self._transport.read_message()
            if msg is None:
                break  # EOF

            # Check if it's a response to one of our outgoing requests
            if self._transport.handle_response(msg):
                continue

            # It's an incoming request or notification
            method = msg.get("method", "")
            params = msg.get("params", {})
            request_id = msg.get("id")

            try:
                result = await self._dispatch(method, params)
                if request_id is not None:
                    self._transport.send_response(request_id, result)
            except Exception as exc:
                logger.error("ACP handler error: %s %s", method, exc)
                if request_id is not None:
                    self._transport.send_response(
                        request_id,
                        error={"code": -32603, "message": str(exc)},
                    )

    async def _dispatch(self, method: str, params: dict[str, Any]) -> Any:
        """Dispatch a JSON-RPC method to the appropriate handler."""
        if method == "initialize":
            return self._handle_initialize(params)
        if method == "session/new":
            return await self._handle_session_new(params)
        if method == "session/prompt":
            return await self._handle_session_prompt(params)
        if method == "session/set_model":
            return self._handle_set_model(params)
        logger.debug("ACP: unhandled method %s", method)
        return None

    # ------------------------------------------------------------------
    # Protocol handlers
    # ------------------------------------------------------------------

    def _handle_initialize(self, _params: dict[str, Any]) -> dict[str, Any]:
        """Handle `initialize` — return protocol version + capabilities."""
        self._initialized = True
        return {
            "protocolVersion": PROTOCOL_VERSION,
            "serverCapabilities": {
                "streaming": True,
                "toolExecution": True,
            },
        }

    async def _handle_session_new(self, params: dict[str, Any]) -> dict[str, Any]:
        """Handle `session/new` — create ManagedAgentLoop, return session info."""
        cwd = params.get("cwd", "")
        self._session_id = str(uuid.uuid4())

        # Create push-mode observer that emits ACP notifications
        observer = AgentObserver(
            on_update=self._make_update_callback(self._session_id),
        )

        # Create the loop via factory (chat.py provides the factory)
        self._loop = await self._loop_factory(
            session_id=self._session_id,
            cwd=cwd,
            observer=observer,
        )

        model = getattr(self._loop, "_model", None) or "unknown"

        return {
            "sessionId": self._session_id,
            "models": {
                "currentModelId": model,
                "availableModels": [{"id": model, "name": model}],
            },
        }

    async def _handle_session_prompt(self, params: dict[str, Any]) -> dict[str, Any]:
        """Handle `session/prompt` — run agent loop, stream updates.

        Sudowork sends prompt content as array of content blocks.
        We extract text and run the loop. Updates stream via observer callback.
        """
        if self._loop is None:
            raise RuntimeError("No active session — call session/new first")

        # Extract text from prompt content blocks
        prompt_parts = params.get("prompt", [])
        text_parts = []
        for part in prompt_parts:
            if isinstance(part, dict) and part.get("type") == "text":
                text_parts.append(part.get("text", ""))
            elif isinstance(part, str):
                text_parts.append(part)
        prompt = "\n".join(text_parts) if text_parts else str(prompt_parts)

        # Run the agent loop — updates stream via observer callback
        result = await self._loop.run(prompt)

        return {
            "sessionId": self._session_id,
            "text": result.text,
            "stopReason": result.stop_reason,
        }

    def _handle_set_model(self, params: dict[str, Any]) -> dict[str, Any]:
        """Handle `session/set_model` — switch model mid-session."""
        model_id = params.get("modelId", "")
        if self._loop is not None:
            self._loop._model = model_id
        return {"modelId": model_id}

    # ------------------------------------------------------------------
    # Push-mode observer callback (§4A.3)
    # ------------------------------------------------------------------

    def _make_update_callback(self, session_id: str) -> Any:
        """Create an observer callback that emits ACP session/update notifications."""
        transport = self._transport

        def _on_update(update_type: str, update: dict[str, Any]) -> None:
            if update_type == "agent_message_chunk":
                content = update.get("content", {})
                if content.get("type") == "text":
                    transport.emit_agent_message_chunk(session_id, content.get("text", ""))

            elif update_type == "tool_call":
                # §4A.4: emit tool_call with status
                tc = update
                func = tc.get("function", {})
                transport.emit_tool_call(
                    session_id=session_id,
                    tool_call_id=tc.get("id", ""),
                    title=func.get("name", "tool"),
                    status="in_progress",
                    kind=_classify_tool_kind(func.get("name", "")),
                    raw_input=func,
                )

            elif update_type == "tool_call_complete":
                transport.emit_tool_call_update(
                    session_id=session_id,
                    tool_call_id=update.get("tool_call_id", ""),
                    status="completed",
                    content=update.get("content"),
                )

            elif update_type == "tool_call_failed":
                transport.emit_tool_call_update(
                    session_id=session_id,
                    tool_call_id=update.get("tool_call_id", ""),
                    status="failed",
                    content=update.get("error"),
                )

            elif update_type == "usage_update":
                usage = update.get("usage", {})
                transport.emit_usage_update(
                    session_id=session_id,
                    used=usage.get("total_tokens", 0),
                    size=usage.get("max_tokens", 200000),
                )

        return _on_update


def _classify_tool_kind(tool_name: str) -> str:
    """Map tool name to ACP tool kind (read/edit/execute)."""
    if tool_name in ("read_file", "grep", "glob"):
        return "read"
    if tool_name in ("write_file", "edit_file"):
        return "edit"
    return "execute"
