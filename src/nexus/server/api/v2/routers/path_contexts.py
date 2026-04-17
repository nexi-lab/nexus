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


async def _get_store(request: Request) -> PathContextStore:
    """Return a PathContextStore bound to the current request's event loop.

    Issue #3773 note: the startup-time store on ``app.state`` is bound to the
    lifespan loop, which diverges from the request loop under
    BaseHTTPMiddleware — asyncpg trips ``got result for unknown protocol
    state`` when used cross-loop. Lazily create a request-loop-native engine
    on first use and cache it on ``app.state`` keyed by the running loop.
    """
    import asyncio
    import os

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    loop = asyncio.get_running_loop()
    cached: dict[Any, PathContextStore] | None = getattr(
        request.app.state, "_path_context_store_by_loop", None
    )
    if cached is None:
        cached = {}
        request.app.state._path_context_store_by_loop = cached

    existing = cached.get(loop)
    if existing is not None:
        return existing

    # Fallback order: env vars -> app.state.database_url (set by create_app) ->
    # the startup-time injected store. Env-only lookup misses the
    # ``create_app(database_url=...)`` code path (Issue #3773 review feedback).
    db_url = (
        os.environ.get("DATABASE_URL")
        or os.environ.get("NEXUS_DATABASE_URL")
        or getattr(request.app.state, "database_url", None)
    )
    if not db_url:
        store: PathContextStore | None = getattr(request.app.state, "path_context_store", None)
        if store is None:
            raise HTTPException(status_code=503, detail="path context store not configured")
        cached[loop] = store
        return store

    if db_url.startswith("postgresql://"):
        db_url = db_url.replace("postgresql://", "postgresql+asyncpg://", 1)
        db_type = "postgresql"
    elif db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql+asyncpg://", 1)
        db_type = "postgresql"
    elif db_url.startswith("sqlite:") and "+aiosqlite" not in db_url:
        db_url = db_url.replace("sqlite:", "sqlite+aiosqlite:", 1)
        db_type = "sqlite"
    elif db_url.startswith("sqlite"):
        db_type = "sqlite"
    else:
        db_type = "sqlite"

    engine = create_async_engine(db_url, future=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    store = PathContextStore(async_session_factory=factory, db_type=db_type)
    cached[loop] = store
    # Track the engine for disposal on app shutdown (Issue #3773 review).
    engines: dict[Any, Any] | None = getattr(
        request.app.state, "_path_context_engines_by_loop", None
    )
    if engines is None:
        engines = {}
        request.app.state._path_context_engines_by_loop = engines
    engines[loop] = engine
    return store


async def dispose_loop_local_engines(app_state: Any) -> None:
    """Dispose engines cached by :func:`_get_store`.

    Called from the app's lifespan shutdown hook so we don't leak pooled
    asyncpg connections when request loops die off (Issue #3773 review).
    """
    import contextlib

    engines: dict[Any, Any] | None = getattr(app_state, "_path_context_engines_by_loop", None)
    if not engines:
        return
    for engine in list(engines.values()):
        with contextlib.suppress(Exception):
            await engine.dispose()
    engines.clear()
    cached: dict[Any, Any] | None = getattr(app_state, "_path_context_store_by_loop", None)
    if cached is not None:
        cached.clear()


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
    auth: dict[str, Any] = Depends(require_auth),
    store: PathContextStore = Depends(_get_store),
) -> dict[str, Any]:
    """List path contexts.

    Non-admin callers see only their own zone's contexts — enumeration across
    zones is admin-only (Issue #3773 review feedback). Admins may pass any
    ``zone_id`` or omit it to list across all zones.
    """
    is_admin = bool(auth.get("is_admin", False))
    caller_zone = auth.get("zone_id") or ROOT_ZONE_ID
    if not is_admin:
        if zone_id is not None and zone_id != caller_zone:
            raise HTTPException(
                status_code=403,
                detail="non-admin callers cannot list other zones",
            )
        zone_id = caller_zone
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
