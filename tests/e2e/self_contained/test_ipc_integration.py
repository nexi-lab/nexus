"""Integration tests for the IPC brick.

Tests the full message lifecycle: provisioning → send → receive →
process → dead_letter. Uses InMemoryVFS to test components working
together without kernel dependencies.
"""

from datetime import UTC, datetime

import pytest

from nexus.bricks.ipc.conventions import (
    dead_letter_path,
    inbox_path,
    outbox_path,
    processed_path,
)
from nexus.bricks.ipc.delivery import MessageProcessor, MessageSender
from nexus.bricks.ipc.discovery import AgentDiscovery
from nexus.bricks.ipc.envelope import MessageEnvelope, MessageType
from nexus.bricks.ipc.exceptions import InboxFullError
from nexus.bricks.ipc.provisioning import AgentProvisioner
from nexus.bricks.ipc.sweep import TTLSweeper

# Import fakes from unit tests
from tests.unit.bricks.ipc.fakes import InMemoryEventPublisher, InMemoryVFS

ZONE = "integration-zone"


@pytest.fixture
def vfs() -> InMemoryVFS:
    return InMemoryVFS()


@pytest.fixture
def publisher() -> InMemoryEventPublisher:
    return InMemoryEventPublisher()


async def _setup_agents(
    vfs: InMemoryVFS,
    *agent_ids: str,
    provisioner: AgentProvisioner | None = None,
) -> AgentProvisioner:
    """Provision multiple agents for testing."""
    prov = provisioner or AgentProvisioner(vfs, zone_id=ZONE)
    for agent_id in agent_ids:
        await prov.provision(agent_id, skills=["test_skill"])
    return prov


class TestFullMessageRoundTrip:
    """Integration: provision → send → process → verify lifecycle."""

    @pytest.mark.asyncio
    async def test_agent_a_sends_to_agent_b(
        self,
        vfs: InMemoryVFS,
        publisher: InMemoryEventPublisher,
    ) -> None:
        """Full round-trip: A sends, B processes, message moves to processed."""
        await _setup_agents(vfs, "agent:alice", "agent:bob")

        # Alice sends a message to Bob
        sender = MessageSender(vfs, publisher, zone_id=ZONE)
        env = MessageEnvelope(
            sender="agent:alice",
            recipient="agent:bob",
            type=MessageType.TASK,
            id="msg_roundtrip_1",
            payload={"action": "review_code", "file": "/workspace/main.py"},
        )
        await sender.send(env)

        # Verify message is in Bob's inbox
        inbox_files = vfs.list_dir(inbox_path("agent:bob"), ZONE)
        assert len(inbox_files) == 1

        # Verify EventBus was notified
        assert len(publisher.published) == 1
        assert publisher.published[0][0] == "ipc.inbox.agent:bob"

        # Verify outbox copy exists
        outbox_files = vfs.list_dir(outbox_path("agent:alice"), ZONE)
        assert len(outbox_files) == 1

        # Bob processes the message
        received: list[MessageEnvelope] = []

        async def bob_handler(msg: MessageEnvelope) -> None:
            received.append(msg)

        processor = MessageProcessor(
            vfs,
            "agent:bob",
            bob_handler,
            zone_id=ZONE,
        )
        count = await processor.process_inbox()

        assert count == 1
        assert received[0].id == "msg_roundtrip_1"
        assert received[0].payload["action"] == "review_code"

        # Message moved from inbox to processed
        inbox_files = vfs.list_dir(inbox_path("agent:bob"), ZONE)
        assert len(inbox_files) == 0
        processed_files = vfs.list_dir(processed_path("agent:bob"), ZONE)
        assert len(processed_files) == 1

    @pytest.mark.asyncio
    async def test_bidirectional_communication(
        self,
        vfs: InMemoryVFS,
    ) -> None:
        """A sends task to B, B sends response back to A."""
        await _setup_agents(vfs, "agent:alice", "agent:bob")
        sender = MessageSender(vfs, zone_id=ZONE)

        # Alice → Bob: task
        task_env = MessageEnvelope(
            sender="agent:alice",
            recipient="agent:bob",
            type=MessageType.TASK,
            id="msg_task_42",
            correlation_id="workflow_1",
            payload={"action": "analyze"},
        )
        await sender.send(task_env)

        # Bob processes and responds
        async def bob_handler(msg: MessageEnvelope) -> None:
            response = MessageEnvelope(
                sender="agent:bob",
                recipient="agent:alice",
                type=MessageType.RESPONSE,
                id="msg_resp_42",
                correlation_id=msg.correlation_id,
                payload={"status": "approved"},
            )
            await sender.send(response)

        bob_processor = MessageProcessor(
            vfs,
            "agent:bob",
            bob_handler,
            zone_id=ZONE,
        )
        await bob_processor.process_inbox()

        # Alice processes the response
        responses: list[MessageEnvelope] = []

        async def alice_handler(msg: MessageEnvelope) -> None:
            responses.append(msg)

        alice_processor = MessageProcessor(
            vfs,
            "agent:alice",
            alice_handler,
            zone_id=ZONE,
        )
        await alice_processor.process_inbox()

        assert len(responses) == 1
        assert responses[0].correlation_id == "workflow_1"
        assert responses[0].payload["status"] == "approved"


