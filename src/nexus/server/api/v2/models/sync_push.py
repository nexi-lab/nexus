"""Sync push request/response models for API v2 (Issue #1129)."""

from __future__ import annotations

from typing import Any

from pydantic import Field

from nexus.server.api.v2.models.base import ApiModel


class SyncPushResponse(ApiModel):
    """Response for POST /api/v2/sync/mounts/{mount_point}/push."""

    mount_point: str
    changes_pushed: int = Field(default=0, description="Number of changes successfully pushed")
    changes_failed: int = Field(default=0, description="Number of changes that failed")
    conflicts_detected: int = Field(default=0, description="Number of conflicts detected")
    metrics: dict[str, Any] = Field(
        default_factory=dict, description="Detailed per-backend metrics snapshot"
    )
