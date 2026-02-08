"""Message Gateway types.

Boardroom message model for human/agent communication.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class Message(BaseModel):
    """Boardroom message for human/agent communication.

    Multi-party conversation model where all participants share a common "boardroom".
    Key principles:
    - No request/response pairing - messages are independent
    - Everyone sees everything - all messages visible to all participants
    - Threading via parent_id - replies link to parent message
    - @mention hints via target - suggest intended recipient (not enforced)
    """

    id: str = Field(..., description="Unique message ID")
    text: str = Field(..., description="Message content")
    user: str = Field(..., description="Sender (human ID or agent ID)")
    role: Literal["human", "agent"] = Field(..., description="Who sent this message")
    session_id: str = Field(..., description="Room/conversation key")
    channel: str = Field(..., description="Platform (discord, slack, etc.)")
    ts: str = Field(..., description="ISO8601 timestamp")

    parent_id: str | None = Field(None, description="Reply-to for threading")
    target: str | None = Field(None, description="@mention hint (not enforced)")
    metadata: dict[str, Any] = Field(default_factory=dict, description="Extensible context")

    def to_jsonl(self) -> str:
        """Serialize to JSONL format (single line JSON)."""
        return self.model_dump_json()

    @classmethod
    def from_jsonl(cls, line: str) -> Message:
        """Deserialize from JSONL format."""
        return cls.model_validate_json(line)