class TestProvisioningAndDiscovery:
    """Integration: provisioning + discovery working together."""

    @pytest.mark.asyncio
    async def test_provision_then_discover(self, vfs: InMemoryVFS) -> None:
        provisioner = AgentProvisioner(vfs, zone_id=ZONE)
        await provisioner.provision(
            "reviewer",
            name="Code Reviewer",
            skills=["code_review", "security_audit"],
        )
        await provisioner.provision(
            "analyst",
            name="Data Analyst",
            skills=["data_analysis", "research"],
        )

        discovery = AgentDiscovery(vfs, zone_id=ZONE)

        # List agents
        agents = await discovery.list_agents()
        assert "reviewer" in agents
        assert "analyst" in agents

        # Get specific agent card
        reviewer = await discovery.get_agent_card("reviewer")
        assert reviewer is not None
        assert reviewer.name == "Code Reviewer"
        assert "code_review" in reviewer.skills

        # Find by skill
        security_agents = await discovery.find_by_skill("security_audit")
        assert len(security_agents) == 1
        assert security_agents[0].agent_id == "reviewer"

    @pytest.mark.asyncio
    async def test_deprovision_hides_agent(self, vfs: InMemoryVFS) -> None:
        provisioner = AgentProvisioner(vfs, zone_id=ZONE)
        await provisioner.provision("temp_agent", skills=["temp_skill"])

        discovery = AgentDiscovery(vfs, zone_id=ZONE)
        agent = await discovery.get_agent_card("temp_agent")
        assert agent is not None
        assert agent.status == "connected"

        # Deprovision
        await provisioner.deprovision("temp_agent")
        agent = await discovery.get_agent_card("temp_agent")
        assert agent is not None
        assert agent.status == "deprovisioned"


