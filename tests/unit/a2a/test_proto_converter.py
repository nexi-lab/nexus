"""Unit tests for A2A proto converter module.

Tests cover:
- Part conversion (text, file, data) in both directions
- Message conversion (with and without metadata)
- Artifact conversion
- TaskStatus conversion (with timestamps)
- Task conversion (full round-trip)
- Request conversion helpers
- Edge cases (None metadata, empty parts, all part types)
"""

import base64
from datetime import UTC, datetime

import pytest

from nexus.a2a import a2a_pb2
from nexus.a2a.models import (
    Artifact,
    DataPart,
    FileContent,
    FilePart,
    Message,
    Task,
    TaskState,
    TaskStatus,
    TextPart,
)
from nexus.a2a.proto_converter import (
    artifact_from_proto,
    artifact_to_proto,
    message_from_proto,
    message_to_proto,
    part_from_proto,
    part_to_proto,
    send_request_from_proto,
    task_from_proto,
    task_state_from_proto,
    task_state_to_proto,
    task_status_from_proto,
    task_status_to_proto,
    task_to_proto,
)

# ------------------------------------------------------------------
# TaskState mapping
# ------------------------------------------------------------------

class TestTaskStateMapping:
    """Tests for TaskState enum conversion."""

    @pytest.mark.parametrize(
        ("pydantic_state", "proto_value"),
        [
            (TaskState.SUBMITTED, a2a_pb2.TASK_STATE_SUBMITTED),
            (TaskState.WORKING, a2a_pb2.TASK_STATE_WORKING),
            (TaskState.INPUT_REQUIRED, a2a_pb2.TASK_STATE_INPUT_REQUIRED),
            (TaskState.COMPLETED, a2a_pb2.TASK_STATE_COMPLETED),
            (TaskState.FAILED, a2a_pb2.TASK_STATE_FAILED),
            (TaskState.CANCELED, a2a_pb2.TASK_STATE_CANCELED),
            (TaskState.REJECTED, a2a_pb2.TASK_STATE_REJECTED),
        ],
    )
    def test_roundtrip(self, pydantic_state: TaskState, proto_value: int) -> None:
        assert task_state_to_proto(pydantic_state) == proto_value
        assert task_state_from_proto(proto_value) == pydantic_state

# ------------------------------------------------------------------
# Part conversion
# ------------------------------------------------------------------

