"""Unit tests for MessageEnvelope — validation, serialization, TTL."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from nexus.ipc.envelope import MessageEnvelope, MessageType
from nexus.ipc.exceptions import EnvelopeValidationError


class TestEnvelopeCreation:
    """Tests for creating valid envelopes."""

    def test_create_minimal(self) -> None:
        env = MessageEnvelope(
            sender="agent:alice",
            recipient="agent:bob",
            type=MessageType.TASK,
        )
        assert env.sender == "agent:alice"
        assert env.recipient == "agent:bob"
        assert env.type == MessageType.TASK
        assert env.nexus_message == "1.0"
        assert env.id.startswith("msg_")
        assert env.correlation_id is None
        assert env.ttl_seconds is None
        assert env.payload == {}

    def test_create_full(self) -> None:
        ts = datetime(2026, 2, 12, 10, 0, 0, tzinfo=UTC)
        env = MessageEnvelope(
            sender="agent:analyst",
            recipient="agent:reviewer",
            type=MessageType.TASK,
            id="msg_custom01",
            correlation_id="task_42",
            timestamp=ts,
            ttl_seconds=3600,
            payload={"action": "review_document"},
        )
        assert env.id == "msg_custom01"
        assert env.correlation_id == "task_42"
        assert env.ttl_seconds == 3600
        assert env.payload["action"] == "review_document"

    def test_frozen_immutability(self) -> None:
        env = MessageEnvelope(
            sender="agent:a",
            recipient="agent:b",
            type=MessageType.TASK,
        )
        with pytest.raises(ValidationError):
            env.sender = "agent:c"  # type: ignore[misc]

    def test_create_via_alias(self) -> None:
        """Ensure we can create from JSON with 'from'/'to' keys."""
        data = {
            "from": "agent:alice",
            "to": "agent:bob",
            "type": "task",
        }
        env = MessageEnvelope.model_validate(data)
        assert env.sender == "agent:alice"
        assert env.recipient == "agent:bob"


class TestEnvelopeValidation:
    """Tests for envelope field validation."""

    def test_empty_sender_rejected(self) -> None:
        with pytest.raises(ValidationError):
            MessageEnvelope(
                sender="",
                recipient="agent:bob",
                type=MessageType.TASK,
            )

    def test_whitespace_sender_rejected(self) -> None:
        with pytest.raises(ValidationError):
            MessageEnvelope(
                sender="   ",
                recipient="agent:bob",
                type=MessageType.TASK,
            )

    def test_empty_recipient_rejected(self) -> None:
        with pytest.raises(ValidationError):
            MessageEnvelope(
                sender="agent:alice",
                recipient="",
                type=MessageType.TASK,
            )

    def test_negative_ttl_rejected(self) -> None:
        with pytest.raises(ValidationError):
            MessageEnvelope(
                sender="agent:a",
                recipient="agent:b",
                type=MessageType.TASK,
                ttl_seconds=-1,
            )

    def test_zero_ttl_rejected(self) -> None:
        with pytest.raises(ValidationError):
            MessageEnvelope(
                sender="agent:a",
                recipient="agent:b",
                type=MessageType.TASK,
                ttl_seconds=0,
            )

    def test_invalid_type_rejected(self) -> None:
        with pytest.raises(ValidationError):
            MessageEnvelope(
                sender="agent:a",
                recipient="agent:b",
                type="invalid_type",  # type: ignore[arg-type]
            )


class TestEnvelopeSerialization:
    """Tests for to_bytes/from_bytes round-trip."""

    def test_round_trip(self) -> None:
        original = MessageEnvelope(
            sender="agent:alice",
            recipient="agent:bob",
            type=MessageType.RESPONSE,
            correlation_id="task_1",
            ttl_seconds=600,
            payload={"result": "approved"},
        )
        data = original.to_bytes()
        restored = MessageEnvelope.from_bytes(data)
        assert restored.sender == original.sender
        assert restored.recipient == original.recipient
        assert restored.type == original.type
        assert restored.correlation_id == original.correlation_id
        assert restored.ttl_seconds == original.ttl_seconds
        assert restored.payload == original.payload
        assert restored.id == original.id

    def test_json_uses_from_to_aliases(self) -> None:
        env = MessageEnvelope(
            sender="agent:alice",
            recipient="agent:bob",
            type=MessageType.TASK,
        )
        data = json.loads(env.to_bytes())
        assert "from" in data
        assert "to" in data
        assert "sender" not in data
        assert "recipient" not in data

    def test_from_bytes_invalid_json(self) -> None:
        with pytest.raises(EnvelopeValidationError, match="Invalid JSON"):
            MessageEnvelope.from_bytes(b"not json {{{")

    def test_from_bytes_missing_fields(self) -> None:
        with pytest.raises(EnvelopeValidationError):
            MessageEnvelope.from_bytes(json.dumps({"nexus_message": "1.0"}).encode())


class TestEnvelopeTTL:
    """Tests for TTL expiry logic."""

    def test_no_ttl_never_expires(self) -> None:
        env = MessageEnvelope(
            sender="agent:a",
            recipient="agent:b",
            type=MessageType.TASK,
        )
        # Even far in the future, no TTL = never expires
        future = datetime.now(UTC) + timedelta(days=365)
        assert not env.is_expired(now=future)

    def test_within_ttl(self) -> None:
        ts = datetime(2026, 2, 12, 10, 0, 0, tzinfo=UTC)
        env = MessageEnvelope(
            sender="agent:a",
            recipient="agent:b",
            type=MessageType.TASK,
            timestamp=ts,
            ttl_seconds=3600,
        )
        check_time = ts + timedelta(seconds=1800)  # 30 min later
        assert not env.is_expired(now=check_time)

    def test_past_ttl(self) -> None:
        ts = datetime(2026, 2, 12, 10, 0, 0, tzinfo=UTC)
        env = MessageEnvelope(
            sender="agent:a",
            recipient="agent:b",
            type=MessageType.TASK,
            timestamp=ts,
            ttl_seconds=3600,
        )
        check_time = ts + timedelta(seconds=3601)  # 1s past TTL
        assert env.is_expired(now=check_time)

    def test_exactly_at_ttl_boundary(self) -> None:
        ts = datetime(2026, 2, 12, 10, 0, 0, tzinfo=UTC)
        env = MessageEnvelope(
            sender="agent:a",
            recipient="agent:b",
            type=MessageType.TASK,
            timestamp=ts,
            ttl_seconds=3600,
        )
        check_time = ts + timedelta(seconds=3600)  # exactly at boundary
        assert not env.is_expired(now=check_time)
