"""Shared messaging primitives for A2A and IPC.

Re-exports the canonical Part types from ``a2a.models`` and defines
common metadata fields used across message formats.

Moved from ``core/`` to ``a2a/`` because this module depends on
``nexus.a2a.models`` — the kernel must not import from services.

See: Decision 2 / #1587
"""

from datetime import UTC, datetime

from pydantic import BaseModel, Field

# Re-export Part types from a2a.models (canonical definitions)
from nexus.a2a.models import DataPart, FilePart, Part, TextPart

__all__ = [
    "DataPart",
    "FilePart",
    "MessageMetadata",
    "Part",
    "TextPart",
]

class MessageMetadata(BaseModel):
    """Common metadata fields shared across message formats.

    Used by both A2A Messages and IPC MessageEnvelopes.
    """

    correlation_id: str | None = None
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    ttl_seconds: int | None = None
    version: str = "1.0"