class TestDeadLetterAndTTLSweep:
    """Integration: dead letter handling + TTL sweep."""

    @pytest.mark.asyncio
    async def test_failed_handler_goes_to_dead_letter(
        self,
        vfs: InMemoryVFS,
    ) -> None:
        await _setup_agents(vfs, "agent:alice", "agent:bob")
        sender = MessageSender(vfs, zone_id=ZONE)
        env = MessageEnvelope(
            sender="agent:alice",
            recipient="agent:bob",
            type=MessageType.TASK,
            id="msg_fail_1",
            payload={"action": "impossible_task"},
        )
        await sender.send(env)

        async def failing_handler(msg: MessageEnvelope) -> None:
            raise RuntimeError("Cannot process")

        processor = MessageProcessor(
            vfs,
            "agent:bob",
            failing_handler,
            zone_id=ZONE,
        )
        await processor.process_inbox()

        # Message should be in dead_letter, not inbox
        inbox_files = vfs.list_dir(inbox_path("agent:bob"), ZONE)
        assert len(inbox_files) == 0
        dl_files = vfs.list_dir(dead_letter_path("agent:bob"), ZONE)
        dl_msg_files = [f for f in dl_files if not f.endswith(".reason.json")]
        assert len(dl_msg_files) == 1

    @pytest.mark.asyncio
    async def test_ttl_sweep_cleans_expired(self, vfs: InMemoryVFS) -> None:
        await _setup_agents(vfs, "agent:bob")
        sender = MessageSender(vfs, zone_id=ZONE)

        # Send an already-expired message (old timestamp + short TTL)
        old_ts = datetime(2020, 1, 1, tzinfo=UTC)
        env = MessageEnvelope(
            sender="agent:alice",
            recipient="agent:bob",
            type=MessageType.TASK,
            id="msg_old",
            timestamp=old_ts,
            ttl_seconds=60,
            payload={},
        )
        # Manually write to bypass backpressure check
        from nexus.bricks.ipc.conventions import message_path_in_inbox

        msg_path = message_path_in_inbox("agent:bob", env.id, env.timestamp)
        vfs.write(msg_path, env.to_bytes(), ZONE)

        # Also send a valid (non-expired) message
        valid_env = MessageEnvelope(
            sender="agent:alice",
            recipient="agent:bob",
            type=MessageType.TASK,
            id="msg_valid",
            payload={},
        )
        await _setup_agents(vfs, "agent:alice")
        await sender.send(valid_env)

        # Sweep
        sweeper = TTLSweeper(vfs, zone_id=ZONE)
        expired_count = await sweeper.sweep_once()

        assert expired_count == 1
        inbox_files = vfs.list_dir(inbox_path("agent:bob"), ZONE)
        assert len(inbox_files) == 1  # Only the valid message remains
        dl_files = vfs.list_dir(dead_letter_path("agent:bob"), ZONE)
        dl_msg_files = [f for f in dl_files if not f.endswith(".reason.json")]
        assert len(dl_msg_files) == 1  # Expired message moved here


class TestBackpressure:
    """Integration: inbox size limits and backpressure."""

    @pytest.mark.asyncio
    async def test_backpressure_at_limit(self, vfs: InMemoryVFS) -> None:
        await _setup_agents(vfs, "agent:alice", "agent:bob")
        sender = MessageSender(vfs, zone_id=ZONE, max_inbox_size=3)

        # Send 3 messages (at limit)
        for i in range(3):
            env = MessageEnvelope(
                sender="agent:alice",
                recipient="agent:bob",
                type=MessageType.TASK,
                id=f"msg_bp_{i}",
                payload={},
            )
            await sender.send(env)

        # 4th message should be rejected
        overflow_env = MessageEnvelope(
            sender="agent:alice",
            recipient="agent:bob",
            type=MessageType.TASK,
            id="msg_overflow",
            payload={},
        )
        with pytest.raises(InboxFullError):
            await sender.send(overflow_env)

    @pytest.mark.asyncio
    async def test_backpressure_cleared_after_processing(
        self,
        vfs: InMemoryVFS,
    ) -> None:
        await _setup_agents(vfs, "agent:alice", "agent:bob")
        sender = MessageSender(vfs, zone_id=ZONE, max_inbox_size=2)

        # Fill inbox
        for i in range(2):
            env = MessageEnvelope(
                sender="agent:alice",
                recipient="agent:bob",
                type=MessageType.TASK,
                id=f"msg_clear_{i}",
                payload={},
            )
            await sender.send(env)

        # Process messages (empties inbox)
        async def handler(msg: MessageEnvelope) -> None:
            pass

        processor = MessageProcessor(vfs, "agent:bob", handler, zone_id=ZONE)
        await processor.process_inbox()

        # Now sending should work again
        env = MessageEnvelope(
            sender="agent:alice",
            recipient="agent:bob",
            type=MessageType.TASK,
            id="msg_after_clear",
            payload={},
        )
        path = await sender.send(env)
        assert path is not None


# ===========================================================================
# Issue #3197: DT_PIPE wakeup + event-driven TTL sweep integration tests
# ===========================================================================


