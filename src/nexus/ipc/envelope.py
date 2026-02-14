"""Message envelope model for filesystem-as-IPC.

The envelope wraps every message written to an agent's inbox.
Designed per KERNEL-ARCHITECTURE.md.

Wire format uses ``"from"`` and ``"to"`` field names (matching the design
doc), while Python code uses ``sender`` and ``recipient`` to avoid
clashing with the ``from`` keyword.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator


class MessageType(StrEnum):
    """Types of inter-agent messages."""

    TASK = "task"
    RESPONSE = "response"
    EVENT = "event"
    CANCEL = "cancel"


class MessageEnvelope(BaseModel):
    """Immutable message envelope for agent-to-agent communication.

    Fields ``sender`` and ``recipient`` serialize to/from the JSON keys
    ``"from"`` and ``"to"`` respectively, via Pydantic aliases.

    Example JSON on disk::

        {
            "nexus_message": "1.0",
            "id": "msg_7f3a9b2c",
            "from": "agent:analyst",
            "to": "agent:reviewer",
            "type": "task",
            "correlation_id": "task_42",
            "timestamp": "2026-02-12T10:00:00Z",
            "ttl_seconds": 3600,
            "payload": {"action": "review_document"}
        }
    """

    model_config = ConfigDict(
        frozen=True,
        populate_by_name=True,
        ser_json_timedelta="float",
    )

    nexus_message: str = "1.0"
    id: str = Field(default_factory=lambda: f"msg_{uuid4().hex[:8]}")
    sender: str = Field(alias="from")
    recipient: str = Field(alias="to")
    type: MessageType
    correlation_id: str | None = None
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    ttl_seconds: int | None = None
    payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("sender", "recipient")
    @classmethod
    def _validate_agent_ref(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("Agent reference must be non-empty")
        return v.strip()

    @field_validator("ttl_seconds")
    @classmethod
    def _validate_ttl(cls, v: int | None) -> int | None:
        if v is not None and v <= 0:
            raise ValueError("ttl_seconds must be positive")
        return v

    def is_expired(self, now: datetime | None = None) -> bool:
        """Check if the message has exceeded its TTL.

        Args:
            now: Current time for testing. Defaults to UTC now.

        Returns:
            True if the message has a TTL and it has expired.
        """
        if self.ttl_seconds is None:
            return False
        now = now or datetime.now(UTC)
        elapsed = (now - self.timestamp).total_seconds()
        return elapsed > self.ttl_seconds

    def to_bytes(self) -> bytes:
        """Serialize to JSON bytes for VFS write."""
        return self.model_dump_json(by_alias=True, indent=2).encode("utf-8")

    @classmethod
    def from_bytes(cls, data: bytes) -> MessageEnvelope:
        """Deserialize from JSON bytes read from VFS.

        Raises:
            EnvelopeValidationError: If the data is not valid JSON or
                fails envelope validation.
        """
        from nexus.ipc.exceptions import EnvelopeValidationError

        try:
            parsed = json.loads(data)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise EnvelopeValidationError(f"Invalid JSON: {exc}") from exc

        try:
            return cls.model_validate(parsed)
        except Exception as exc:
            raise EnvelopeValidationError(str(exc)) from exc
