"""Pydantic schemas for X (Twitter) write operations. Phase 4 (#3148)."""

from pydantic import BaseModel, Field


class CreateTweetSchema(BaseModel):
    """Schema for creating a new tweet."""

    agent_intent: str = Field(..., min_length=10, description="Why this tweet is being posted")
    text: str = Field(..., min_length=1, max_length=280, description="Tweet text")
    reply_to: str | None = Field(default=None, description="Tweet ID to reply to")
    quote_tweet_id: str | None = Field(default=None, description="Tweet ID to quote")
    user_confirmed: bool = Field(default=False, description="User confirmed posting (irreversible)")


class DeleteTweetSchema(BaseModel):
    """Schema for deleting a tweet."""

    agent_intent: str = Field(..., min_length=10, description="Why this tweet is being deleted")
    tweet_id: str = Field(..., description="ID of tweet to delete")
    user_confirmed: bool = Field(default=False, description="User confirmed deletion")
