"""Bidirectional conversion between A2A Pydantic models and proto messages.

Provides functions to convert between the existing Pydantic-based A2A models
(``nexus.a2a.models``) and the gRPC protobuf messages (``nexus.a2a.a2a_pb2``).
"""

from __future__ import annotations

import base64
from datetime import UTC, datetime
from typing import Any

from google.protobuf import struct_pb2, timestamp_pb2

from nexus.a2a import a2a_pb2, models

# ============================================================================
# TaskState mapping
# ============================================================================

_PYDANTIC_TO_PROTO_STATE: dict[models.TaskState, int] = {
    models.TaskState.SUBMITTED: a2a_pb2.TASK_STATE_SUBMITTED,
    models.TaskState.WORKING: a2a_pb2.TASK_STATE_WORKING,
    models.TaskState.INPUT_REQUIRED: a2a_pb2.TASK_STATE_INPUT_REQUIRED,
    models.TaskState.COMPLETED: a2a_pb2.TASK_STATE_COMPLETED,
    models.TaskState.FAILED: a2a_pb2.TASK_STATE_FAILED,
    models.TaskState.CANCELED: a2a_pb2.TASK_STATE_CANCELED,
    models.TaskState.REJECTED: a2a_pb2.TASK_STATE_REJECTED,
}

_PROTO_TO_PYDANTIC_STATE: dict[int, models.TaskState] = {
    v: k for k, v in _PYDANTIC_TO_PROTO_STATE.items()
}


def task_state_to_proto(state: models.TaskState) -> int:
    """Convert a Pydantic TaskState to its proto enum value."""
    return _PYDANTIC_TO_PROTO_STATE[state]


def task_state_from_proto(value: int) -> models.TaskState:
    """Convert a proto TaskState enum value to a Pydantic TaskState."""
    return _PROTO_TO_PYDANTIC_STATE[value]


# ============================================================================
# Metadata / Struct helpers
# ============================================================================


def _dict_to_struct(d: dict[str, Any] | None) -> struct_pb2.Struct | None:
    """Convert a dict to a google.protobuf.Struct, or None."""
    if not d:
        return None
    s = struct_pb2.Struct()
    s.update(d)
    return s


def _struct_to_dict(s: struct_pb2.Struct | None) -> dict[str, Any] | None:
    """Convert a google.protobuf.Struct to a dict, or None."""
    if s is None:
        return None
    result = dict(s)
    return result if result else None


# ============================================================================
# Timestamp helpers
# ============================================================================


def _datetime_to_timestamp(dt: datetime | None) -> timestamp_pb2.Timestamp | None:
    """Convert a datetime to a google.protobuf.Timestamp, or None."""
    if dt is None:
        return None
    ts = timestamp_pb2.Timestamp()
    ts.FromDatetime(dt)
    return ts


def _timestamp_to_datetime(ts: timestamp_pb2.Timestamp | None) -> datetime | None:
    """Convert a google.protobuf.Timestamp to a datetime, or None."""
    if ts is None:
        return None
    return ts.ToDatetime(tzinfo=UTC)


# ============================================================================
# Part conversion
# ============================================================================


def part_to_proto(part: models.TextPart | models.FilePart | models.DataPart) -> a2a_pb2.Part:
    """Convert a Pydantic Part to a proto Part."""
    pb = a2a_pb2.Part()
    meta = _dict_to_struct(part.metadata)
    if meta is not None:
        pb.metadata.CopyFrom(meta)

    if isinstance(part, models.TextPart):
        pb.text = part.text
    elif isinstance(part, models.FilePart):
        fc = part.file
        if fc.bytes is not None:
            pb.raw = base64.b64decode(fc.bytes)
        elif fc.url is not None:
            pb.url = fc.url
        if fc.name:
            pb.filename = fc.name
        if fc.mimeType:
            pb.media_type = fc.mimeType
    elif isinstance(part, models.DataPart):
        data_struct = struct_pb2.Struct()
        data_struct.update(part.data)
        pb.data.CopyFrom(struct_pb2.Value(struct_value=data_struct))

    return pb


def part_from_proto(pb: a2a_pb2.Part) -> models.TextPart | models.FilePart | models.DataPart:
    """Convert a proto Part to a Pydantic Part."""
    metadata = _struct_to_dict(pb.metadata) if pb.HasField("metadata") else None
    content_type = pb.WhichOneof("content")

    if content_type == "text":
        return models.TextPart(text=pb.text, metadata=metadata)
    elif content_type == "data":
        # Data is a google.protobuf.Value wrapping a Struct
        data_value = pb.data
        data_dict = dict(data_value.struct_value) if data_value.HasField("struct_value") else {}
        return models.DataPart(data=data_dict, metadata=metadata)
    elif content_type == "raw":
        fc = models.FileContent(
            bytes=base64.b64encode(pb.raw).decode("ascii"),
            name=pb.filename or None,
            mimeType=pb.media_type or None,
        )
        return models.FilePart(file=fc, metadata=metadata)
    elif content_type == "url":
        fc = models.FileContent(
            url=pb.url,
            name=pb.filename or None,
            mimeType=pb.media_type or None,
        )
        return models.FilePart(file=fc, metadata=metadata)
    else:
        # Fallback: empty text part
        return models.TextPart(text="", metadata=metadata)


