"""ACP protocol smoke tests — verify JSON-RPC handshake without real LLM.

Tests the AcpTransport + AcpProtocolHandler can handle the
initialize → session/new → session/prompt sequence that sudowork sends.

Uses a mock ManagedAgentLoop to avoid LLM dependency.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from nexus.services.agent_runtime.acp_handler import AcpProtocolHandler
from nexus.services.agent_runtime.observer import AgentObserver, AgentTurnResult

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class FakeTransport:
    """In-memory ACP transport for testing (replaces stdin/stdout)."""

    def __init__(self) -> None:
        self._inbox: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self._outbox: list[dict[str, Any]] = []
        self._pending_requests: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._next_request_id = 0

    async def start(self) -> None:
        pass

    async def read_message(self) -> dict[str, Any] | None:
        return await self._inbox.get()

    def write_message(self, msg: dict[str, Any]) -> None:
        self._outbox.append(msg)

    def send_response(
        self, request_id: int, result: Any = None, error: dict[str, Any] | None = None
    ) -> None:
        msg: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id}
        if error is not None:
            msg["error"] = error
        else:
            msg["result"] = result
        self.write_message(msg)

    def send_notification(self, method: str, params: dict[str, Any] | None = None) -> None:
        msg: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        self.write_message(msg)

    def handle_response(self, msg: dict[str, Any]) -> bool:
        return False  # No outgoing requests in tests

    def emit_session_update(self, session_id: str, update: dict[str, Any]) -> None:
        self.send_notification("session/update", {"sessionId": session_id, "update": update})

    def emit_agent_message_chunk(self, session_id: str, text: str) -> None:
        self.emit_session_update(
            session_id,
            {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": text}},
        )

    def emit_tool_call(self, **kwargs: Any) -> None:
        pass

    def emit_tool_call_update(self, **kwargs: Any) -> None:
        pass

    def emit_usage_update(self, **kwargs: Any) -> None:
        pass

    # Test helpers

    def inject(self, msg: dict[str, Any]) -> None:
        """Inject a message as if from sudowork."""
        self._inbox.put_nowait(msg)

    def inject_eof(self) -> None:
        """Signal end of input."""
        self._inbox.put_nowait(None)

    def get_responses(self) -> list[dict[str, Any]]:
        return list(self._outbox)

    def get_response_for_id(self, request_id: int) -> dict[str, Any] | None:
        for msg in self._outbox:
            if msg.get("id") == request_id:
                return msg
        return None


def _make_mock_loop() -> MagicMock:
    """Create a mock ManagedAgentLoop."""
    loop = MagicMock()
    loop._model = "test-model"
    loop._observer = AgentObserver()

    async def mock_run(prompt: str) -> AgentTurnResult:
        return AgentTurnResult(text=f"Echo: {prompt}", stop_reason="stop", model="test-model")

    loop.run = mock_run
    loop.initialize = AsyncMock()
    return loop


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestAcpInitialize:
    @pytest.mark.asyncio
    async def test_initialize_returns_protocol_version(self) -> None:
        transport = FakeTransport()

        async def _factory(session_id: str, cwd: str, observer: AgentObserver) -> Any:
            return _make_mock_loop()

        handler = AcpProtocolHandler(transport=transport, loop_factory=_factory)

        # Inject initialize + EOF
        transport.inject({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}})
        transport.inject_eof()

        await handler.run()

        resp = transport.get_response_for_id(0)
        assert resp is not None
        assert resp["result"]["protocolVersion"] == 1


class TestAcpSessionNew:
    @pytest.mark.asyncio
    async def test_session_new_returns_session_id(self) -> None:
        transport = FakeTransport()

        created_loops: list[Any] = []

        async def _factory(session_id: str, cwd: str, observer: AgentObserver) -> Any:
            loop = _make_mock_loop()
            loop._observer = observer
            created_loops.append(loop)
            return loop

        handler = AcpProtocolHandler(transport=transport, loop_factory=_factory)

        transport.inject({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}})
        transport.inject(
            {"jsonrpc": "2.0", "id": 1, "method": "session/new", "params": {"cwd": "/tmp"}}
        )
        transport.inject_eof()

        await handler.run()

        resp = transport.get_response_for_id(1)
        assert resp is not None
        assert "sessionId" in resp["result"]
        assert resp["result"]["models"]["currentModelId"] == "test-model"
        assert len(created_loops) == 1


class TestAcpSessionPrompt:
    @pytest.mark.asyncio
    async def test_prompt_returns_text(self) -> None:
        transport = FakeTransport()

        async def _factory(session_id: str, cwd: str, observer: AgentObserver) -> Any:
            loop = _make_mock_loop()
            loop._observer = observer
            return loop

        handler = AcpProtocolHandler(transport=transport, loop_factory=_factory)

        transport.inject({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}})
        transport.inject(
            {"jsonrpc": "2.0", "id": 1, "method": "session/new", "params": {"cwd": "/tmp"}}
        )
        transport.inject(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "session/prompt",
                "params": {
                    "sessionId": "test",
                    "prompt": [{"type": "text", "text": "hello world"}],
                },
            }
        )
        transport.inject_eof()

        await handler.run()

        resp = transport.get_response_for_id(2)
        assert resp is not None
        assert "Echo: hello world" in resp["result"]["text"]

    @pytest.mark.asyncio
    async def test_prompt_without_session_returns_error(self) -> None:
        transport = FakeTransport()

        async def _factory(session_id: str, cwd: str, observer: AgentObserver) -> Any:
            return _make_mock_loop()

        handler = AcpProtocolHandler(transport=transport, loop_factory=_factory)

        transport.inject({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}})
        # Skip session/new, go directly to prompt
        transport.inject(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "session/prompt",
                "params": {"prompt": [{"type": "text", "text": "hi"}]},
            }
        )
        transport.inject_eof()

        await handler.run()

        resp = transport.get_response_for_id(1)
        assert resp is not None
        assert "error" in resp


class TestAcpObserverPush:
    @pytest.mark.asyncio
    async def test_observer_push_emits_notifications(self) -> None:
        """When loop.run() emits observer updates, they should appear as
        session/update notifications in the transport output."""
        transport = FakeTransport()

        async def _factory(session_id: str, cwd: str, observer: AgentObserver) -> Any:
            loop = _make_mock_loop()
            loop._observer = observer

            # Override run to emit observer updates before returning
            async def mock_run_with_updates(prompt: str) -> AgentTurnResult:
                observer.reset_turn()
                observer.observe_update(
                    "agent_message_chunk",
                    {"content": {"type": "text", "text": "streaming token"}},
                )
                return AgentTurnResult(text="streaming token", stop_reason="stop")

            loop.run = mock_run_with_updates
            return loop

        handler = AcpProtocolHandler(transport=transport, loop_factory=_factory)

        transport.inject({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}})
        transport.inject(
            {"jsonrpc": "2.0", "id": 1, "method": "session/new", "params": {"cwd": "/tmp"}}
        )
        transport.inject(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "session/prompt",
                "params": {"prompt": [{"type": "text", "text": "test"}]},
            }
        )
        transport.inject_eof()

        await handler.run()

        # Find session/update notifications
        notifications = [
            m for m in transport.get_responses() if m.get("method") == "session/update"
        ]
        assert len(notifications) >= 1
        chunk = notifications[0]["params"]["update"]
        assert chunk["sessionUpdate"] == "agent_message_chunk"
        assert chunk["content"]["text"] == "streaming token"