class TestPartConversion:
    """Tests for Part conversion in both directions."""

    def test_text_part_to_proto(self) -> None:
        part = TextPart(text="Hello, world!")
        pb = part_to_proto(part)

        assert pb.WhichOneof("content") == "text"
        assert pb.text == "Hello, world!"

    def test_text_part_from_proto(self) -> None:
        pb = a2a_pb2.Part(text="Hello, world!")
        part = part_from_proto(pb)

        assert isinstance(part, TextPart)
        assert part.text == "Hello, world!"

    def test_text_part_with_metadata(self) -> None:
        part = TextPart(text="Hello", metadata={"key": "value"})
        pb = part_to_proto(part)
        result = part_from_proto(pb)

        assert isinstance(result, TextPart)
        assert result.text == "Hello"
        assert result.metadata == {"key": "value"}

    def test_file_part_with_bytes_to_proto(self) -> None:
        raw = b"file content"
        encoded = base64.b64encode(raw).decode("ascii")
        part = FilePart(
            file=FileContent(bytes=encoded, name="test.txt", mimeType="text/plain"),
        )
        pb = part_to_proto(part)

        assert pb.WhichOneof("content") == "raw"
        assert pb.raw == raw
        assert pb.filename == "test.txt"
        assert pb.media_type == "text/plain"

    def test_file_part_with_bytes_from_proto(self) -> None:
        raw = b"file content"
        pb = a2a_pb2.Part(raw=raw, filename="test.txt", media_type="text/plain")
        part = part_from_proto(pb)

        assert isinstance(part, FilePart)
        assert part.file.bytes == base64.b64encode(raw).decode("ascii")
        assert part.file.name == "test.txt"
        assert part.file.mimeType == "text/plain"

    def test_file_part_with_url_to_proto(self) -> None:
        part = FilePart(
            file=FileContent(url="https://example.com/file.pdf", name="file.pdf"),
        )
        pb = part_to_proto(part)

        assert pb.WhichOneof("content") == "url"
        assert pb.url == "https://example.com/file.pdf"
        assert pb.filename == "file.pdf"

    def test_file_part_with_url_from_proto(self) -> None:
        pb = a2a_pb2.Part(url="https://example.com/file.pdf", filename="file.pdf")
        part = part_from_proto(pb)

        assert isinstance(part, FilePart)
        assert part.file.url == "https://example.com/file.pdf"
        assert part.file.name == "file.pdf"

    def test_data_part_to_proto(self) -> None:
        part = DataPart(data={"key": "value", "count": 42})
        pb = part_to_proto(part)

        assert pb.WhichOneof("content") == "data"

    def test_data_part_from_proto(self) -> None:
        from google.protobuf import struct_pb2

        data_struct = struct_pb2.Struct()
        data_struct.update({"key": "value"})
        pb = a2a_pb2.Part(data=struct_pb2.Value(struct_value=data_struct))
        part = part_from_proto(pb)

        assert isinstance(part, DataPart)
        assert part.data["key"] == "value"

    def test_part_none_metadata(self) -> None:
        part = TextPart(text="Hello", metadata=None)
        pb = part_to_proto(part)
        result = part_from_proto(pb)

        assert isinstance(result, TextPart)
        assert result.metadata is None

# ------------------------------------------------------------------
# Message conversion
# ------------------------------------------------------------------

class TestMessageConversion:
    """Tests for Message conversion."""

    def test_simple_message_roundtrip(self) -> None:
        msg = Message(role="user", parts=[TextPart(text="Hello")])
        pb = message_to_proto(msg)
        result = message_from_proto(pb)

        assert result.role == "user"
        assert len(result.parts) == 1
        assert isinstance(result.parts[0], TextPart)
        assert result.parts[0].text == "Hello"

    def test_message_with_metadata(self) -> None:
        msg = Message(
            role="agent",
            parts=[TextPart(text="Response")],
            metadata={"session": "abc123"},
        )
        pb = message_to_proto(msg)
        result = message_from_proto(pb)

        assert result.metadata == {"session": "abc123"}

    def test_message_multiple_parts(self) -> None:
        msg = Message(
            role="user",
            parts=[
                TextPart(text="Check this file"),
                FilePart(file=FileContent(url="https://example.com/doc.pdf")),
            ],
        )
        pb = message_to_proto(msg)
        result = message_from_proto(pb)

        assert len(result.parts) == 2
        assert isinstance(result.parts[0], TextPart)
        assert isinstance(result.parts[1], FilePart)

# ------------------------------------------------------------------
# Artifact conversion
# ------------------------------------------------------------------

class TestArtifactConversion:
    """Tests for Artifact conversion."""

    def test_artifact_roundtrip(self) -> None:
        art = Artifact(
            artifactId="art-1",
            name="output.txt",
            description="Generated output",
            parts=[TextPart(text="Hello output")],
            metadata={"format": "text"},
        )
        pb = artifact_to_proto(art)
        result = artifact_from_proto(pb)

        assert result.artifactId == "art-1"
        assert result.name == "output.txt"
        assert result.description == "Generated output"
        assert len(result.parts) == 1
        assert result.metadata == {"format": "text"}

    def test_artifact_minimal(self) -> None:
        art = Artifact(
            artifactId="art-2",
            parts=[TextPart(text="data")],
        )
        pb = artifact_to_proto(art)
        result = artifact_from_proto(pb)

        assert result.artifactId == "art-2"
        assert result.name is None
        assert result.description is None

