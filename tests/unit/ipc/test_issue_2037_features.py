"""Unit tests for Issue #2037 core features.

Validates:
1. MessageProcessorRegistry
2. POST_WRITE hook integration
3. create_reply helper method
"""

import asyncio

import pytest

from nexus.ipc.delivery import MessageProcessor, MessageSender
from nexus.ipc.envelope import MessageEnvelope, MessageType
from nexus.ipc.hooks import register_ipc_hooks
from nexus.ipc.provisioning import AgentProvisioner
from nexus.ipc.registry import MessageProcessorRegistry
from nexus.services.hook_engine import ScopedHookEngine
from nexus.services.protocols.hook_engine import POST_WRITE, HookContext
from tests.unit.ipc.fakes import InMemoryStorageDriver

ZONE = "test-zone"


@pytest.mark.asyncio
async def test_message_processor_registry():
    """Test MessageProcessorRegistry basic operations."""
    storage = InMemoryStorageDriver()
    registry = MessageProcessorRegistry()

    # Create two processors
    messages_a = []

    async def handler_a(envelope: MessageEnvelope) -> None:
        messages_a.append(envelope)

    processor_a = MessageProcessor(
        storage=storage,
        agent_id="agent_a",
        handler=handler_a,
        zone_id=ZONE,
    )

    messages_b = []

    async def handler_b(envelope: MessageEnvelope) -> None:
        messages_b.append(envelope)

    processor_b = MessageProcessor(
        storage=storage,
        agent_id="agent_b",
        handler=handler_b,
        zone_id=ZONE,
    )

    # Register
    await registry.register("agent_a", processor_a)
    await registry.register("agent_b", processor_b)

    # Get
    assert registry.get("agent_a") is processor_a
    assert registry.get("agent_b") is processor_b
    assert registry.get("agent_c") is None

    # Count
    assert registry.count() == 2
    assert sorted(registry.list_agents()) == ["agent_a", "agent_b"]

    # Unregister
    result = await registry.unregister("agent_a")
    assert result is True
    assert registry.get("agent_a") is None
    assert registry.count() == 1

    # Stop all
    await registry.stop_all()
    assert registry.count() == 0


@pytest.mark.asyncio
async def test_post_write_hook_triggers_processor():
    """Test POST_WRITE hook integration (core Issue #2037 feature).

    This validates that writing to inbox via VFS triggers MessageProcessor,
    not just REST API.
    """
    storage = InMemoryStorageDriver()
    provisioner = AgentProvisioner(storage=storage, zone_id=ZONE)
    await provisioner.provision("agent_a")
    await provisioner.provision("agent_b")

    # Setup MessageProcessor with handler
    messages_received = []

    async def handler(envelope: MessageEnvelope) -> None:
        messages_received.append(envelope)

    processor = MessageProcessor(
        storage=storage,
        agent_id="agent_b",
        handler=handler,
        zone_id=ZONE,
    )

    # Setup registry and hooks
    registry = MessageProcessorRegistry()
    await registry.register("agent_b", processor)

    # Create a minimal hook engine for testing
    from nexus.plugins.async_hooks import AsyncHookEngine
    from nexus.plugins.hooks import PluginHooks

    inner_engine = AsyncHookEngine(inner=PluginHooks())
    hook_engine = ScopedHookEngine(inner=inner_engine)
    await register_ipc_hooks(hook_engine, registry)

    # Send message to agent_b's inbox
    sender = MessageSender(storage=storage, zone_id=ZONE)
    envelope = MessageEnvelope.model_validate(
        {
            "from": "agent_a",
            "to": "agent_b",
            "type": "task",
            "payload": {"action": "test_hook"},
        }
    )
    msg_path = await sender.send(envelope)

    # Fire POST_WRITE hook (simulating VFS POST_WRITE trigger)
    context = HookContext(
        phase=POST_WRITE,
        path=msg_path,
        zone_id=ZONE,
        agent_id=None,
        payload={},
    )
    result = await hook_engine.fire(POST_WRITE, context)
    assert result.proceed is True

    # Wait briefly for async hook processing
    await asyncio.sleep(0.1)

    # Validate: message was processed
    assert len(messages_received) == 1
    assert messages_received[0].sender == "agent_a"
    assert messages_received[0].payload["action"] == "test_hook"

    # Cleanup
    await registry.stop_all()


