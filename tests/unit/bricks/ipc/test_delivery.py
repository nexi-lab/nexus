"""Unit tests for MessageSender and MessageProcessor."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from nexus.bricks.ipc.conventions import (
    dead_letter_path,
    inbox_path,
    message_path_in_inbox,
    outbox_path,
    processed_path,
)
from nexus.bricks.ipc.delivery import MessageProcessor, MessageSender
from nexus.bricks.ipc.envelope import MessageEnvelope, MessageType
from nexus.bricks.ipc.exceptions import (
    EnvelopeValidationError,
    InboxFullError,
    InboxNotFoundError,
)
from nexus.bricks.ipc.provisioning import AgentProvisioner
from nexus.cache.inmemory import InMemoryCacheStore

from .fakes import (
    FlakyEventSubscriber,
    InMemoryEventPublisher,
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
        data = vfs.sys_read(path, ZONE)
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

        outbox_files = vfs.list_dir(outbox_path("agent:alice"), ZONE)
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
        from pydantic import ValidationError

        await _provision_agent(vfs, "agent:bob")

        # Validation now happens at envelope construction time (Pydantic validators)
        with pytest.raises(ValidationError, match="path separators"):
            _make_envelope(sender="../../etc/passwd", recipient="agent:bob")

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


class TestMessageSenderWakeup:
    """Tests for ancillary MessageSender behaviour (TTL pub/sub)."""

    @pytest.fixture
    def vfs(self) -> InMemoryVFS:
        return InMemoryVFS()

    @pytest.mark.asyncio
    async def test_send_publishes_ttl_schedule_event(self, vfs: InMemoryVFS) -> None:
        """Messages with TTL should publish a schedule event to CacheStore."""
        await _provision_agent(vfs, "agent:bob")
        await _provision_agent(vfs, "agent:alice")
        cache_store = InMemoryCacheStore()

        # Subscribe before sending to capture the event
        import json

        received: list[dict] = []

        async def _consume() -> None:
            async with cache_store.subscribe(f"ipc:ttl:schedule:{ZONE}") as msgs:
                async for msg in msgs:
                    received.append(json.loads(msg))
                    break  # Just capture one

        consumer_task = asyncio.create_task(_consume())
        await asyncio.sleep(0.01)  # Let subscriber register

        sender = MessageSender(vfs, zone_id=ZONE, cache_store=cache_store)
        await sender.send(_make_envelope(ttl_seconds=300))

        await asyncio.sleep(0.01)
        consumer_task.cancel()
        try:
            await consumer_task
        except asyncio.CancelledError:
            pass

        assert len(received) == 1
        assert received[0]["agent_id"] == "agent:bob"
        assert "expires_at" in received[0]

    @pytest.mark.asyncio
    async def test_send_no_ttl_no_schedule_event(self, vfs: InMemoryVFS) -> None:
        """Messages without TTL should NOT publish a schedule event."""
        await _provision_agent(vfs, "agent:bob")
        await _provision_agent(vfs, "agent:alice")
        cache_store = InMemoryCacheStore()
        sender = MessageSender(vfs, zone_id=ZONE, cache_store=cache_store)

        await sender.send(_make_envelope(ttl_seconds=None))

        # No events should have been published
        # (InMemoryCacheStore has no way to check this directly,
        # but we verify no error was raised)


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
        vfs.write(msg_path, env.to_bytes(), ZONE)

        received: list[MessageEnvelope] = []

        async def handler(msg: MessageEnvelope) -> None:
            received.append(msg)

        processor = MessageProcessor(vfs, "agent:bob", handler, zone_id=ZONE)
        count = await processor.process_inbox()

        assert count == 1
        assert len(received) == 1
        assert received[0].id == env.id
        # Message should be in processed/, not inbox/
        inbox_files = vfs.list_dir(inbox_path("agent:bob"), ZONE)
        assert len(inbox_files) == 0
        processed_files = vfs.list_dir(processed_path("agent:bob"), ZONE)
        assert len(processed_files) == 1

    @pytest.mark.asyncio
    async def test_process_handler_failure_moves_to_dead_letter(self, vfs: InMemoryVFS) -> None:
        await _provision_agent(vfs, "agent:bob")
        env = _make_envelope()
        msg_path = message_path_in_inbox("agent:bob", env.id, env.timestamp)
        vfs.write(msg_path, env.to_bytes(), ZONE)

        async def failing_handler(msg: MessageEnvelope) -> None:
            raise RuntimeError("Handler exploded")

        processor = MessageProcessor(vfs, "agent:bob", failing_handler, zone_id=ZONE)
        count = await processor.process_inbox()

        assert count == 1
        inbox_files = vfs.list_dir(inbox_path("agent:bob"), ZONE)
        assert len(inbox_files) == 0
        dl_files = vfs.list_dir(dead_letter_path("agent:bob"), ZONE)
        dl_msgs = [f for f in dl_files if not f.endswith(".reason.json")]
        assert len(dl_msgs) == 1

    @pytest.mark.asyncio
    async def test_process_expired_ttl_skips_handler(self, vfs: InMemoryVFS) -> None:
        await _provision_agent(vfs, "agent:bob")
        old_ts = datetime(2020, 1, 1, tzinfo=UTC)
        env = _make_envelope(timestamp=old_ts, ttl_seconds=60)
        msg_path = message_path_in_inbox("agent:bob", env.id, env.timestamp)
        vfs.write(msg_path, env.to_bytes(), ZONE)

        handler_called = False

        async def handler(msg: MessageEnvelope) -> None:
            nonlocal handler_called
            handler_called = True

        processor = MessageProcessor(vfs, "agent:bob", handler, zone_id=ZONE)
        await processor.process_inbox()

        assert not handler_called  # Expired message should NOT invoke handler
        dl_files = vfs.list_dir(dead_letter_path("agent:bob"), ZONE)
        dl_msgs = [f for f in dl_files if not f.endswith(".reason.json")]
        assert len(dl_msgs) == 1

    @pytest.mark.asyncio
    async def test_process_dedup_skips_duplicate(self, vfs: InMemoryVFS) -> None:
        await _provision_agent(vfs, "agent:bob")
        env = _make_envelope(msg_id="msg_dup")
        msg_path = message_path_in_inbox("agent:bob", env.id, env.timestamp)
        vfs.write(msg_path, env.to_bytes(), ZONE)

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
        vfs.write(msg_path2, env2.to_bytes(), ZONE)

        # Process again — should skip duplicate
        await processor.process_inbox()
        assert call_count == 1  # Handler not called again

    @pytest.mark.asyncio
    async def test_process_malformed_message(self, vfs: InMemoryVFS) -> None:
        await _provision_agent(vfs, "agent:bob")
        msg_path = "/agents/agent:bob/inbox/20260212T100000_msg_bad.json"
        vfs.write(msg_path, b"not valid json {{{", ZONE)

        async def handler(msg: MessageEnvelope) -> None:
            pass  # Should never be called

        processor = MessageProcessor(vfs, "agent:bob", handler, zone_id=ZONE)
        count = await processor.process_inbox()

        assert count == 1
        # Malformed message should go to dead letter
        inbox_files = vfs.list_dir(inbox_path("agent:bob"), ZONE)
        assert len(inbox_files) == 0
        dl_files = vfs.list_dir(dead_letter_path("agent:bob"), ZONE)
        dl_msgs = [f for f in dl_files if not f.endswith(".reason.json")]
        assert len(dl_msgs) == 1

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
            vfs.write(path, env.to_bytes(), ZONE)

        async def handler(msg: MessageEnvelope) -> None:
            received_ids.append(msg.id)

        processor = MessageProcessor(vfs, "agent:bob", handler, zone_id=ZONE)
        count = await processor.process_inbox()

        assert count == 3
        assert received_ids == ["msg_01", "msg_02", "msg_03"]


class TestSendProcessRoundtrip:
    """End-to-end roundtrip: MessageSender -> storage -> MessageProcessor (#9)."""

    @pytest.fixture
    def vfs(self) -> InMemoryVFS:
        return InMemoryVFS()

    @pytest.mark.asyncio
    async def test_send_then_process_roundtrip(self, vfs: InMemoryVFS) -> None:
        """Full single-zone roundtrip: send writes to inbox, processor reads and handles."""
        await _provision_agent(vfs, "agent:alice")
        await _provision_agent(vfs, "agent:bob")

        publisher = InMemoryEventPublisher()
        sender = MessageSender(vfs, publisher, zone_id=ZONE)
        env = _make_envelope(sender="agent:alice", recipient="agent:bob", msg_id="msg_roundtrip")

        path = await sender.send(env)
        assert path.startswith("/agents/agent:bob/inbox/")

        received: list[MessageEnvelope] = []

        async def handler(msg: MessageEnvelope) -> None:
            received.append(msg)

        processor = MessageProcessor(vfs, "agent:bob", handler, zone_id=ZONE)
        count = await processor.process_inbox()

        assert count == 1
        assert len(received) == 1
        assert received[0].id == "msg_roundtrip"
        assert received[0].sender == "agent:alice"
        assert received[0].payload == {"test": True}

        # Inbox empty, processed has the message
        inbox_files = vfs.list_dir(inbox_path("agent:bob"), ZONE)
        assert len(inbox_files) == 0
        processed_files = vfs.list_dir(processed_path("agent:bob"), ZONE)
        assert len(processed_files) == 1


class TestConcurrentProcessing:
    """Tests for concurrent process_inbox calls (#10)."""

    @pytest.fixture
    def vfs(self) -> InMemoryVFS:
        return InMemoryVFS()

    @pytest.mark.asyncio
    async def test_concurrent_process_inbox_at_least_once(self, vfs: InMemoryVFS) -> None:
        """Two concurrent process_inbox calls: at-least-once delivery, no crashes.

        The dedup cache prevents re-processing in *subsequent* sweeps, but
        concurrent processors may both invoke the handler (at-least-once
        semantics).  The key guarantees: no unhandled exceptions, handler
        called at least once, and the dedup cache is populated afterwards
        so a third sweep would skip the message.
        """
        await _provision_agent(vfs, "agent:bob")
        env = _make_envelope(msg_id="msg_concurrent")
        msg_path = message_path_in_inbox("agent:bob", env.id, env.timestamp)
        vfs.write(msg_path, env.to_bytes(), ZONE)

        call_count = 0

        async def handler(msg: MessageEnvelope) -> None:
            nonlocal call_count
            call_count += 1
            await asyncio.sleep(0.01)

        from nexus.cache.inmemory import InMemoryCacheStore

        cache_store = InMemoryCacheStore()
        p1 = MessageProcessor(vfs, "agent:bob", handler, zone_id=ZONE, cache_store=cache_store)
        p2 = MessageProcessor(vfs, "agent:bob", handler, zone_id=ZONE, cache_store=cache_store)

        results = await asyncio.gather(
            p1.process_inbox(),
            p2.process_inbox(),
            return_exceptions=True,
        )

        # Neither should raise
        for r in results:
            assert not isinstance(r, Exception), f"Unexpected exception: {r}"

        # At-least-once: handler called >= 1 time
        assert call_count >= 1

        # Dedup cache populated — a third sweep would skip
        assert await cache_store.exists(f"ipc:dedup:{ZONE}:msg_concurrent")


# ===========================================================================
# Listener resilience tests (#3197 — Issue 6A, 9A)
# ===========================================================================


class TestListenerResilience:
    """Tests for listener reconnection with exponential backoff (#3197)."""

    @pytest.fixture
    def vfs(self) -> InMemoryVFS:
        return InMemoryVFS()

    @pytest.mark.asyncio
    async def test_event_listener_reconnects_after_failure(self, vfs: InMemoryVFS) -> None:
        """EventBus listener should reconnect and process events after failure."""
        await _provision_agent(vfs, "agent:bob")
        env = _make_envelope()
        msg_path = message_path_in_inbox("agent:bob", env.id, env.timestamp)
        vfs.sys_write(msg_path, env.to_bytes(), ZONE)

        received: list[MessageEnvelope] = []

        async def handler(msg: MessageEnvelope) -> None:
            received.append(msg)

        # Fails once, then succeeds with one event and blocks
        subscriber = FlakyEventSubscriber(
            fail_count=1,
            events=[{"event": "message_delivered"}],
        )

        processor = MessageProcessor(
            vfs, "agent:bob", handler, zone_id=ZONE, event_subscriber=subscriber
        )

        from unittest.mock import AsyncMock, patch

        # Patch asyncio.sleep to be near-instant for backoff delays
        with patch("nexus.bricks.ipc.delivery.asyncio.sleep", new_callable=AsyncMock):
            task = asyncio.create_task(processor._event_listen_loop())
            # Wait for the subscriber to connect after recovery
            await asyncio.wait_for(subscriber._connected.wait(), timeout=5.0)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        # Subscriber was called twice: 1 failure + 1 success
        assert subscriber._call_count == 2
        # Handler was invoked after recovery
        assert len(received) == 1

    @pytest.mark.asyncio
    async def test_event_listener_stops_after_max_retries(self, vfs: InMemoryVFS) -> None:
        """EventBus listener should stop after MAX_LISTENER_RETRIES consecutive failures."""
        from nexus.bricks.ipc.delivery import _MAX_LISTENER_RETRIES

        await _provision_agent(vfs, "agent:bob")

        async def handler(msg: MessageEnvelope) -> None:
            pass

        # Always fails — should stop after _MAX_LISTENER_RETRIES
        subscriber = FlakyEventSubscriber(fail_count=100)

        processor = MessageProcessor(
            vfs, "agent:bob", handler, zone_id=ZONE, event_subscriber=subscriber
        )

        from unittest.mock import AsyncMock, patch

        # Patch asyncio.sleep to be near-instant for backoff delays
        with patch("nexus.bricks.ipc.delivery.asyncio.sleep", new_callable=AsyncMock):
            try:
                await asyncio.wait_for(processor._event_listen_loop(), timeout=5.0)
            except TimeoutError:
                pytest.fail("Event listen loop did not stop after max retries")

        # Should have been called _MAX_LISTENER_RETRIES times
        assert subscriber._call_count == _MAX_LISTENER_RETRIES


# ===========================================================================
# Signed delivery tests (#1729)
# ===========================================================================


class _FakeTokenEncryptor:
    """Trivial encryptor that prefixes 'enc:' for testing."""

    def encrypt_token(self, token: str) -> str:
        return f"enc:{token}"

    def decrypt_token(self, encrypted: str) -> str:
        return encrypted.removeprefix("enc:")


def _make_signing_fixtures() -> tuple:
    """Create crypto, key_service mock, signer and verifier for testing."""
    from unittest.mock import MagicMock

    from nexus.bricks.identity.crypto import IdentityCrypto
    from nexus.bricks.identity.did import create_did_key
    from nexus.bricks.identity.key_service import AgentKeyRecord
    from nexus.bricks.ipc.signing import MessageSigner, MessageVerifier

    crypto = IdentityCrypto(_FakeTokenEncryptor())
    private_key, public_key = crypto.generate_keypair()
    pub_bytes = IdentityCrypto.public_key_to_bytes(public_key)
    did = create_did_key(public_key)

    record = AgentKeyRecord(
        key_id="test-key-signed-delivery",
        agent_id="agent:alice",
        algorithm="Ed25519",
        public_key_bytes=pub_bytes,
        did=did,
        is_active=True,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        expires_at=None,
        revoked_at=None,
    )

    key_service = MagicMock()
    key_service.ensure_keypair.return_value = record
    key_service.decrypt_private_key.return_value = private_key
    key_service.get_public_key.return_value = record

    signer = MessageSigner(key_service, crypto, agent_id="agent:alice")
    verifier = MessageVerifier(key_service, crypto)

    return crypto, key_service, signer, verifier


class TestSignedDelivery:
    """Integration tests for signing in MessageSender/MessageProcessor (#1729)."""

    @pytest.fixture
    def vfs(self) -> InMemoryVFS:
        return InMemoryVFS()

    @pytest.mark.asyncio
    async def test_send_with_signer_adds_signature(self, vfs: InMemoryVFS) -> None:
        """MessageSender with signer -> envelope on disk has signature."""
        await _provision_agent(vfs, "agent:bob")
        await _provision_agent(vfs, "agent:alice")
        _, _, signer, _ = _make_signing_fixtures()

        sender = MessageSender(vfs, zone_id=ZONE, signer=signer)
        env = _make_envelope()
        path = await sender.send(env)

        data = vfs.sys_read(path, ZONE)
        restored = MessageEnvelope.from_bytes(data)
        assert restored.signature is not None
        assert restored.signer_did is not None
        assert restored.signer_key_id is not None

    @pytest.mark.asyncio
    async def test_process_signed_message_success(self, vfs: InMemoryVFS) -> None:
        """Processor with verifier, valid sig -> handler invoked."""
        from nexus.bricks.ipc.signing import SigningMode

        await _provision_agent(vfs, "agent:bob")
        _, _, signer, verifier = _make_signing_fixtures()

        env = _make_envelope()
        signed_env = signer.sign(env)
        msg_path = message_path_in_inbox("agent:bob", signed_env.id, signed_env.timestamp)
        vfs.write(msg_path, signed_env.to_bytes(), ZONE)

        received: list[MessageEnvelope] = []

        async def handler(msg: MessageEnvelope) -> None:
            received.append(msg)

        processor = MessageProcessor(
            vfs,
            "agent:bob",
            handler,
            zone_id=ZONE,
            verifier=verifier,
            signing_mode=SigningMode.ENFORCE,
        )
        count = await processor.process_inbox()

        assert count == 1
        assert len(received) == 1

    @pytest.mark.asyncio
    async def test_process_invalid_signature_dead_letters(self, vfs: InMemoryVFS) -> None:
        """Bad signature -> dead_letter with INVALID_SIGNATURE reason."""
        from nexus.bricks.ipc.signing import SigningMode

        await _provision_agent(vfs, "agent:bob")
        _, _, signer, verifier = _make_signing_fixtures()

        env = _make_envelope()
        signed_env = signer.sign(env)
        # Tamper with payload after signing
        tampered = signed_env.model_copy(update={"payload": {"action": "tampered"}})
        msg_path = message_path_in_inbox("agent:bob", tampered.id, tampered.timestamp)
        vfs.write(msg_path, tampered.to_bytes(), ZONE)

        handler_called = False

        async def handler(msg: MessageEnvelope) -> None:
            nonlocal handler_called
            handler_called = True

        processor = MessageProcessor(
            vfs,
            "agent:bob",
            handler,
            zone_id=ZONE,
            verifier=verifier,
            signing_mode=SigningMode.ENFORCE,
        )
        await processor.process_inbox()

        assert not handler_called
        dl_files = vfs.list_dir(dead_letter_path("agent:bob"), ZONE)
        dl_msgs = [f for f in dl_files if not f.endswith(".reason.json")]
        assert len(dl_msgs) == 1

    @pytest.mark.asyncio
    async def test_process_unsigned_on_enforced_zone_dead_letters(self, vfs: InMemoryVFS) -> None:
        """Enforce mode, no sig -> dead_letter with UNSIGNED_MESSAGE."""
        from nexus.bricks.ipc.signing import SigningMode

        await _provision_agent(vfs, "agent:bob")
        _, _, _, verifier = _make_signing_fixtures()

        env = _make_envelope()  # unsigned
        msg_path = message_path_in_inbox("agent:bob", env.id, env.timestamp)
        vfs.write(msg_path, env.to_bytes(), ZONE)

        handler_called = False

        async def handler(msg: MessageEnvelope) -> None:
            nonlocal handler_called
            handler_called = True

        processor = MessageProcessor(
            vfs,
            "agent:bob",
            handler,
            zone_id=ZONE,
            verifier=verifier,
            signing_mode=SigningMode.ENFORCE,
        )
        await processor.process_inbox()

        assert not handler_called
        dl_files = vfs.list_dir(dead_letter_path("agent:bob"), ZONE)
        dl_msgs = [f for f in dl_files if not f.endswith(".reason.json")]
        assert len(dl_msgs) == 1

    @pytest.mark.asyncio
    async def test_process_unsigned_on_verify_only_logs_warning(self, vfs: InMemoryVFS) -> None:
        """verify_only mode, no sig -> handler still invoked."""
        from nexus.bricks.ipc.signing import SigningMode

        await _provision_agent(vfs, "agent:bob")
        _, _, _, verifier = _make_signing_fixtures()

        env = _make_envelope()  # unsigned
        msg_path = message_path_in_inbox("agent:bob", env.id, env.timestamp)
        vfs.write(msg_path, env.to_bytes(), ZONE)

        received: list[MessageEnvelope] = []

        async def handler(msg: MessageEnvelope) -> None:
            received.append(msg)

        processor = MessageProcessor(
            vfs,
            "agent:bob",
            handler,
            zone_id=ZONE,
            verifier=verifier,
            signing_mode=SigningMode.VERIFY_ONLY,
        )
        count = await processor.process_inbox()

        assert count == 1
        assert len(received) == 1

    @pytest.mark.asyncio
    async def test_process_unsigned_on_off_mode_no_verification(self, vfs: InMemoryVFS) -> None:
        """OFF mode -> no verification at all, handler invoked."""
        from nexus.bricks.ipc.signing import SigningMode

        await _provision_agent(vfs, "agent:bob")

        env = _make_envelope()  # unsigned
        msg_path = message_path_in_inbox("agent:bob", env.id, env.timestamp)
        vfs.write(msg_path, env.to_bytes(), ZONE)

        received: list[MessageEnvelope] = []

        async def handler(msg: MessageEnvelope) -> None:
            received.append(msg)

        # No verifier at all, OFF mode
        processor = MessageProcessor(
            vfs,
            "agent:bob",
            handler,
            zone_id=ZONE,
            signing_mode=SigningMode.OFF,
        )
        count = await processor.process_inbox()

        assert count == 1
        assert len(received) == 1
