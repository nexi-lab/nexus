"""Unit tests for A2A <-> IPC message adapters."""


from nexus.a2a.messaging_adapters import (
    a2a_message_to_envelope,
    envelope_to_a2a_message,
)
from nexus.a2a.models import (
    DataPart,
    FileContent,
    FilePart,
    Message,
    TextPart,
)
from nexus.ipc.envelope import MessageEnvelope, MessageType


class TestA2AToEnvelope:
    def test_text_parts(self) -> None:
        msg = Message(role="user", parts=[TextPart(text="hello")])
        env = a2a_message_to_envelope(msg, sender="agent:alice", recipient="agent:bob")
        assert env.sender == "agent:alice"
        assert env.recipient == "agent:bob"
        assert env.type == MessageType.TASK
        assert env.payload["role"] == "user"
        assert len(env.payload["parts"]) == 1
        assert env.payload["parts"][0]["type"] == "text"

    def test_file_parts(self) -> None:
        msg = Message(
            role="agent",
            parts=[FilePart(file=FileContent(name="out.txt", mimeType="text/plain"))],
        )
        env = a2a_message_to_envelope(msg, sender="agent:worker", recipient="agent:manager")
        assert env.payload["parts"][0]["type"] == "file"

    def test_data_parts(self) -> None:
        msg = Message(role="agent", parts=[DataPart(data={"result": 42})])
        env = a2a_message_to_envelope(msg, sender="agent:calc", recipient="agent:ui")
        assert env.payload["parts"][0]["type"] == "data"

    def test_preserves_metadata(self) -> None:
        msg = Message(
            role="user",
            parts=[TextPart(text="hi")],
            metadata={"source": "test"},
        )
        env = a2a_message_to_envelope(msg, sender="agent:a", recipient="agent:b")
        assert env.payload["metadata"] == {"source": "test"}

    def test_with_correlation_id(self) -> None:
        msg = Message(role="user", parts=[TextPart(text="hi")])
        env = a2a_message_to_envelope(
            msg,
            sender="agent:a",
            recipient="agent:b",
            correlation_id="task-123",
        )
        assert env.correlation_id == "task-123"

    def test_with_ttl(self) -> None:
        msg = Message(role="user", parts=[TextPart(text="hi")])
        env = a2a_message_to_envelope(
            msg,
            sender="agent:a",
            recipient="agent:b",
            ttl_seconds=3600,
        )
        assert env.ttl_seconds == 3600


class TestEnvelopeToA2A:
    def test_basic(self) -> None:
        env = MessageEnvelope(
            sender="agent:alice",
            recipient="agent:bob",
            type=MessageType.TASK,
            payload={
                "role": "user",
                "parts": [{"type": "text", "text": "hello"}],
            },
        )
        msg = envelope_to_a2a_message(env)
        assert msg.role == "user"
        assert len(msg.parts) == 1
        assert isinstance(msg.parts[0], TextPart)
        assert msg.parts[0].text == "hello"

    def test_with_typed_parts(self) -> None:
        env = MessageEnvelope(
            sender="agent:a",
            recipient="agent:b",
            type=MessageType.RESPONSE,
            payload={
                "role": "agent",
                "parts": [
                    {"type": "text", "text": "result"},
                    {"type": "data", "data": {"x": 1}},
                ],
            },
        )
        msg = envelope_to_a2a_message(env)
        assert len(msg.parts) == 2
        assert isinstance(msg.parts[0], TextPart)
        assert isinstance(msg.parts[1], DataPart)

    def test_untyped_payload_wraps_as_data_part(self) -> None:
        env = MessageEnvelope(
            sender="agent:a",
            recipient="agent:b",
            type=MessageType.EVENT,
            payload={"action": "notify", "value": 42},
        )
        msg = envelope_to_a2a_message(env, role="agent")
        assert msg.role == "agent"
        assert len(msg.parts) == 1
        assert isinstance(msg.parts[0], DataPart)
        assert msg.parts[0].data["action"] == "notify"

    def test_explicit_role(self) -> None:
        env = MessageEnvelope(
            sender="agent:a",
            recipient="agent:b",
            type=MessageType.TASK,
            payload={
                "role": "user",
                "parts": [{"type": "text", "text": "hi"}],
            },
        )
        # Explicit role overrides payload role
        msg = envelope_to_a2a_message(env, role="agent")
        assert msg.role == "agent"


class TestRoundtrip:
    def test_a2a_to_envelope_to_a2a(self) -> None:
        original = Message(
            role="user",
            parts=[
                TextPart(text="hello"),
                DataPart(data={"key": "value"}),
            ],
        )
        env = a2a_message_to_envelope(original, sender="agent:a", recipient="agent:b")
        restored = envelope_to_a2a_message(env)
        assert restored.role == original.role
        assert len(restored.parts) == len(original.parts)

    def test_envelope_to_a2a_to_envelope(self) -> None:
        original_env = MessageEnvelope(
            sender="agent:x",
            recipient="agent:y",
            type=MessageType.TASK,
            payload={
                "role": "agent",
                "parts": [{"type": "text", "text": "result"}],
                "metadata": {"source": "test"},
            },
        )
        msg = envelope_to_a2a_message(original_env)
        restored_env = a2a_message_to_envelope(msg, sender="agent:x", recipient="agent:y")
        assert restored_env.payload["role"] == "agent"
        assert restored_env.payload["parts"][0]["text"] == "result"
