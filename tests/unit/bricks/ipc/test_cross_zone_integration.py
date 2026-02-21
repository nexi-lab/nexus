"""Integration tests for cross-zone IPC with full roundtrip.

Two in-memory zones, testing message send + receive + processing
across zone boundaries.

Issue: #1727
"""

from typing import Any

import pytest

from nexus.bricks.ipc.conventions import dead_letter_path, inbox_path
from nexus.bricks.ipc.delivery import MessageProcessor, MessageSender
from nexus.bricks.ipc.envelope import MessageEnvelope, MessageType
from nexus.bricks.ipc.storage.cross_zone_driver import CrossZoneStorageDriver
from nexus.services.protocols.agent_registry import AgentInfo
from tests.unit.bricks.ipc.fakes import (
    InMemoryEventPublisher,
    InMemoryHotPathPublisher,
    InMemoryStorageDriver,
)

# ── Shared fakes ───────────────────────────────────────────────────────


class FakeAgentRegistry:
    """In-memory agent registry for integration tests."""

    def __init__(self) -> None:
        self._agents: dict[str, AgentInfo] = {}

    def add(self, agent_id: str, zone_id: str) -> None:
        self._agents[agent_id] = AgentInfo(
            agent_id=agent_id,
            owner_id="owner",
            zone_id=zone_id,
            name=agent_id,
            state="CONNECTED",
            generation=1,
        )

    async def register(self, agent_id: str, owner_id: str, **kw: Any) -> AgentInfo:
        info = AgentInfo(
            agent_id=agent_id,
            owner_id=owner_id,
            zone_id=kw.get("zone_id"),
            name=kw.get("name"),
            state="CONNECTED",
            generation=1,
        )
        self._agents[agent_id] = info
        return info

    async def get(self, agent_id: str) -> AgentInfo | None:
        return self._agents.get(agent_id)

    async def transition(self, agent_id: str, target_state: str, **kw: Any) -> AgentInfo:
        raise NotImplementedError

    async def heartbeat(self, agent_id: str) -> None:
        pass

    async def list_by_zone(self, zone_id: str) -> list[AgentInfo]:
        return [a for a in self._agents.values() if a.zone_id == zone_id]

    async def unregister(self, agent_id: str) -> bool:
        return self._agents.pop(agent_id, None) is not None


class FakePermissionChecker:
    """Allows all by default, can be configured to deny."""

    def __init__(self, *, allow_all: bool = True) -> None:
        self._allow_all = allow_all

    async def rebac_check(self, subject: Any, permission: str, object: Any, **kw: Any) -> bool:
        return self._allow_all

    async def rebac_check_batch(self, checks: list, **kw: Any) -> list[bool]:
        return [self._allow_all] * len(checks)

    async def rebac_create(self, *a: Any, **kw: Any) -> dict[str, Any]:
        return {}

    async def rebac_delete(self, tuple_id: str) -> bool:
        return True

    async def rebac_expand(self, *a: Any, **kw: Any) -> list[tuple[str, str]]:
        return []

    async def rebac_list_tuples(self, *a: Any, **kw: Any) -> list[dict[str, Any]]:
        return []


async def _provision_agent(storage: InMemoryStorageDriver, agent_id: str, zone_id: str) -> None:
    """Set up IPC directory structure for an agent in the given zone."""
    for subdir in ("inbox", "outbox", "processed", "dead_letter"):
        await storage.mkdir(f"/agents/{agent_id}/{subdir}", zone_id)


@pytest.fixture()
async def two_zone_env():
    """Build two-zone environment with cross-zone drivers for both directions."""
    storage = InMemoryStorageDriver()
    registry = FakeAgentRegistry()
    publisher = InMemoryHotPathPublisher()
    event_pub = InMemoryEventPublisher()
    permissions = FakePermissionChecker(allow_all=True)

    zone_a = "zone-a"
    zone_b = "zone-b"

    registry.add("alice", zone_a)
    registry.add("bob", zone_b)

    await _provision_agent(storage, "alice", zone_a)
    await _provision_agent(storage, "bob", zone_b)

    # Cross-zone driver from zone-a's perspective
    driver_a = CrossZoneStorageDriver(
        inner=storage,
        agent_registry=registry,
        local_zone_id=zone_a,
        permission_checker=permissions,
        hot_publisher=publisher,
    )

    # Cross-zone driver from zone-b's perspective
    driver_b = CrossZoneStorageDriver(
        inner=storage,
        agent_registry=registry,
        local_zone_id=zone_b,
        permission_checker=permissions,
        hot_publisher=publisher,
    )

    return {
        "storage": storage,
        "registry": registry,
        "publisher": publisher,
        "event_pub": event_pub,
        "permissions": permissions,
        "driver_a": driver_a,
        "driver_b": driver_b,
        "zone_a": zone_a,
        "zone_b": zone_b,
    }


