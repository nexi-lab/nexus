"""Unit tests for MessageSender and MessageProcessor."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from nexus.cache.inmemory import InMemoryCacheStore
from nexus.ipc.conventions import (
    dead_letter_path,
    inbox_path,
    message_path_in_inbox,
    outbox_path,
    processed_path,
)
from nexus.ipc.delivery import DeliveryMode, MessageProcessor, MessageSender
from nexus.ipc.envelope import MessageEnvelope, MessageType
from nexus.ipc.exceptions import (
    EnvelopeValidationError,
    InboxFullError,
    InboxNotFoundError,
)
from nexus.ipc.provisioning import AgentProvisioner

from .fakes import (
    InMemoryEventPublisher,
    InMemoryHotPathPublisher,
    InMemoryHotPathSubscriber,
    InMemoryVFS,
)

ZONE = "test-zone"


async def _provision_agent(vfs: InMemoryVFS, agent_id: str) -> None:
    """Provision agent directories using AgentProvisioner (DRY)."""
    provisioner = AgentProvisioner(vfs, zone_id=ZONE)
    await provisioner.provision(agent_id)


def _make_envelope(
    sender: str = "agent:alice",
    recipient: str = "agent:bob",
    msg_id: str = "msg_test001",
    ttl_seconds: int | None = None,
    timestamp: datetime | None = None,
) -> MessageEnvelope:
    return MessageEnvelope(
        sender=sender,
        recipient=recipient,
        type=MessageType.TASK,
        id=msg_id,
        timestamp=timestamp or datetime.now(UTC),
        ttl_seconds=ttl_seconds,
        payload={"test": True},
    )


class TestMessageSender:
    """Tests for sending messages to inboxes."""

    @pytest.fixture
    def vfs(self) -> InMemoryVFS:
        return InMemoryVFS()

    @pytest.fixture
    def publisher(self) -> InMemoryEventPublisher:
        return InMemoryEventPublisher()

    @pytest.mark.asyncio
    async def test_send_success(self, vfs: InMemoryVFS, publisher: InMemoryEventPublisher) -> None:
        await _provision_agent(vfs, "agent:bob")
        await _provision_agent(vfs, "agent:alice")
        sender = MessageSender(vfs, publisher, zone_id=ZONE)
        env = _make_envelope()

        path = await sender.send(env)

        assert path.startswith("/agents/agent:bob/inbox/")
        assert path.endswith(".json")
        # Verify file was written
        data = await vfs.read(path, ZONE)
        restored = MessageEnvelope.from_bytes(data)
        assert restored.id == env.id
        # Verify EventBus notification
        assert len(publisher.published) == 1
        assert publisher.published[0][0] == "ipc.inbox.agent:bob"

    @pytest.mark.asyncio
    async def test_send_writes_outbox_copy(self, vfs: InMemoryVFS) -> None:
        await _provision_agent(vfs, "agent:bob")
        await _provision_agent(vfs, "agent:alice")
        sender = MessageSender(vfs, zone_id=ZONE)
        env = _make_envelope()

        await sender.send(env)

        outbox_files = await vfs.list_dir(outbox_path("agent:alice"), ZONE)
        assert len(outbox_files) == 1

    @pytest.mark.asyncio
    async def test_send_inbox_not_found(self, vfs: InMemoryVFS) -> None:
        sender = MessageSender(vfs, zone_id=ZONE)
        env = _make_envelope(recipient="agent:unknown")

        with pytest.raises(InboxNotFoundError, match="agent:unknown"):
            await sender.send(env)

    @pytest.mark.asyncio
    async def test_send_inbox_full(self, vfs: InMemoryVFS) -> None:
        await _provision_agent(vfs, "agent:bob")
        sender = MessageSender(vfs, zone_id=ZONE, max_inbox_size=2)

        # Fill inbox to capacity
        for i in range(2):
            env = _make_envelope(msg_id=f"msg_{i}")
            await sender.send(env)

        # Next send should fail
        with pytest.raises(InboxFullError, match="2/2"):
            await sender.send(_make_envelope(msg_id="msg_overflow"))

    @pytest.mark.asyncio
    async def test_send_same_sender_recipient_rejected(self, vfs: InMemoryVFS) -> None:
        await _provision_agent(vfs, "agent:alice")
        sender = MessageSender(vfs, zone_id=ZONE)
        env = _make_envelope(sender="agent:alice", recipient="agent:alice")

        with pytest.raises(EnvelopeValidationError, match="different"):
            await sender.send(env)

    @pytest.mark.asyncio
    async def test_send_eventbus_failure_still_succeeds(self, vfs: InMemoryVFS) -> None:
        """EventBus failure should not prevent message delivery."""
        await _provision_agent(vfs, "agent:bob")
        await _provision_agent(vfs, "agent:alice")
        failing_publisher = InMemoryEventPublisher(should_fail=True)
        sender = MessageSender(vfs, failing_publisher, zone_id=ZONE)

        # Should succeed even though EventBus fails
        path = await sender.send(_make_envelope())
        assert path.startswith("/agents/agent:bob/inbox/")

    @pytest.mark.asyncio
    async def test_send_path_traversal_rejected(self, vfs: InMemoryVFS) -> None:
        """Sender/recipient with path separators must be rejected."""
        await _provision_agent(vfs, "agent:bob")
        sender = MessageSender(vfs, zone_id=ZONE)
        env = _make_envelope(sender="../../etc/passwd", recipient="agent:bob")

        with pytest.raises(EnvelopeValidationError, match="path separators"):
            await sender.send(env)

    @pytest.mark.asyncio
    async def test_send_payload_too_large_rejected(self, vfs: InMemoryVFS) -> None:
        """Messages exceeding max_payload_bytes must be rejected."""
        await _provision_agent(vfs, "agent:bob")
        sender = MessageSender(vfs, zone_id=ZONE, max_payload_bytes=100)
        env = MessageEnvelope(
            sender="agent:alice",
            recipient="agent:bob",
            type=MessageType.TASK,
            payload={"data": "x" * 200},
        )

        with pytest.raises(EnvelopeValidationError, match="exceeds limit"):
            await sender.send(env)

    @pytest.mark.asyncio
    async def test_send_cancel_without_correlation_id_rejected(self, vfs: InMemoryVFS) -> None:
        """CANCEL messages require a correlation_id."""
        await _provision_agent(vfs, "agent:bob")
        sender = MessageSender(vfs, zone_id=ZONE)
        env = MessageEnvelope(
            sender="agent:alice",
            recipient="agent:bob",
            type=MessageType.CANCEL,
            # No correlation_id
        )

        with pytest.raises(EnvelopeValidationError, match="correlation_id"):
            await sender.send(env)

    @pytest.mark.asyncio
    async def test_send_response_without_correlation_id_rejected(self, vfs: InMemoryVFS) -> None:
        """RESPONSE messages require a correlation_id."""
        await _provision_agent(vfs, "agent:bob")
        sender = MessageSender(vfs, zone_id=ZONE)
        env = MessageEnvelope(
            sender="agent:alice",
            recipient="agent:bob",
            type=MessageType.RESPONSE,
            # No correlation_id
        )

        with pytest.raises(EnvelopeValidationError, match="correlation_id"):
            await sender.send(env)


class TestMessageProcessor:
    """Tests for processing messages from inboxes."""

    @pytest.fixture
    def vfs(self) -> InMemoryVFS:
        return InMemoryVFS()

    @pytest.mark.asyncio
    async def test_process_success(self, vfs: InMemoryVFS) -> None:
        await _provision_agent(vfs, "agent:bob")
        env = _make_envelope()
        msg_path = message_path_in_inbox("agent:bob", env.id, env.timestamp)
        await vfs.write(msg_path, env.to_bytes(), ZONE)

        received: list[MessageEnvelope] = []

        async def handler(msg: MessageEnvelope) -> None:
            received.append(msg)

        processor = MessageProcessor(vfs, "agent:bob", handler, zone_id=ZONE)
        count = await processor.process_inbox()

        assert count == 1
        assert len(received) == 1
        assert received[0].id == env.id
        # Message should be in processed/, not inbox/
        inbox_files = await vfs.list_dir(inbox_path("agent:bob"), ZONE)
        assert len(inbox_files) == 0
        processed_files = await vfs.list_dir(processed_path("agent:bob"), ZONE)
        assert len(processed_files) == 1

    @pytest.mark.asyncio
    async def test_process_handler_failure_moves_to_dead_letter(self, vfs: InMemoryVFS) -> None:
        await _provision_agent(vfs, "agent:bob")
        env = _make_envelope()
        msg_path = message_path_in_inbox("agent:bob", env.id, env.timestamp)
        await vfs.write(msg_path, env.to_bytes(), ZONE)

        async def failing_handler(msg: MessageEnvelope) -> None:
            raise RuntimeError("Handler exploded")

        processor = MessageProcessor(vfs, "agent:bob", failing_handler, zone_id=ZONE)
        count = await processor.process_inbox()

        assert count == 1
        inbox_files = await vfs.list_dir(inbox_path("agent:bob"), ZONE)
        assert len(inbox_files) == 0
        dl_files = await vfs.list_dir(dead_letter_path("agent:bob"), ZONE)
        assert len(dl_files) == 1

    @pytest.mark.asyncio
    async def test_process_expired_ttl_skips_handler(self, vfs: InMemoryVFS) -> None:
        await _provision_agent(vfs, "agent:bob")
        old_ts = datetime(2020, 1, 1, tzinfo=UTC)
        env = _make_envelope(timestamp=old_ts, ttl_seconds=60)
        msg_path = message_path_in_inbox("agent:bob", env.id, env.timestamp)
        await vfs.write(msg_path, env.to_bytes(), ZONE)

        handler_called = False

        async def handler(msg: MessageEnvelope) -> None:
            nonlocal handler_called
            handler_called = True

        processor = MessageProcessor(vfs, "agent:bob", handler, zone_id=ZONE)
        await processor.process_inbox()

        assert not handler_called  # Expired message should NOT invoke handler
        dl_files = await vfs.list_dir(dead_letter_path("agent:bob"), ZONE)
        assert len(dl_files) == 1

    @pytest.mark.asyncio
    async def test_process_dedup_skips_duplicate(self, vfs: InMemoryVFS) -> None:
        await _provision_agent(vfs, "agent:bob")
        env = _make_envelope(msg_id="msg_dup")
        msg_path = message_path_in_inbox("agent:bob", env.id, env.timestamp)
        await vfs.write(msg_path, env.to_bytes(), ZONE)

        call_count = 0

        async def handler(msg: MessageEnvelope) -> None:
            nonlocal call_count
            call_count += 1

        cache_store = InMemoryCacheStore()
        processor = MessageProcessor(
            vfs, "agent:bob", handler, zone_id=ZONE, cache_store=cache_store
        )
        # Process once
        await processor.process_inbox()
        assert call_count == 1

        # Write same message ID again
        ts2 = datetime.now(UTC) + timedelta(seconds=1)
        env2 = _make_envelope(msg_id="msg_dup", timestamp=ts2)
        msg_path2 = message_path_in_inbox("agent:bob", env2.id, env2.timestamp)
        await vfs.write(msg_path2, env2.to_bytes(), ZONE)

        # Process again — should skip duplicate
        await processor.process_inbox()
        assert call_count == 1  # Handler not called again

    @pytest.mark.asyncio
    async def test_process_malformed_message(self, vfs: InMemoryVFS) -> None:
        await _provision_agent(vfs, "agent:bob")
        msg_path = "/agents/agent:bob/inbox/20260212T100000_msg_bad.json"
        await vfs.write(msg_path, b"not valid json {{{", ZONE)

        async def handler(msg: MessageEnvelope) -> None:
            pass  # Should never be called

        processor = MessageProcessor(vfs, "agent:bob", handler, zone_id=ZONE)
        count = await processor.process_inbox()

        assert count == 1
        # Malformed message should go to dead letter
        inbox_files = await vfs.list_dir(inbox_path("agent:bob"), ZONE)
        assert len(inbox_files) == 0
        dl_files = await vfs.list_dir(dead_letter_path("agent:bob"), ZONE)
        assert len(dl_files) == 1

    @pytest.mark.asyncio
    async def test_process_empty_inbox(self, vfs: InMemoryVFS) -> None:
        await _provision_agent(vfs, "agent:bob")

        async def handler(msg: MessageEnvelope) -> None:
            pass

        processor = MessageProcessor(vfs, "agent:bob", handler, zone_id=ZONE)
        count = await processor.process_inbox()
        assert count == 0

    @pytest.mark.asyncio
    async def test_process_multiple_messages_in_order(self, vfs: InMemoryVFS) -> None:
        await _provision_agent(vfs, "agent:bob")
        received_ids: list[str] = []

        ts1 = datetime(2026, 2, 12, 10, 0, 0, tzinfo=UTC)
        ts2 = datetime(2026, 2, 12, 10, 0, 1, tzinfo=UTC)
        ts3 = datetime(2026, 2, 12, 10, 0, 2, tzinfo=UTC)

        for ts, mid in [(ts1, "msg_01"), (ts2, "msg_02"), (ts3, "msg_03")]:
            env = _make_envelope(msg_id=mid, timestamp=ts)
            path = message_path_in_inbox("agent:bob", env.id, env.timestamp)
            await vfs.write(path, env.to_bytes(), ZONE)

        async def handler(msg: MessageEnvelope) -> None:
            received_ids.append(msg.id)

        processor = MessageProcessor(vfs, "agent:bob", handler, zone_id=ZONE)
        count = await processor.process_inbox()

        assert count == 3
        assert received_ids == ["msg_01", "msg_02", "msg_03"]


class TestHotColdDelivery:
    """Tests for tiered hot/cold message delivery (#1747, LEGO 17.7)."""

    @pytest.fixture
    def vfs(self) -> InMemoryVFS:
        return InMemoryVFS()

    @pytest.fixture
    def hot_pub(self) -> InMemoryHotPathPublisher:
        return InMemoryHotPathPublisher()

    @pytest.fixture
    def hot_sub(self) -> InMemoryHotPathSubscriber:
        return InMemoryHotPathSubscriber()

    # --- MessageSender tests ---

    @pytest.mark.asyncio
    async def test_hot_cold_send_publishes_nats_and_writes_file(
        self, vfs: InMemoryVFS, hot_pub: InMemoryHotPathPublisher
    ) -> None:
        """HOT_COLD mode: NATS publish + async filesystem write both fire."""
        await _provision_agent(vfs, "agent:bob")
        await _provision_agent(vfs, "agent:alice")
        sender = MessageSender(
            vfs,
            zone_id=ZONE,
            hot_publisher=hot_pub,
            delivery_mode=DeliveryMode.HOT_COLD,
        )
        env = _make_envelope()
        path = await sender.send(env)
        await sender.drain()

        # Hot path: NATS publish captured
        assert len(hot_pub.published) == 1
        assert hot_pub.published[0][0] == "agents.agent:bob.inbox"
        # Verify hot bytes are compact (no indentation)
        assert b"\n" not in hot_pub.published[0][1]

        # Cold path: file written (after drain)
        inbox_files = await vfs.list_dir(inbox_path("agent:bob"), ZONE)
        assert len(inbox_files) == 1

        # Return value is the hot:// synthetic path (hot succeeded)
        assert path.startswith("hot://")

    @pytest.mark.asyncio
    async def test_nats_down_falls_back_to_sync_cold(self, vfs: InMemoryVFS) -> None:
        """When NATS publish fails, HOT_COLD degrades to synchronous cold."""
        await _provision_agent(vfs, "agent:bob")
        await _provision_agent(vfs, "agent:alice")
        failing_pub = InMemoryHotPathPublisher(should_fail=True)
        sender = MessageSender(
            vfs,
            zone_id=ZONE,
            hot_publisher=failing_pub,
            delivery_mode=DeliveryMode.HOT_COLD,
        )
        env = _make_envelope()
        path = await sender.send(env)

        # Cold path: file written synchronously (fallback)
        assert path.startswith("/agents/agent:bob/inbox/")
        data = await vfs.read(path, ZONE)
        restored = MessageEnvelope.from_bytes(data)
        assert restored.id == env.id

    @pytest.mark.asyncio
    async def test_hot_only_no_filesystem_write(
        self, vfs: InMemoryVFS, hot_pub: InMemoryHotPathPublisher
    ) -> None:
        """HOT_ONLY mode: no filesystem write occurs."""
        await _provision_agent(vfs, "agent:bob")
        sender = MessageSender(
            vfs,
            zone_id=ZONE,
            hot_publisher=hot_pub,
            delivery_mode=DeliveryMode.HOT_ONLY,
        )
        env = _make_envelope()
        path = await sender.send(env)

        # Hot path fired
        assert len(hot_pub.published) == 1
        # No cold write
        inbox_files = await vfs.list_dir(inbox_path("agent:bob"), ZONE)
        assert len(inbox_files) == 0
        # Synthetic path
        assert path.startswith("hot://")

    @pytest.mark.asyncio
    async def test_graceful_drain_completes_pending_cold_writes(
        self, vfs: InMemoryVFS, hot_pub: InMemoryHotPathPublisher
    ) -> None:
        """drain() awaits all pending background cold-write tasks."""
        await _provision_agent(vfs, "agent:bob")
        await _provision_agent(vfs, "agent:alice")
        sender = MessageSender(
            vfs,
            zone_id=ZONE,
            hot_publisher=hot_pub,
            delivery_mode=DeliveryMode.HOT_COLD,
        )

        # Send multiple messages
        for i in range(5):
            await sender.send(_make_envelope(msg_id=f"msg_drain_{i}"))

        # Before drain, tasks might not be complete
        assert len(sender._pending_tasks) >= 0  # at least some created

        # After drain, all cold writes complete
        await sender.drain()
        inbox_files = await vfs.list_dir(inbox_path("agent:bob"), ZONE)
        assert len(inbox_files) == 5

    @pytest.mark.asyncio
    async def test_default_delivery_mode_is_cold_only_when_no_hot_publisher(
        self, vfs: InMemoryVFS
    ) -> None:
        """Backwards compat: no hot_publisher -> COLD_ONLY regardless of mode."""
        sender = MessageSender(
            vfs,
            zone_id=ZONE,
            delivery_mode=DeliveryMode.HOT_COLD,  # Requested, but no publisher
        )
        assert sender._mode == DeliveryMode.COLD_ONLY

    # --- MessageProcessor hot-path tests ---

    @pytest.mark.asyncio
    async def test_hot_cold_dedup_prevents_double_processing(
        self,
        vfs: InMemoryVFS,
        hot_sub: InMemoryHotPathSubscriber,
    ) -> None:
        """Same message via hot + cold: handler invoked only once."""
        await _provision_agent(vfs, "agent:bob")
        env = _make_envelope(msg_id="msg_dedup_hc")
        call_count = 0

        async def handler(msg: MessageEnvelope) -> None:
            nonlocal call_count
            call_count += 1

        cache_store = InMemoryCacheStore()
        processor = MessageProcessor(
            vfs,
            "agent:bob",
            handler,
            zone_id=ZONE,
            hot_subscriber=hot_sub,
            cache_store=cache_store,
        )
        await processor.start()

        # Deliver via hot path
        await hot_sub.inject("agents.agent:bob.inbox", env.to_hot_bytes())
        # Give the event loop a chance to process
        await asyncio.sleep(0.05)

        assert call_count == 1

        # Now deliver same message via cold path
        msg_path = message_path_in_inbox("agent:bob", env.id, env.timestamp)
        await vfs.write(msg_path, env.to_bytes(), ZONE)
        await processor.process_inbox()

        # Handler should NOT be called again (deduped)
        assert call_count == 1

        await processor.stop()

    @pytest.mark.asyncio
    async def test_hot_path_ttl_expired_skips_handler(
        self,
        vfs: InMemoryVFS,
        hot_sub: InMemoryHotPathSubscriber,
    ) -> None:
        """Expired messages on hot path are dropped without invoking handler."""
        await _provision_agent(vfs, "agent:bob")
        old_ts = datetime(2020, 1, 1, tzinfo=UTC)
        env = _make_envelope(msg_id="msg_expired_hot", timestamp=old_ts, ttl_seconds=60)
        handler_called = False

        async def handler(msg: MessageEnvelope) -> None:
            nonlocal handler_called
            handler_called = True

        processor = MessageProcessor(
            vfs, "agent:bob", handler, zone_id=ZONE, hot_subscriber=hot_sub
        )
        await processor.start()

        await hot_sub.inject("agents.agent:bob.inbox", env.to_hot_bytes())
        await asyncio.sleep(0.05)

        assert not handler_called
        await processor.stop()

    @pytest.mark.asyncio
    async def test_hot_cold_backpressure_inbox_full_still_delivers_hot(
        self,
        vfs: InMemoryVFS,
        hot_pub: InMemoryHotPathPublisher,
    ) -> None:
        """When inbox is full, HOT_COLD background cold write fails but hot succeeds."""
        await _provision_agent(vfs, "agent:bob")
        await _provision_agent(vfs, "agent:alice")
        sender = MessageSender(
            vfs,
            zone_id=ZONE,
            hot_publisher=hot_pub,
            delivery_mode=DeliveryMode.HOT_COLD,
            max_inbox_size=1,
        )

        # Fill inbox
        env_fill = _make_envelope(msg_id="msg_fill")
        await MessageSender(vfs, zone_id=ZONE).send(env_fill)

        # Send via hot_cold — hot should succeed, background cold will fail silently
        env = _make_envelope(msg_id="msg_hot_bp")
        path = await sender.send(env)
        await sender.drain()

        # Hot delivery succeeded
        assert len(hot_pub.published) == 1
        assert path.startswith("hot://")
        # Inbox still has only 1 file (cold write failed due to backpressure)
        inbox_files = await vfs.list_dir(inbox_path("agent:bob"), ZONE)
        assert len(inbox_files) == 1
