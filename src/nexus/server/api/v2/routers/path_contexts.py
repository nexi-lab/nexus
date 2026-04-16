"""Path Contexts API v2 router (Issue #3773).

Admin-managed per-zone path-prefix -> description mappings. Search results
carry the longest-prefix-matching description in their ``context`` field.

Endpoints:
- PUT    /api/v2/path-contexts/       Upsert (admin)
- GET    /api/v2/path-contexts/       List contexts (auth)
- DELETE /api/v2/path-contexts/       Delete one (admin)

Pattern mirrors access_manifests.py.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field, field_validator

from nexus.bricks.search.path_context import PathContextStore
from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.server.dependencies import require_admin, require_auth

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v2/path-contexts", tags=["path_contexts"])


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


def _normalize_prefix(raw: str) -> str:
    """Canonical form: no leading/trailing slashes, no '..' traversal.

    Raises ValueError on traversal attempts.
    """
    value = raw.strip()
    while value.startswith("/"):
        value = value[1:]
    while value.endswith("/"):
        value = value[:-1]
    parts = value.split("/") if value else []
    for segment in parts:
        if segment == ".." or segment == ".":
            raise ValueError(f"path_prefix must not contain '.' or '..' segments (got {raw!r})")
    return value


class PathContextIn(BaseModel):
    zone_id: str = Field(default=ROOT_ZONE_ID, max_length=255)
    path_prefix: str = Field(max_length=1024)
    description: str = Field(max_length=4096, min_length=1)

    @field_validator("path_prefix")
    @classmethod
    def _validate_prefix(cls, v: str) -> str:
        return _normalize_prefix(v)


class PathContextOut(BaseModel):
    zone_id: str
    path_prefix: str
    description: str
    created_at: Any
    updated_at: Any


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------


def _get_store(request: Request) -> PathContextStore:
    store: PathContextStore | None = getattr(request.app.state, "path_context_store", None)
    if store is None:
        raise HTTPException(status_code=503, detail="path context store not configured")
    return store


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.put("/")
async def upsert_context(
    body: PathContextIn,
    _admin: dict[str, Any] = Depends(require_admin),
    store: PathContextStore = Depends(_get_store),
) -> dict[str, Any]:
    """Upsert a path context (admin only)."""
    await store.upsert(body.zone_id, body.path_prefix, body.description)
    return {
        "zone_id": body.zone_id,
        "path_prefix": body.path_prefix,
        "description": body.description,
    }


@router.get("/")
async def list_contexts(
    zone_id: str | None = Query(default=None),
    _auth: dict[str, Any] = Depends(require_auth),
    store: PathContextStore = Depends(_get_store),
) -> dict[str, Any]:
    """List path contexts (any authenticated caller). Optional ?zone_id filter."""
    records = await store.list(zone_id)
    return {
        "contexts": [
            {
                "zone_id": r.zone_id,
                "path_prefix": r.path_prefix,
                "description": r.description,
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "updated_at": r.updated_at.isoformat() if r.updated_at else None,
            }
            for r in records
        ]
    }


@router.delete("/")
async def delete_context(
    zone_id: str = Query(...),
    path_prefix: str = Query(...),
    _admin: dict[str, Any] = Depends(require_admin),
    store: PathContextStore = Depends(_get_store),
) -> dict[str, Any]:
    """Delete a path context (admin only)."""
    try:
        normalized = _normalize_prefix(path_prefix)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    removed = await store.delete(zone_id, normalized)
    if not removed:
        raise HTTPException(status_code=404, detail="path context not found")
    return {"zone_id": zone_id, "path_prefix": normalized, "status": "deleted"}