# ------------------------------------------------------------------
# TaskStatus conversion
# ------------------------------------------------------------------

class TestTaskStatusConversion:
    """Tests for TaskStatus conversion."""

    def test_status_with_timestamp(self) -> None:
        now = datetime(2025, 1, 15, 12, 0, 0, tzinfo=UTC)
        status = TaskStatus(state=TaskState.WORKING, timestamp=now)
        pb = task_status_to_proto(status)
        result = task_status_from_proto(pb)

        assert result.state == TaskState.WORKING
        assert result.timestamp is not None
        assert result.timestamp.year == 2025
        assert result.message is None

    def test_status_with_message(self) -> None:
        msg = Message(role="agent", parts=[TextPart(text="Processing...")])
        status = TaskStatus(state=TaskState.WORKING, message=msg)
        pb = task_status_to_proto(status)
        result = task_status_from_proto(pb)

        assert result.message is not None
        assert result.message.role == "agent"

    def test_status_minimal(self) -> None:
        status = TaskStatus(state=TaskState.SUBMITTED)
        pb = task_status_to_proto(status)
        result = task_status_from_proto(pb)

        assert result.state == TaskState.SUBMITTED
        assert result.message is None
        assert result.timestamp is None

# ------------------------------------------------------------------
# Task conversion
# ------------------------------------------------------------------

class TestTaskConversion:
    """Tests for full Task conversion."""

    def test_task_roundtrip(self) -> None:
        now = datetime(2025, 1, 15, 12, 0, 0, tzinfo=UTC)
        task = Task(
            id="task-1",
            contextId="ctx-1",
            status=TaskStatus(state=TaskState.SUBMITTED, timestamp=now),
            history=[
                Message(role="user", parts=[TextPart(text="Hello")]),
            ],
            metadata={"priority": "high"},
        )
        pb = task_to_proto(task)
        result = task_from_proto(pb)

        assert result.id == "task-1"
        assert result.contextId == "ctx-1"
        assert result.status.state == TaskState.SUBMITTED
        assert len(result.history) == 1
        assert result.metadata == {"priority": "high"}

    def test_task_with_artifacts(self) -> None:
        task = Task(
            id="task-2",
            status=TaskStatus(state=TaskState.COMPLETED),
            artifacts=[
                Artifact(artifactId="a1", parts=[TextPart(text="output")]),
            ],
        )
        pb = task_to_proto(task)
        result = task_from_proto(pb)

        assert len(result.artifacts) == 1
        assert result.artifacts[0].artifactId == "a1"

    def test_task_minimal(self) -> None:
        task = Task(
            id="task-3",
            status=TaskStatus(state=TaskState.SUBMITTED),
        )
        pb = task_to_proto(task)
        result = task_from_proto(pb)

        assert result.id == "task-3"
        assert result.contextId is None
        assert result.artifacts == []
        assert result.history == []
        assert result.metadata is None

# ------------------------------------------------------------------
# Request conversion
# ------------------------------------------------------------------

class TestSendRequestConversion:
    """Tests for SendMessageRequest conversion."""

    def test_basic_request(self) -> None:
        pb = a2a_pb2.SendMessageRequest(
            message=a2a_pb2.Message(
                role="user",
                parts=[a2a_pb2.Part(text="Hello")],
            ),
        )
        msg, metadata = send_request_from_proto(pb)

        assert msg.role == "user"
        assert len(msg.parts) == 1
        assert metadata is None

    def test_request_with_metadata(self) -> None:
        from google.protobuf import struct_pb2

        meta = struct_pb2.Struct()
        meta.update({"session": "abc"})
        pb = a2a_pb2.SendMessageRequest(
            message=a2a_pb2.Message(
                role="user",
                parts=[a2a_pb2.Part(text="Hello")],
            ),
            metadata=meta,
        )
        msg, metadata = send_request_from_proto(pb)

        assert metadata == {"session": "abc"}