# ── Tests ──────────────────────────────────────────────────────────────


class TestCrossZoneIntegration:
    """Full roundtrip integration tests across two zones."""

    @pytest.mark.asyncio
    async def test_roundtrip_zone_a_to_zone_b(self, two_zone_env: dict) -> None:
        """Alice (zone-a) sends to Bob (zone-b): Bob processes the message."""
        env = two_zone_env
        storage = env["storage"]

        # Alice sends from zone-a
        sender = MessageSender(env["driver_a"], env["event_pub"], zone_id=env["zone_a"])
        msg = MessageEnvelope(
            sender="alice",
            recipient="bob",
            type=MessageType.TASK,
            payload={"task": "review PR #42"},
        )
        await sender.send(msg)

        # Bob processes in zone-b
        received: list[MessageEnvelope] = []

        async def handler(m: MessageEnvelope) -> None:
            received.append(m)

        processor = MessageProcessor(storage, "bob", handler, zone_id=env["zone_b"])
        count = await processor.process_inbox()

        assert count == 1
        assert len(received) == 1
        assert received[0].payload["task"] == "review PR #42"
        assert received[0].sender == "alice"

    @pytest.mark.asyncio
    async def test_roundtrip_response_zone_b_to_zone_a(self, two_zone_env: dict) -> None:
        """Bidirectional: Bob (zone-b) responds to Alice (zone-a)."""
        env = two_zone_env

        # Bob sends from zone-b to Alice in zone-a
        sender_b = MessageSender(env["driver_b"], env["event_pub"], zone_id=env["zone_b"])
        msg = MessageEnvelope(
            sender="bob",
            recipient="alice",
            type=MessageType.RESPONSE,
            correlation_id="task_42",
            payload={"result": "LGTM"},
        )
        await sender_b.send(msg)

        # Alice processes in zone-a
        received: list[MessageEnvelope] = []

        async def handler(m: MessageEnvelope) -> None:
            received.append(m)

        processor = MessageProcessor(env["storage"], "alice", handler, zone_id=env["zone_a"])
        count = await processor.process_inbox()

        assert count == 1
        assert received[0].payload["result"] == "LGTM"
        assert received[0].type == MessageType.RESPONSE

    @pytest.mark.asyncio
    async def test_cross_zone_dead_letter_on_handler_failure(self, two_zone_env: dict) -> None:
        """Handler fails processing a cross-zone message → dead_letter in target zone."""
        env = two_zone_env
        storage = env["storage"]

        # Alice sends to Bob
        sender = MessageSender(env["driver_a"], env["event_pub"], zone_id=env["zone_a"])
        msg = MessageEnvelope(
            sender="alice",
            recipient="bob",
            type=MessageType.TASK,
            payload={"action": "crash"},
        )
        await sender.send(msg)

        # Bob's handler fails
        async def failing_handler(m: MessageEnvelope) -> None:
            raise RuntimeError("Handler exploded")

        processor = MessageProcessor(storage, "bob", failing_handler, zone_id=env["zone_b"])
        await processor.process_inbox()

        # Inbox should be empty (only .routing sidecar may remain), dead_letter has the message
        inbox_files = await storage.list_dir(inbox_path("bob"), env["zone_b"])
        msg_files = [f for f in inbox_files if f.endswith(".json")]
        dl_files = await storage.list_dir(dead_letter_path("bob"), env["zone_b"])
        dl_msg_files = [
            f for f in dl_files if f.endswith(".json") and not f.endswith(".reason.json")
        ]
        assert len(msg_files) == 0
        assert len(dl_msg_files) == 1

    @pytest.mark.asyncio
    async def test_cross_zone_discovery_via_registry(self, two_zone_env: dict) -> None:
        """Find agents in other zones via AgentRegistryProtocol."""
        registry = two_zone_env["registry"]

        # Discover bob's zone
        bob = await registry.get("bob")
        assert bob is not None
        assert bob.zone_id == "zone-b"

        # List agents in zone-b
        zone_b_agents = await registry.list_by_zone("zone-b")
        assert len(zone_b_agents) == 1
        assert zone_b_agents[0].agent_id == "bob"

    @pytest.mark.asyncio
    async def test_cross_zone_permission_traversal(self, two_zone_env: dict) -> None:
        """ReBAC check enforced across zone boundary — denied blocks delivery."""
        env = two_zone_env
        permissions = env["permissions"]
        permissions._allow_all = False

        sender = MessageSender(env["driver_a"], env["event_pub"], zone_id=env["zone_a"])
        msg = MessageEnvelope(
            sender="alice",
            recipient="bob",
            type=MessageType.TASK,
            payload={"action": "forbidden"},
        )

        from nexus.bricks.ipc.exceptions import CrossZoneDeliveryError

        with pytest.raises(CrossZoneDeliveryError) as exc_info:
            await sender.send(msg)

        assert exc_info.value.reason.value == "permission_denied"
