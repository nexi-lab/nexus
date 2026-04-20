"""Password vault REST API — domain-typed wrapper over SecretsService.

    PUT    /api/v2/password_vault/{title}                     — Create/update entry
    GET    /api/v2/password_vault/{title}                     — Get entry (latest)
    GET    /api/v2/password_vault/{title}?version=N           — Get specific version
    DELETE /api/v2/password_vault/{title}                     — Soft delete
    POST   /api/v2/password_vault/{title}/restore             — Restore soft-deleted
    GET    /api/v2/password_vault                             — List entries (full)
    GET    /api/v2/password_vault/{title}/versions            — List version history

Titles use the ``{title:path}`` convertor so URL-encoded slashes in a
title (e.g., a URL used *as* a title) round-trip correctly. Starlette
decodes ``%2F`` to ``/`` before routing, so only the ``path`` convertor
can match a title that contains ``/``. The 1024-char cap on the decoded
title guards against pathological URLs / accidental DoS.

Route declaration order matters: suffixed routes (``/versions``,
``/restore``) must come before the bare ``/{title:path}`` wildcards, or
the wildcard will swallow the suffix and the targeted handler will
never be reached.

Performance: all endpoints use plain ``def`` (not ``async def``) so
FastAPI auto-dispatches to a threadpool — matches the secrets router
and keeps the asyncio event loop unblocked during synchronous
SQLAlchemy I/O in the underlying SecretsService.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.server.dependencies import require_auth
from nexus.services.password_vault.schema import VaultEntry
from nexus.services.password_vault.service import (
    PasswordVaultService,
    VaultEntryNotFoundError,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v2/password_vault", tags=["password_vault"])

_MAX_TITLE_LEN = 1024


def _validate_title(title: str) -> None:
    """Reject pathologically long titles before hitting the service layer."""
    if len(title) > _MAX_TITLE_LEN:
        raise HTTPException(
            status_code=400,
            detail=f"title too long ({len(title)} > {_MAX_TITLE_LEN} chars)",
        )


# --------------------------------------------------------------------------
# Dependency — injected by fastapi_server.py
# --------------------------------------------------------------------------


def get_password_vault_service() -> PasswordVaultService:
    """Placeholder dependency — overridden by fastapi_server.py."""
    raise HTTPException(status_code=500, detail="Password vault service not configured")


# --------------------------------------------------------------------------
# List (no path param — safe to declare first)
# --------------------------------------------------------------------------


@router.get("")
def list_entries(
    auth_result: dict[str, Any] = Depends(require_auth),
    service: PasswordVaultService = Depends(get_password_vault_service),
) -> dict[str, Any]:
    """List every (live) vault entry with full decrypted payloads."""
    actor_id = auth_result.get("subject_id") or "anonymous"
    zone_id = auth_result.get("zone_id") or ROOT_ZONE_ID
    subject_id = auth_result.get("subject_id") or "anonymous"
    subject_type = auth_result.get("subject_type") or "user"

    try:
        entries = service.list_entries(
            actor_id=actor_id,
            zone_id=zone_id,
            subject_id=subject_id,
            subject_type=subject_type,
        )
        return {"entries": [e.model_dump() for e in entries], "count": len(entries)}
    except Exception as e:
        logger.error("Failed to list vault entries: %s", e)
        raise HTTPException(status_code=500, detail=f"Failed to list vault entries: {e}") from e


# --------------------------------------------------------------------------
# Suffixed routes — MUST come before bare /{title:path} wildcards
# --------------------------------------------------------------------------


@router.get("/{title:path}/versions")
def list_versions(
    title: str,
    auth_result: dict[str, Any] = Depends(require_auth),
    service: PasswordVaultService = Depends(get_password_vault_service),
) -> dict[str, Any]:
    """List version history for a vault entry (for rotation audits)."""
    _validate_title(title)
    subject_id = auth_result.get("subject_id") or "anonymous"
    subject_type = auth_result.get("subject_type") or "user"

    try:
        versions = service.list_versions(
            title,
            subject_id=subject_id,
            subject_type=subject_type,
        )
        return {"title": title, "versions": versions, "count": len(versions)}
    except Exception as e:
        logger.error("Failed to list vault entry versions: %s", e)
        raise HTTPException(
            status_code=500, detail=f"Failed to list vault entry versions: {e}"
        ) from e


@router.post("/{title:path}/restore")
def restore_entry(
    title: str,
    auth_result: dict[str, Any] = Depends(require_auth),
    service: PasswordVaultService = Depends(get_password_vault_service),
) -> dict[str, Any]:
    """Restore a soft-deleted vault entry."""
    _validate_title(title)
    actor_id = auth_result.get("subject_id") or "anonymous"
    zone_id = auth_result.get("zone_id") or ROOT_ZONE_ID
    subject_id = auth_result.get("subject_id") or "anonymous"
    subject_type = auth_result.get("subject_type") or "user"

    try:
        ok = service.restore_entry(
            title,
            actor_id=actor_id,
            zone_id=zone_id,
            subject_id=subject_id,
            subject_type=subject_type,
        )
        if not ok:
            raise HTTPException(status_code=404, detail="Vault entry not found")
        return {"title": title, "restored": True}
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Failed to restore vault entry: %s", e)
        raise HTTPException(status_code=500, detail=f"Failed to restore vault entry: {e}") from e


# --------------------------------------------------------------------------
# Bare /{title:path} — wildcards, declared last so suffixed routes win
# --------------------------------------------------------------------------


@router.put("/{title:path}")
def put_entry(
    title: str,
    entry: VaultEntry,
    auth_result: dict[str, Any] = Depends(require_auth),
    service: PasswordVaultService = Depends(get_password_vault_service),
) -> dict[str, Any]:
    """Create or update a vault entry (new version per write).

    ``body.title`` must match the URL path segment.
    """
    _validate_title(title)
    if entry.title != title:
        raise HTTPException(
            status_code=400,
            detail=f"Body title {entry.title!r} does not match URL title {title!r}",
        )

    actor_id = auth_result.get("subject_id") or "anonymous"
    zone_id = auth_result.get("zone_id") or ROOT_ZONE_ID
    subject_id = auth_result.get("subject_id") or "anonymous"
    subject_type = auth_result.get("subject_type") or "user"

    try:
        return service.put_entry(
            entry,
            actor_id=actor_id,
            zone_id=zone_id,
            subject_id=subject_id,
            subject_type=subject_type,
        )
    except Exception as e:
        logger.error("Failed to put vault entry: %s", e)
        raise HTTPException(status_code=500, detail=f"Failed to put vault entry: {e}") from e


@router.get("/{title:path}")
def get_entry(
    title: str,
    version: int | None = None,
    auth_result: dict[str, Any] = Depends(require_auth),
    service: PasswordVaultService = Depends(get_password_vault_service),
) -> VaultEntry:
    """Get a vault entry (latest unless ``?version=N`` is specified)."""
    _validate_title(title)
    actor_id = auth_result.get("subject_id") or "anonymous"
    zone_id = auth_result.get("zone_id") or ROOT_ZONE_ID
    subject_id = auth_result.get("subject_id") or "anonymous"
    subject_type = auth_result.get("subject_type") or "user"

    try:
        from nexus.bricks.secrets.service import SecretDisabledError

        return service.get_entry(
            title,
            version=version,
            actor_id=actor_id,
            zone_id=zone_id,
            subject_id=subject_id,
            subject_type=subject_type,
        )
    except VaultEntryNotFoundError as e:
        raise HTTPException(status_code=404, detail="Vault entry not found") from e
    except SecretDisabledError as e:
        raise HTTPException(status_code=403, detail=str(e)) from e
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Failed to get vault entry: %s", e)
        raise HTTPException(status_code=500, detail=f"Failed to get vault entry: {e}") from e


@router.delete("/{title:path}")
def delete_entry(
    title: str,
    auth_result: dict[str, Any] = Depends(require_auth),
    service: PasswordVaultService = Depends(get_password_vault_service),
) -> dict[str, Any]:
    """Soft-delete a vault entry."""
    _validate_title(title)
    actor_id = auth_result.get("subject_id") or "anonymous"
    zone_id = auth_result.get("zone_id") or ROOT_ZONE_ID
    subject_id = auth_result.get("subject_id") or "anonymous"
    subject_type = auth_result.get("subject_type") or "user"

    try:
        ok = service.delete_entry(
            title,
            actor_id=actor_id,
            zone_id=zone_id,
            subject_id=subject_id,
            subject_type=subject_type,
        )
        if not ok:
            raise HTTPException(status_code=404, detail="Vault entry not found")
        return {"title": title, "deleted": True}
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Failed to delete vault entry: %s", e)
        raise HTTPException(status_code=500, detail=f"Failed to delete vault entry: {e}") from e
