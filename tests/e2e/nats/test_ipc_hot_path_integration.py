"""Integration tests for hot-path IPC delivery with a real NATS server.

Requires: nats-server running (JetStream not required — core pub/sub only).
Start with:
  docker run -d --name nats-test -p 4222:4222 nats:2.10-alpine

Set NEXUS_NATS_URL to override the default nats://localhost:4222.

Related: Issue #1747 (LEGO 17.7)
"""

from __future__ import annotations

import asyncio
import os

import pytest

from nexus.bricks.ipc.delivery import DeliveryMode, MessageProcessor, MessageSender
from nexus.bricks.ipc.envelope import MessageEnvelope, MessageType
from nexus.bricks.ipc.nats_adapter import NatsHotPathAdapter
from nexus.bricks.ipc.provisioning import AgentProvisioner

NATS_URL = os.environ.get("NEXUS_NATS_URL", "nats://localhost:4222")
ZONE = "e2e-hot-path-zone"


def _is_nats_available() -> bool:
    """Check if NATS server is reachable."""
    import socket

    try:
        url = NATS_URL.replace("nats://", "")
        host, port_str = url.split(":")
        sock = socket.create_connection((host, int(port_str)), timeout=2)
        sock.close()
        return True
    except (OSError, ValueError):
        return False


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not _is_nats_available(), reason="NATS not available"),
    pytest.mark.xdist_group("nats"),
]


# --- In-memory VFS for the cold path (reuse from unit tests) ---


class SimpleInMemoryVFS:
    """Minimal VFS for e2e tests (no zone isolation needed)."""

    def __init__(self) -> None:
        self._files: dict[tuple[str, str], bytes] = {}
        self._dirs: set[tuple[str, str]] = set()

    async def read(self, path: str, zone_id: str) -> bytes:
        key = (path, zone_id)
        if key not in self._files:
            raise FileNotFoundError(path)
        return self._files[key]

    async def write(self, path: str, data: bytes, zone_id: str) -> None:
        self._files[(path, zone_id)] = data

    async def list_dir(self, path: str, zone_id: str) -> list[str]:
        if (path, zone_id) not in self._dirs:
            raise FileNotFoundError(path)
        prefix = path.rstrip("/") + "/"
        results: list[str] = []
        for (fpath, fzone), _ in self._files.items():
            if fzone == zone_id and fpath.startswith(prefix):
                rest = fpath[len(prefix) :]
                if "/" not in rest:
                    results.append(rest)
        for dpath, dzone in self._dirs:
            if dzone == zone_id and dpath.startswith(prefix):
                rest = dpath[len(prefix) :]
                if "/" not in rest and rest:
                    results.append(rest)
        return sorted(set(results))

    async def rename(self, src: str, dst: str, zone_id: str) -> None:
        key = (src, zone_id)
        if key not in self._files:
            raise FileNotFoundError(src)
        self._files[(dst, zone_id)] = self._files.pop(key)

    async def mkdir(self, path: str, zone_id: str) -> None:
        self._dirs.add((path, zone_id))
        parts = path.strip("/").split("/")
        for i in range(1, len(parts)):
            self._dirs.add(("/" + "/".join(parts[:i]), zone_id))

    async def count_dir(self, path: str, zone_id: str) -> int:
        return len(await self.list_dir(path, zone_id))

    async def exists(self, path: str, zone_id: str) -> bool:
        return (path, zone_id) in self._files or (path, zone_id) in self._dirs


@pytest.fixture
async def nats_client():
    """Create and connect a NATS client for testing."""
    import nats

    nc = await nats.connect(NATS_URL)
    yield nc
    await nc.drain()


class TestIPCHotPathIntegration:
    """Integration tests for hot-path IPC with real NATS."""

    @pytest.mark.asyncio
    async def test_hot_cold_roundtrip(self, nats_client) -> None:
        """Send via HOT_COLD, verify hot delivery + cold persistence."""
        adapter = NatsHotPathAdapter(nats_client)
        vfs = SimpleInMemoryVFS()
        provisioner = AgentProvisioner(vfs, zone_id=ZONE)
        await provisioner.provision("agent:sender")
        await provisioner.provision("agent:receiver")

        received: list[MessageEnvelope] = []
        received_event = asyncio.Event()

        async def handler(msg: MessageEnvelope) -> None:
            received.append(msg)
            received_event.set()

        # Set up processor with hot subscriber
        processor = MessageProcessor(
            vfs, "agent:receiver", handler, zone_id=ZONE, hot_subscriber=adapter
        )
        await processor.start()

        # Allow subscription to establish
        await asyncio.sleep(0.1)

        # Send message via HOT_COLD
        sender = MessageSender(
            vfs,
            zone_id=ZONE,
            hot_publisher=adapter,
            delivery_mode=DeliveryMode.HOT_COLD,
        )
        env = MessageEnvelope(
            sender="agent:sender",
            recipient="agent:receiver",
            type=MessageType.TASK,
            payload={"action": "e2e_test"},
        )
        path = await sender.send(env)
        assert path.startswith("hot://")

        # Wait for hot delivery
        await asyncio.wait_for(received_event.wait(), timeout=5.0)
        assert len(received) == 1
        assert received[0].id == env.id
        assert received[0].payload == {"action": "e2e_test"}

        # Wait for cold persistence
        await sender.drain()

        await processor.stop()