@pytest.mark.asyncio
async def test_create_reply_helper():
    """Test MessageEnvelope.create_reply() helper (Issue #2037 acceptance criteria)."""
    # Agent A sends request to B
    request = MessageEnvelope.model_validate(
        {
            "from": "agent_a",
            "to": "agent_b",
            "type": "task",
            "payload": {"action": "process", "data": "foo"},
            "ttl_seconds": 3600,
        }
    )

    # Agent B creates reply using helper
    reply = request.create_reply(payload={"status": "done", "result": 42})

    # Validate reply envelope
    assert reply.sender == "agent_b"  # Swapped
    assert reply.recipient == "agent_a"  # Swapped
    assert reply.type == MessageType.RESPONSE
    assert reply.correlation_id == request.id  # Linked
    assert reply.payload == {"status": "done", "result": 42}
    assert reply.ttl_seconds == 3600  # Inherited
    assert reply.id != request.id  # New message ID
    assert reply.id.startswith("msg_")

    # Test TTL override
    reply_custom_ttl = request.create_reply(payload={"status": "ok"}, ttl_seconds=60)
    assert reply_custom_ttl.ttl_seconds == 60


@pytest.mark.asyncio
async def test_reply_pattern_e2e():
    """Test full reply pattern: request → reply → response delivery."""
    storage = InMemoryStorageDriver()
    provisioner = AgentProvisioner(storage=storage, zone_id=ZONE)
    await provisioner.provision("agent_a")
    await provisioner.provision("agent_b")

    sender = MessageSender(storage=storage, zone_id=ZONE)

    # Agent A sends request to B
    request = MessageEnvelope.model_validate(
        {
            "from": "agent_a",
            "to": "agent_b",
            "type": "task",
            "payload": {"action": "compute", "x": 10, "y": 20},
        }
    )
    await sender.send(request)

    # Agent B processes request
    b_requests = []

    async def handler_b(envelope: MessageEnvelope) -> None:
        b_requests.append(envelope)

    processor_b = MessageProcessor(
        storage=storage,
        agent_id="agent_b",
        handler=handler_b,
        zone_id=ZONE,
    )
    await processor_b.process_inbox()

    assert len(b_requests) == 1
    received_request = b_requests[0]
    assert received_request.sender == "agent_a"
    assert received_request.payload["action"] == "compute"

    # Agent B creates and sends reply
    reply = received_request.create_reply(payload={"result": 30, "status": "success"})
    await sender.send(reply)

    # Agent A processes reply
    a_replies = []

    async def handler_a(envelope: MessageEnvelope) -> None:
        a_replies.append(envelope)

    processor_a = MessageProcessor(
        storage=storage,
        agent_id="agent_a",
        handler=handler_a,
        zone_id=ZONE,
    )
    await processor_a.process_inbox()

    assert len(a_replies) == 1
    received_reply = a_replies[0]
    assert received_reply.type == MessageType.RESPONSE
    assert received_reply.sender == "agent_b"
    assert received_reply.recipient == "agent_a"
    assert received_reply.correlation_id == request.id
    assert received_reply.payload["result"] == 30

    # Cleanup
    await processor_a.stop()
    await processor_b.stop()


if __name__ == "__main__":
    asyncio.run(test_message_processor_registry())
    asyncio.run(test_post_write_hook_triggers_processor())
    asyncio.run(test_create_reply_helper())
    asyncio.run(test_reply_pattern_e2e())
    print("✅ All Issue #2037 features validated")
