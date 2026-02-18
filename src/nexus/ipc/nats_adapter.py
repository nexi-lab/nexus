"""Thin adapter wrapping nats.aio.client.Client for IPC HotPath protocols.

Keeps the IPC brick decoupled from the NATS library — the adapter
is created at wiring time (factory) and injected into MessageSender
and MessageProcessor.

Issue: #1747 (LEGO 17.7)
"""


from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nats.aio.client import Client as NatsClient


class NatsHotPathAdapter:
    """Adapts ``nats.aio.client.Client`` to IPC HotPath protocols.

    Satisfies both ``HotPathPublisher`` and ``HotPathSubscriber``
    via structural subtyping (Protocol).

    Args:
        nc: A connected NATS client (shared/multiplexed).
    """

    def __init__(self, nc: NatsClient) -> None:
        self._nc = nc

    async def publish(self, subject: str, data: bytes) -> None:
        """Publish *data* to the given NATS *subject*."""
        await self._nc.publish(subject, data)

    async def subscribe(self, subject: str) -> AsyncIterator[bytes]:
        """Subscribe to *subject* and yield raw message payloads."""
        sub = await self._nc.subscribe(subject)
        async for msg in sub.messages:
            yield msg.data