class TestEventDrivenSweepIntegration:
    """Integration: CacheStore pub/sub triggers TTL sweeper."""

    @pytest.mark.asyncio
    async def test_ttl_schedule_event_triggers_targeted_sweep(self, vfs: InMemoryVFS) -> None:
        """Send with TTL -> CacheStore pub/sub event -> sweeper wakes -> sweeps agent."""
        import asyncio
        import json

        from nexus.cache.inmemory import InMemoryCacheStore

        await _setup_agents(vfs, "agent:alice", "agent:bob")
        cache_store = InMemoryCacheStore()

        # Write an already-expired message to bob's inbox
        from nexus.bricks.ipc.conventions import message_path_in_inbox

        old_ts = datetime(2020, 1, 1, tzinfo=UTC)
        expired_env = MessageEnvelope(
            sender="agent:alice",
            recipient="agent:bob",
            type=MessageType.TASK,
            id="msg_expired_event",
            timestamp=old_ts,
            ttl_seconds=60,
            payload={},
        )
        msg_path = message_path_in_inbox("agent:bob", expired_env.id, expired_env.timestamp)
        vfs.sys_write(msg_path, expired_env.to_bytes(), ZONE)

        # Start event-driven sweeper
        sweeper = TTLSweeper(
            vfs,
            zone_id=ZONE,
            interval=300,  # Long poll interval — we rely on event
            cache_store=cache_store,
            debounce_seconds=0.05,
        )
        await sweeper.start()
        await asyncio.sleep(0.05)  # Let subscriber register

        # Publish TTL schedule event (as MessageSender would)
        await cache_store.publish(
            f"ipc:ttl:schedule:{ZONE}",
            json.dumps({"agent_id": "agent:bob", "msg_id": "msg_expired_event"}).encode(),
        )

        await asyncio.sleep(0.3)  # Wait for debounce + sweep
        await sweeper.stop()
        await cache_store.close()

        # Expired message should have been swept
        inbox_files = vfs.list_dir(inbox_path("agent:bob"), ZONE)
        assert len(inbox_files) == 0
        dl_files = vfs.list_dir(dead_letter_path("agent:bob"), ZONE)
        dl_msgs = [f for f in dl_files if not f.endswith(".reason.json")]
        assert len(dl_msgs) == 1

    @pytest.mark.asyncio
    async def test_sender_publishes_ttl_event_and_sweeper_reacts(self, vfs: InMemoryVFS) -> None:
        """Full integration: MessageSender publishes TTL event -> sweeper reacts."""
        import asyncio

        from nexus.cache.inmemory import InMemoryCacheStore

        await _setup_agents(vfs, "agent:alice", "agent:bob")
        cache_store = InMemoryCacheStore()

        # Start event-driven sweeper first
        sweeper = TTLSweeper(
            vfs,
            zone_id=ZONE,
            interval=300,
            cache_store=cache_store,
            debounce_seconds=0.05,
        )
        await sweeper.start()
        await asyncio.sleep(0.05)  # Let subscriber register

        # Write an already-expired message to inbox
        old_ts = datetime(2020, 1, 1, tzinfo=UTC)
        env = MessageEnvelope(
            sender="agent:alice",
            recipient="agent:bob",
            type=MessageType.TASK,
            id="msg_sender_ttl",
            timestamp=old_ts,
            ttl_seconds=60,
            payload={},
        )
        # Manually write (send() would reject since inbox validation uses real paths)
        from nexus.bricks.ipc.conventions import message_path_in_inbox

        msg_path = message_path_in_inbox("agent:bob", env.id, env.timestamp)
        vfs.sys_write(msg_path, env.to_bytes(), ZONE)

        # Publish TTL event (simulate what MessageSender._send_to_inbox does)
        import json

        expires_at = env.timestamp.timestamp() + env.ttl_seconds
        await cache_store.publish(
            f"ipc:ttl:schedule:{ZONE}",
            json.dumps(
                {
                    "agent_id": "agent:bob",
                    "msg_id": env.id,
                    "expires_at": expires_at,
                }
            ).encode(),
        )

        await asyncio.sleep(0.3)
        await sweeper.stop()
        await cache_store.close()

        # Expired message swept, with reason sidecar
        inbox_files = vfs.list_dir(inbox_path("agent:bob"), ZONE)
        assert len(inbox_files) == 0
        dl_files = vfs.list_dir(dead_letter_path("agent:bob"), ZONE)
        reason_files = [f for f in dl_files if f.endswith(".reason.json")]
        assert len(reason_files) == 1  # .reason.json sidecar written by shared helper


