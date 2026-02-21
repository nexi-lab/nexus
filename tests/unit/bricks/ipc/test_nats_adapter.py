"""Unit tests for NatsHotPathAdapter (#11)."""

from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, MagicMock

import pytest

from nexus.bricks.ipc.nats_adapter import NatsHotPathAdapter


class FakeNatsMsg:
    """Simulates a nats.aio.client.Msg."""

    def __init__(self, data: bytes) -> None:
        self.data = data


class FakeSubscription:
    """Simulates nats.aio.client.Subscription with async message iterator."""

    def __init__(self, messages_data: list[bytes]) -> None:
        self._messages_data = messages_data

    @property
    def messages(self) -> AsyncIterator[FakeNatsMsg]:
        return self._aiter()

    async def _aiter(self) -> AsyncIterator[FakeNatsMsg]:
        for data in self._messages_data:
            yield FakeNatsMsg(data)


class TestNatsHotPathAdapter:
    """Tests for the NATS adapter's publish and subscribe wiring."""

    @pytest.mark.asyncio
    async def test_publish_delegates_to_nats_client(self) -> None:
        nc = MagicMock()
        nc.publish = AsyncMock()

        adapter = NatsHotPathAdapter(nc)
        await adapter.publish("agents.bob.inbox", b'{"test": true}')

        nc.publish.assert_awaited_once_with("agents.bob.inbox", b'{"test": true}')

    @pytest.mark.asyncio
    async def test_subscribe_yields_raw_message_data(self) -> None:
        nc = MagicMock()
        sub = FakeSubscription([b"msg1", b"msg2", b"msg3"])
        nc.subscribe = AsyncMock(return_value=sub)

        adapter = NatsHotPathAdapter(nc)
        received: list[bytes] = []

        async for raw in adapter.subscribe("agents.bob.inbox"):
            received.append(raw)
            if len(received) == 3:
                break

        assert received == [b"msg1", b"msg2", b"msg3"]
        nc.subscribe.assert_awaited_once_with("agents.bob.inbox")

    @pytest.mark.asyncio
    async def test_publish_propagates_nats_errors(self) -> None:
        nc = MagicMock()
        nc.publish = AsyncMock(side_effect=ConnectionError("NATS down"))

        adapter = NatsHotPathAdapter(nc)

        with pytest.raises(ConnectionError, match="NATS down"):
            await adapter.publish("agents.bob.inbox", b"payload")
