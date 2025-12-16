"""Pydantic models for subscription API."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator


class SubscriptionCreate(BaseModel):
    """Request model for creating a subscription."""

    url: str = Field(..., description="Webhook URL to receive events")
    event_types: list[str] = Field(
        default=["file_write", "file_delete", "file_rename"],
        description="Event types to subscribe to",
    )
    patterns: list[str] | None = Field(
        default=None,
        description="Glob patterns to filter file paths (e.g., '/workspace/**/*')",
    )
    secret: str | None = Field(
        default=None,
        description="HMAC secret for signing webhook payloads",
    )
    name: str | None = Field(default=None, description="Human-readable name")
    description: str | None = Field(default=None, description="Description")
    metadata: dict[str, Any] | None = Field(
        default=None,
        description="Custom metadata to include in webhook payloads",
    )

    @field_validator("url")
    @classmethod
    def validate_url(cls, v: str) -> str:
        if not v.startswith(("http://", "https://")):
            raise ValueError("URL must start with http:// or https://")
        return v

    @field_validator("event_types")
    @classmethod
    def validate_event_types(cls, v: list[str]) -> list[str]:
        valid_events = {"file_write", "file_delete", "file_rename", "metadata_change"}
        for event in v:
            if event not in valid_events:
                raise ValueError(f"Invalid event type: {event}. Valid: {valid_events}")
        return v


class SubscriptionUpdate(BaseModel):
    """Request model for updating a subscription."""

    url: str | None = None
    event_types: list[str] | None = None
    patterns: list[str] | None = None
    secret: str | None = None
    name: str | None = None
    description: str | None = None
    metadata: dict[str, Any] | None = None
    enabled: bool | None = None

    @field_validator("url")
    @classmethod
    def validate_url(cls, v: str | None) -> str | None:
        if v is not None and not v.startswith(("http://", "https://")):
            raise ValueError("URL must start with http:// or https://")
        return v

    @field_validator("event_types")
    @classmethod
    def validate_event_types(cls, v: list[str] | None) -> list[str] | None:
        if v is not None:
            valid_events = {"file_write", "file_delete", "file_rename", "metadata_change"}
            for event in v:
                if event not in valid_events:
                    raise ValueError(f"Invalid event type: {event}. Valid: {valid_events}")
        return v


class Subscription(BaseModel):
    """Response model for a subscription."""

    id: str = Field(..., description="Subscription ID")
    tenant_id: str = Field(..., description="Tenant ID")
    url: str = Field(..., description="Webhook URL")
    event_types: list[str] = Field(..., description="Subscribed event types")
    patterns: list[str] | None = Field(None, description="File path patterns")
    name: str | None = None
    description: str | None = None
    metadata: dict[str, Any] | None = None
    enabled: bool = True
    last_delivery_at: datetime | None = None
    last_delivery_status: str | None = None
    consecutive_failures: int = 0
    created_at: datetime
    updated_at: datetime
    created_by: str | None = None

    model_config = {"from_attributes": True}


class WebhookPayload(BaseModel):
    """Payload sent to webhook endpoints."""

    id: str = Field(..., description="Unique event ID")
    event: str = Field(..., description="Event type (file_write, file_delete, etc.)")
    timestamp: datetime = Field(..., description="When the event occurred")
    data: dict[str, Any] = Field(..., description="Event data")
    subscription: SubscriptionInfo = Field(..., description="Subscription info")


class SubscriptionInfo(BaseModel):
    """Subscription info included in webhook payloads."""

    id: str
    metadata: dict[str, Any] | None = None


class WebhookDelivery(BaseModel):
    """Record of a webhook delivery attempt."""

    delivery_id: str
    subscription_id: str
    event_id: str
    event_type: str
    status: str  # pending, success, failed
    status_code: int | None = None
    response_body: str | None = None
    error_message: str | None = None
    attempt: int = 1
    delivered_at: datetime | None = None
    created_at: datetime
