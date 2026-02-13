"""Unit tests for MessageSender and MessageProcessor."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from nexus.ipc.conventions import (
    dead_letter_path,
    inbox_path,
    message_path_in_inbox,
    outbox_path,
    processed_path,
)
from nexus.ipc.delivery import MessageProcessor, MessageSender
from nexus.ipc.envelope import MessageEnvelope, MessageType
from nexus.ipc.exceptions import (
    EnvelopeValidationError,
    InboxFullError,
    InboxNotFoundError,
)

from .fakes import InMemoryEventPublisher, InMemoryVFS

ZONE = "test-zone"


async def _provision_agent(vfs: InMemoryVFS, agent_id: str) -> None:
    """Helper to create the standard directory layout for an agent."""
    await vfs.mkdir(f"/agents/{agent_id}", ZONE)
    await vfs.mkdir(f"/agents/{agent_id}/inbox", ZONE)
    await vfs.mkdir(f"/agents/{agent_id}/outbox", ZONE)
    await vfs.mkdir(f"/agents/{agent_id}/processed", ZONE)
    await vfs.mkdir(f"/agents/{agent_id}/dead_letter", ZONE)


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

        processor = MessageProcessor(vfs, "agent:bob", handler, zone_id=ZONE)
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