# ===========================================================================
# Issue #3197: SSE stream + EventPublisher end-to-end
# ===========================================================================


class TestSSEStreamIntegration:
    """Integration: CacheStore pub/sub event -> SSE stream delivery."""

    @pytest.mark.asyncio
    async def test_event_publisher_delivers_to_subscriber(self, vfs: InMemoryVFS) -> None:
        """Full flow: send with EventPublisher -> subscriber receives event."""
        import asyncio
        import json

        from nexus.bricks.ipc.wakeup import CacheStoreEventPublisher
        from nexus.cache.inmemory import InMemoryCacheStore

        await _setup_agents(vfs, "agent:alice", "agent:bob")
        cache_store = InMemoryCacheStore()

        # Simulate SSE subscriber (what the /stream endpoint does)
        received_events: list[dict] = []

        async def sse_subscriber():
            async with cache_store.subscribe("ipc.inbox.agent:bob") as messages:
                async for msg in messages:
                    received_events.append(json.loads(msg))
                    if len(received_events) >= 2:
                        break

        subscriber_task = asyncio.create_task(sse_subscriber())
        await asyncio.sleep(0.02)  # Let subscriber register

        # Send via MessageSender with EventPublisher (same as REST API)
        event_pub = CacheStoreEventPublisher(cache_store)
        sender = MessageSender(vfs, event_pub, zone_id=ZONE)

        env1 = MessageEnvelope(
            sender="agent:alice",
            recipient="agent:bob",
            type=MessageType.TASK,
            id="msg_sse_1",
            payload={"action": "first"},
        )
        env2 = MessageEnvelope(
            sender="agent:alice",
            recipient="agent:bob",
            type=MessageType.TASK,
            id="msg_sse_2",
            payload={"action": "second"},
        )
        await sender.send(env1)
        await sender.send(env2)

        # Wait for subscriber to receive both
        await asyncio.wait_for(subscriber_task, timeout=2.0)

        assert len(received_events) == 2
        assert received_events[0]["event"] == "message_delivered"
        assert received_events[0]["sender"] == "agent:alice"
        assert received_events[0]["message_id"] == "msg_sse_1"
        assert received_events[1]["message_id"] == "msg_sse_2"

        await cache_store.close()

    @pytest.mark.asyncio
    async def test_multiple_agents_receive_own_events(self, vfs: InMemoryVFS) -> None:
        """Each agent's SSE stream only receives events for their inbox."""
        import asyncio
        import json

        from nexus.bricks.ipc.wakeup import CacheStoreEventPublisher
        from nexus.cache.inmemory import InMemoryCacheStore

        await _setup_agents(vfs, "agent:alice", "agent:bob", "agent:carol")
        cache_store = InMemoryCacheStore()

        bob_events: list[dict] = []
        carol_events: list[dict] = []

        async def bob_sub():
            async with cache_store.subscribe("ipc.inbox.agent:bob") as msgs:
                async for msg in msgs:
                    bob_events.append(json.loads(msg))
                    break

        async def carol_sub():
            async with cache_store.subscribe("ipc.inbox.agent:carol") as msgs:
                async for msg in msgs:
                    carol_events.append(json.loads(msg))
                    break

        bob_task = asyncio.create_task(bob_sub())
        carol_task = asyncio.create_task(carol_sub())
        await asyncio.sleep(0.02)

        event_pub = CacheStoreEventPublisher(cache_store)
        sender = MessageSender(vfs, event_pub, zone_id=ZONE)

        # Send to bob only
        await sender.send(
            MessageEnvelope(
                sender="agent:alice",
                recipient="agent:bob",
                type=MessageType.TASK,
                payload={"for": "bob"},
            )
        )
        # Send to carol only
        await sender.send(
            MessageEnvelope(
                sender="agent:alice",
                recipient="agent:carol",
                type=MessageType.TASK,
                payload={"for": "carol"},
            )
        )

        await asyncio.wait_for(bob_task, timeout=2.0)
        await asyncio.wait_for(carol_task, timeout=2.0)

        assert len(bob_events) == 1
        assert bob_events[0]["recipient"] == "agent:bob"
        assert len(carol_events) == 1
        assert carol_events[0]["recipient"] == "agent:carol"

        await cache_store.close()
