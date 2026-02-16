"""Integration tests for the IPC brick.

Tests the full message lifecycle: provisioning → send → receive →
process → dead_letter. Uses InMemoryVFS to test components working
together without kernel dependencies.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from nexus.ipc.conventions import (
    dead_letter_path,
    inbox_path,
    outbox_path,
    processed_path,
)
from nexus.ipc.delivery import MessageProcessor, MessageSender
from nexus.ipc.discovery import AgentDiscovery
from nexus.ipc.envelope import MessageEnvelope, MessageType
from nexus.ipc.exceptions import InboxFullError
from nexus.ipc.provisioning import AgentProvisioner
from nexus.ipc.sweep import TTLSweeper

# Import fakes from unit tests
from tests.unit.ipc.fakes import InMemoryEventPublisher, InMemoryVFS

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
        inbox_files = await vfs.list_dir(inbox_path("agent:bob"), ZONE)
        assert len(inbox_files) == 1

        # Verify EventBus was notified
        assert len(publisher.published) == 1
        assert publisher.published[0][0] == "ipc.inbox.agent:bob"

        # Verify outbox copy exists
        outbox_files = await vfs.list_dir(outbox_path("agent:alice"), ZONE)
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
        inbox_files = await vfs.list_dir(inbox_path("agent:bob"), ZONE)
        assert len(inbox_files) == 0
        processed_files = await vfs.list_dir(processed_path("agent:bob"), ZONE)
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
        inbox_files = await vfs.list_dir(inbox_path("agent:bob"), ZONE)
        assert len(inbox_files) == 0
        dl_files = await vfs.list_dir(dead_letter_path("agent:bob"), ZONE)
        assert len(dl_files) == 1

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
        from nexus.ipc.conventions import message_path_in_inbox

        msg_path = message_path_in_inbox("agent:bob", env.id, env.timestamp)
        await vfs.write(msg_path, env.to_bytes(), ZONE)

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
        inbox_files = await vfs.list_dir(inbox_path("agent:bob"), ZONE)
        assert len(inbox_files) == 1  # Only the valid message remains
        dl_files = await vfs.list_dir(dead_letter_path("agent:bob"), ZONE)
        assert len(dl_files) == 1  # Expired message moved here


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