# ============================================================================
# Message conversion
# ============================================================================


def message_to_proto(msg: models.Message) -> a2a_pb2.Message:
    """Convert a Pydantic Message to a proto Message."""
    pb = a2a_pb2.Message(role=msg.role)
    for part in msg.parts:
        pb.parts.append(part_to_proto(part))
    meta = _dict_to_struct(msg.metadata)
    if meta is not None:
        pb.metadata.CopyFrom(meta)
    return pb


def message_from_proto(pb: a2a_pb2.Message) -> models.Message:
    """Convert a proto Message to a Pydantic Message."""
    parts = [part_from_proto(p) for p in pb.parts]
    metadata = _struct_to_dict(pb.metadata) if pb.HasField("metadata") else None
    return models.Message(role=pb.role, parts=parts, metadata=metadata)


# ============================================================================
# Artifact conversion
# ============================================================================


def artifact_to_proto(art: models.Artifact) -> a2a_pb2.Artifact:
    """Convert a Pydantic Artifact to a proto Artifact."""
    pb = a2a_pb2.Artifact(artifact_id=art.artifactId)
    if art.name:
        pb.name = art.name
    if art.description:
        pb.description = art.description
    for part in art.parts:
        pb.parts.append(part_to_proto(part))
    meta = _dict_to_struct(art.metadata)
    if meta is not None:
        pb.metadata.CopyFrom(meta)
    return pb


def artifact_from_proto(pb: a2a_pb2.Artifact) -> models.Artifact:
    """Convert a proto Artifact to a Pydantic Artifact."""
    parts = [part_from_proto(p) for p in pb.parts]
    metadata = _struct_to_dict(pb.metadata) if pb.HasField("metadata") else None
    return models.Artifact(
        artifactId=pb.artifact_id,
        name=pb.name or None,
        description=pb.description or None,
        parts=parts,
        metadata=metadata,
    )


# ============================================================================
# TaskStatus conversion
# ============================================================================


def task_status_to_proto(status: models.TaskStatus) -> a2a_pb2.TaskStatus:
    """Convert a Pydantic TaskStatus to a proto TaskStatus."""
    pb = a2a_pb2.TaskStatus(state=task_state_to_proto(status.state))
    if status.message is not None:
        pb.message.CopyFrom(message_to_proto(status.message))
    ts = _datetime_to_timestamp(status.timestamp)
    if ts is not None:
        pb.timestamp.CopyFrom(ts)
    return pb


def task_status_from_proto(pb: a2a_pb2.TaskStatus) -> models.TaskStatus:
    """Convert a proto TaskStatus to a Pydantic TaskStatus."""
    msg = message_from_proto(pb.message) if pb.HasField("message") else None
    ts = _timestamp_to_datetime(pb.timestamp) if pb.HasField("timestamp") else None
    return models.TaskStatus(
        state=task_state_from_proto(pb.state),
        message=msg,
        timestamp=ts,
    )


# ============================================================================
# Task conversion
# ============================================================================


def task_to_proto(task: models.Task) -> a2a_pb2.Task:
    """Convert a Pydantic Task to a proto Task."""
    pb = a2a_pb2.Task(id=task.id)
    if task.contextId:
        pb.context_id = task.contextId
    pb.status.CopyFrom(task_status_to_proto(task.status))
    for art in task.artifacts:
        pb.artifacts.append(artifact_to_proto(art))
    for msg in task.history:
        pb.history.append(message_to_proto(msg))
    meta = _dict_to_struct(task.metadata)
    if meta is not None:
        pb.metadata.CopyFrom(meta)
    return pb


def task_from_proto(pb: a2a_pb2.Task) -> models.Task:
    """Convert a proto Task to a Pydantic Task."""
    artifacts = [artifact_from_proto(a) for a in pb.artifacts]
    history = [message_from_proto(m) for m in pb.history]
    metadata = _struct_to_dict(pb.metadata) if pb.HasField("metadata") else None
    return models.Task(
        id=pb.id,
        contextId=pb.context_id or None,
        status=task_status_from_proto(pb.status),
        artifacts=artifacts,
        history=history,
        metadata=metadata,
    )


# ============================================================================
# Request conversion helpers
# ============================================================================


def send_request_from_proto(
    pb: a2a_pb2.SendMessageRequest,
) -> tuple[models.Message, dict[str, Any] | None]:
    """Convert a proto SendMessageRequest to a (Message, metadata) tuple."""
    msg = message_from_proto(pb.message)
    metadata = _struct_to_dict(pb.metadata) if pb.HasField("metadata") else None
    return msg, metadata
