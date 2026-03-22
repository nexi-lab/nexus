"""Pydantic schemas for Slack write operations. Phase 4 (#3148)."""

from pydantic import BaseModel, Field


class SendMessageSchema(BaseModel):
    """Schema for sending a new Slack message."""

    agent_intent: str = Field(..., min_length=10, description="Why this message is being sent")
    channel: str = Field(..., description="Channel ID or name")
    text: str = Field(..., min_length=1, description="Message text (supports Slack markdown)")
    thread_ts: str | None = Field(default=None, description="Thread timestamp for threaded replies")
    unfurl_links: bool = Field(default=True, description="Unfurl links in message")
    user_confirmed: bool = Field(default=False, description="User confirmed sending")


class DeleteMessageSchema(BaseModel):
    """Schema for deleting a Slack message."""

    agent_intent: str = Field(..., min_length=10, description="Why this message is being deleted")
    channel: str = Field(..., description="Channel ID")
    ts: str = Field(..., description="Message timestamp to delete")
    user_confirmed: bool = Field(default=False, description="User confirmed deletion")


class UpdateMessageSchema(BaseModel):
    """Schema for updating an existing Slack message."""

    agent_intent: str = Field(..., min_length=10, description="Why this message is being updated")
    channel: str = Field(..., description="Channel ID")
    ts: str = Field(..., description="Message timestamp to update")
    text: str = Field(..., min_length=1, description="New message text")
    confirm: bool = Field(default=False, description="Explicit confirmation")
