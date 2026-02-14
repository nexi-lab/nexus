"""Pydantic models for the Lock API (#1186, #1288).

Extracted from fastapi_server.py during monolith decomposition.
"""

from __future__ import annotations

import os
from typing import Literal

from pydantic import BaseModel, Field

# Maximum TTL: 24 hours. Configurable via NEXUS_LOCK_MAX_TTL env var.
LOCK_MAX_TTL = float(os.environ.get("NEXUS_LOCK_MAX_TTL", "86400"))


class LockAcquireRequest(BaseModel):
    """Request model for acquiring a lock."""

    path: str
    timeout: float = Field(default=30.0, ge=0, le=3600, description="Max seconds to wait")
    ttl: float = Field(default=30.0, ge=1, le=LOCK_MAX_TTL, description="Lock TTL in seconds")
    max_holders: int = Field(default=1, ge=1, le=10000, description="1=mutex, >1=semaphore")
    blocking: bool = True  # If false, return immediately without waiting


class LockHolderResponse(BaseModel):
    """Information about a single lock holder."""

    lock_id: str
    holder_info: str = ""
    acquired_at: float  # Unix timestamp
    expires_at: float  # Unix timestamp


class LockInfoMutex(BaseModel):
    """Lock info for a mutex (exclusive) lock."""

    mode: Literal["mutex"] = "mutex"
    max_holders: Literal[1] = 1
    lock_id: str  # The single holder's lock ID
    holder_info: str = ""
    acquired_at: float
    expires_at: float
    fence_token: int


class LockInfoSemaphore(BaseModel):
    """Lock info for a semaphore (shared) lock."""

    mode: Literal["semaphore"] = "semaphore"
    max_holders: int
    holders: list[LockHolderResponse]
    current_holders: int
    fence_token: int


class LockResponse(BaseModel):
    """Response model for lock operations."""

    lock_id: str
    path: str
    mode: Literal["mutex", "semaphore"]
    max_holders: int
    ttl: int
    expires_at: str  # ISO 8601 timestamp
    fence_token: int


class LockStatusResponse(BaseModel):
    """Response model for lock status queries."""

    path: str
    locked: bool
    lock_info: LockInfoMutex | LockInfoSemaphore | None = None


class LockExtendRequest(BaseModel):
    """Request model for extending a lock."""

    lock_id: str
    ttl: float = Field(default=30.0, ge=1, le=LOCK_MAX_TTL, description="New TTL in seconds")


class LockListResponse(BaseModel):
    """Response model for listing locks."""

    locks: list[LockInfoMutex | LockInfoSemaphore]
    count: int
