"""Tests for agent state event system (Issue #1274).

Tests emitter handler calls, exception isolation, and handler management.
"""

import pytest

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.system_services.scheduler.events import AgentStateEmitter, AgentStateEvent


def _make_event(**kwargs) -> AgentStateEvent:
    defaults = {
        "agent_id": "agent-1",
        "previous_state": "IDLE",
        "new_state": "CONNECTED",
        "generation": 1,
        "zone_id": ROOT_ZONE_ID,
    }
    defaults.update(kwargs)
    return AgentStateEvent(**defaults)


class TestAgentStateEvent:
    """Test event dataclass."""

    def test_frozen(self):
        event = _make_event()
        with pytest.raises(AttributeError):
            event.agent_id = "changed"  # type: ignore[misc]

    def test_fields(self):
        event = _make_event(agent_id="x", new_state="SUSPENDED")
        assert event.agent_id == "x"
        assert event.new_state == "SUSPENDED"


class TestAgentStateEmitter:
    """Test emitter handler management and dispatch."""

    @pytest.mark.asyncio
    async def test_handler_called(self):
        emitter = AgentStateEmitter()
        received = []

        async def handler(event: AgentStateEvent) -> None:
            received.append(event)

        emitter.add_handler(handler)
        event = _make_event()
        await emitter.emit(event)

        assert len(received) == 1
        assert received[0] is event

    @pytest.mark.asyncio
    async def test_multiple_handlers(self):
        emitter = AgentStateEmitter()
        calls: list[str] = []

        async def handler_a(event: AgentStateEvent) -> None:
            calls.append("a")

        async def handler_b(event: AgentStateEvent) -> None:
            calls.append("b")

        emitter.add_handler(handler_a)
        emitter.add_handler(handler_b)
        await emitter.emit(_make_event())

        assert calls == ["a", "b"]

    @pytest.mark.asyncio
    async def test_exception_isolation(self):
        """A failing handler should not prevent other handlers from running."""
        emitter = AgentStateEmitter()
        calls: list[str] = []

        async def bad_handler(event: AgentStateEvent) -> None:
            raise RuntimeError("boom")

        async def good_handler(event: AgentStateEvent) -> None:
            calls.append("ok")

        emitter.add_handler(bad_handler)
        emitter.add_handler(good_handler)
        await emitter.emit(_make_event())

        assert calls == ["ok"]

    @pytest.mark.asyncio
    async def test_remove_handler(self):
        emitter = AgentStateEmitter()
        calls: list[str] = []

        async def handler(event: AgentStateEvent) -> None:
            calls.append("called")

        emitter.add_handler(handler)
        emitter.remove_handler(handler)
        await emitter.emit(_make_event())

        assert calls == []

    def test_handler_count(self):
        emitter = AgentStateEmitter()

        async def handler(event: AgentStateEvent) -> None:
            pass

        assert emitter.handler_count == 0
        emitter.add_handler(handler)
        assert emitter.handler_count == 1
        emitter.remove_handler(handler)
        assert emitter.handler_count == 0

    def test_remove_nonexistent_handler_no_error(self):
        emitter = AgentStateEmitter()

        async def handler(event: AgentStateEvent) -> None:
            pass

        emitter.remove_handler(handler)  # Should not raise
