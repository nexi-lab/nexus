"""Pydantic models for health endpoints (#1288).

Extracted from fastapi_server.py during monolith decomposition.
"""

from __future__ import annotations

from pydantic import BaseModel


class HealthResponse(BaseModel):
    """Health check response."""

    status: str
    service: str
    enforce_permissions: bool | None = None
    enforce_zone_isolation: bool | None = None
    has_auth: bool | None = None


class WhoamiResponse(BaseModel):
    """Authentication info response."""

    authenticated: bool
    subject_type: str | None = None
    subject_id: str | None = None
    zone_id: str | None = None
    is_admin: bool = False
    inherit_permissions: bool = True
    user: str | None = None
