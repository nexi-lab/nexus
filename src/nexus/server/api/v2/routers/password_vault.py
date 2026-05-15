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
from collections.abc import Callable
from typing import Any, TypeVar, cast

from anyio import from_thread
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.contracts.secrets_access import (
    ACCESS_CONTEXT_VALUES,
    DEFAULT_ACCESS_CONTEXT,
    AccessAuditContext,
    AccessContext,
)
from nexus.server.dependencies import require_auth
from nexus.server.zone_execution import run_zone_scoped
from nexus.services.password_vault.schema import VaultEntry
from nexus.services.password_vault.service import (
    PasswordVaultService,
    TotpNotConfiguredError,
    VaultEntryNotFoundError,
)


class TotpRequest(BaseModel):
    """Request body for ``POST /{title}/totp``.

    All fields optional — carries the same caller-tag tuple as Ask 1 GET
    endpoints so a TOTP generation shows up in the audit log with the
    same client_id / agent_session as the parent auto-login session.
    """

    model_config = ConfigDict(extra="ignore")

    access_context: str | None = None
    client_id: str | None = None
    agent_session: str | None = None


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v2/password_vault", tags=["password_vault"])

_MAX_TITLE_LEN = 1024
T = TypeVar("T")


def _validate_title(title: str) -> None:
    """Reject pathologically long titles before hitting the service layer."""
    if len(title) > _MAX_TITLE_LEN:
        raise HTTPException(
            status_code=400,
            detail=f"title too long ({len(title)} > {_MAX_TITLE_LEN} chars)",
        )


def _build_audit_context(
    access_context: str | None,
    client_id: str | None,
    agent_session: str | None,
) -> AccessAuditContext:
    """Validate query params and build an ``AccessAuditContext``.

    Unknown ``access_context`` values → 400, keeping the typed-enum
    invariant clients (and future enforcement) can rely on. Missing
    value defaults to ``admin_cli``.
    """
    value: AccessContext
    if access_context is None:
        value = DEFAULT_ACCESS_CONTEXT
    elif access_context in ACCESS_CONTEXT_VALUES:
        # Runtime-validated against the Literal's canonical values.
        value = cast(AccessContext, access_context)
    else:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Unknown access_context {access_context!r}. "
                f"Allowed: {sorted(ACCESS_CONTEXT_VALUES)}"
            ),
        )
    return AccessAuditContext(
        access_context=value,
        client_id=client_id,
        agent_session=agent_session,
    )


# --------------------------------------------------------------------------
# Dependency — injected by fastapi_server.py
# --------------------------------------------------------------------------


def get_password_vault_service() -> PasswordVaultService:
    """Placeholder dependency — overridden by fastapi_server.py."""
    raise HTTPException(status_code=500, detail="Password vault service not configured")


def _get_zone_registry(request: Request) -> Any | None:
    return getattr(request.app.state, "zone_registry", None)


def _auth_zone(auth_result: dict[str, Any]) -> str | None:
    zone_id = auth_result.get("zone_id") or ROOT_ZONE_ID
    return zone_id if zone_id != ROOT_ZONE_ID else None


def _run_zone_scoped_sync(request: Request, zone_id: str | None, work: Callable[[], T]) -> T:
    zone_registry = _get_zone_registry(request)
    if zone_registry is None or zone_id is None:
        return work()

    async def _work() -> T:
        return work()

    return from_thread.run(run_zone_scoped, zone_registry, zone_id, _work)


# --------------------------------------------------------------------------
# List (no path param — safe to declare first)
# --------------------------------------------------------------------------


@router.get("")
def list_entries(
    request: Request,
    access_context: str | None = Query(default=None),
    client_id: str | None = Query(default=None),
    agent_session: str | None = Query(default=None),
    auth_result: dict[str, Any] = Depends(require_auth),
    service: PasswordVaultService = Depends(get_password_vault_service),
) -> dict[str, Any]:
    """List every (live) vault entry with full decrypted payloads."""
    actor_id = auth_result.get("subject_id") or "anonymous"
    zone_id = auth_result.get("zone_id") or ROOT_ZONE_ID
    subject_id = auth_result.get("subject_id") or "anonymous"
    subject_type = auth_result.get("subject_type") or "user"
    audit_context = _build_audit_context(access_context, client_id, agent_session)

    def _list() -> dict[str, Any]:
        entries = service.list_entries(
            actor_id=actor_id,
            zone_id=zone_id,
            subject_id=subject_id,
            subject_type=subject_type,
            audit_context=audit_context,
        )
        return {"entries": [e.model_dump() for e in entries], "count": len(entries)}

    try:
        return _run_zone_scoped_sync(request, _auth_zone(auth_result), _list)
    except Exception as e:
        logger.error("Failed to list vault entries: %s", e)
        raise HTTPException(status_code=500, detail=f"Failed to list vault entries: {e}") from e


# --------------------------------------------------------------------------
# Suffixed routes — MUST come before bare /{title:path} wildcards
# --------------------------------------------------------------------------


@router.get("/{title:path}/versions")
def list_versions(
    title: str,
    request: Request,
    auth_result: dict[str, Any] = Depends(require_auth),
    service: PasswordVaultService = Depends(get_password_vault_service),
) -> dict[str, Any]:
    """List version history for a vault entry (for rotation audits)."""
    _validate_title(title)
    subject_id = auth_result.get("subject_id") or "anonymous"
    subject_type = auth_result.get("subject_type") or "user"

    def _list_versions() -> dict[str, Any]:
        versions = service.list_versions(
            title,
            subject_id=subject_id,
            subject_type=subject_type,
        )
        return {"title": title, "versions": versions, "count": len(versions)}

    try:
        return _run_zone_scoped_sync(request, _auth_zone(auth_result), _list_versions)
    except Exception as e:
        logger.error("Failed to list vault entry versions: %s", e)
        raise HTTPException(
            status_code=500, detail=f"Failed to list vault entry versions: {e}"
        ) from e


@router.post("/{title:path}/restore")
def restore_entry(
    title: str,
    request: Request,
    auth_result: dict[str, Any] = Depends(require_auth),
    service: PasswordVaultService = Depends(get_password_vault_service),
) -> dict[str, Any]:
    """Restore a soft-deleted vault entry."""
    _validate_title(title)
    actor_id = auth_result.get("subject_id") or "anonymous"
    zone_id = auth_result.get("zone_id") or ROOT_ZONE_ID
    subject_id = auth_result.get("subject_id") or "anonymous"
    subject_type = auth_result.get("subject_type") or "user"

    def _restore() -> dict[str, Any]:
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

    try:
        return _run_zone_scoped_sync(request, _auth_zone(auth_result), _restore)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Failed to restore vault entry: %s", e)
        raise HTTPException(status_code=500, detail=f"Failed to restore vault entry: {e}") from e


@router.post("/{title:path}/totp")
def generate_totp_code(
    title: str,
    request: Request,
    body: TotpRequest | None = None,
    auth_result: dict[str, Any] = Depends(require_auth),
    service: PasswordVaultService = Depends(get_password_vault_service),
) -> dict[str, Any]:
    """Compute a TOTP code server-side from the entry's stored ``totp_secret``.

    The secret never leaves nexus; only the 6-digit code + window metadata
    are returned. Emits a distinct ``totp_generated`` audit event.

    Response: ``{"code", "expires_in_seconds", "period_seconds"}``.

    Status codes:
        200 — Code generated.
        400 — Unknown ``access_context`` value in body.
        404 — Entry does not exist, or subject has no access to it.
        422 — Entry exists but has no ``totp_secret`` configured.
    """
    _validate_title(title)
    actor_id = auth_result.get("subject_id") or "anonymous"
    zone_id = auth_result.get("zone_id") or ROOT_ZONE_ID
    subject_id = auth_result.get("subject_id") or "anonymous"
    subject_type = auth_result.get("subject_type") or "user"

    body = body or TotpRequest()
    audit_context = _build_audit_context(body.access_context, body.client_id, body.agent_session)

    def _generate() -> dict[str, Any] | None:
        result = service.generate_totp(
            title,
            actor_id=actor_id,
            zone_id=zone_id,
            subject_id=subject_id,
            subject_type=subject_type,
            audit_context=audit_context,
        )

        return result

    try:
        result = _run_zone_scoped_sync(request, _auth_zone(auth_result), _generate)
    except TotpNotConfiguredError as e:
        raise HTTPException(status_code=422, detail="totp_not_configured") from e
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Failed to generate TOTP: %s", e)
        raise HTTPException(status_code=500, detail=f"Failed to generate TOTP: {e}") from e

    if result is None:
        raise HTTPException(status_code=404, detail="Vault entry not found")
    return result


# --------------------------------------------------------------------------
# Bare /{title:path} — wildcards, declared last so suffixed routes win
# --------------------------------------------------------------------------


@router.put("/{title:path}")
def put_entry(
    title: str,
    entry: VaultEntry,
    request: Request,
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

    def _put() -> dict[str, Any]:
        return service.put_entry(
            entry,
            actor_id=actor_id,
            zone_id=zone_id,
            subject_id=subject_id,
            subject_type=subject_type,
        )

    try:
        return _run_zone_scoped_sync(request, _auth_zone(auth_result), _put)
    except Exception as e:
        logger.error("Failed to put vault entry: %s", e)
        raise HTTPException(status_code=500, detail=f"Failed to put vault entry: {e}") from e


@router.get("/{title:path}")
def get_entry(
    title: str,
    request: Request,
    version: int | None = None,
    access_context: str | None = Query(default=None),
    client_id: str | None = Query(default=None),
    agent_session: str | None = Query(default=None),
    auth_result: dict[str, Any] = Depends(require_auth),
    service: PasswordVaultService = Depends(get_password_vault_service),
) -> VaultEntry:
    """Get a vault entry (latest unless ``?version=N`` is specified)."""
    _validate_title(title)
    actor_id = auth_result.get("subject_id") or "anonymous"
    zone_id = auth_result.get("zone_id") or ROOT_ZONE_ID
    subject_id = auth_result.get("subject_id") or "anonymous"
    subject_type = auth_result.get("subject_type") or "user"
    audit_context = _build_audit_context(access_context, client_id, agent_session)

    from nexus.bricks.secrets.service import SecretDisabledError

    def _get() -> VaultEntry:
        return service.get_entry(
            title,
            version=version,
            actor_id=actor_id,
            zone_id=zone_id,
            subject_id=subject_id,
            subject_type=subject_type,
            audit_context=audit_context,
        )

    try:
        return _run_zone_scoped_sync(request, _auth_zone(auth_result), _get)
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
    request: Request,
    auth_result: dict[str, Any] = Depends(require_auth),
    service: PasswordVaultService = Depends(get_password_vault_service),
) -> dict[str, Any]:
    """Soft-delete a vault entry."""
    _validate_title(title)
    actor_id = auth_result.get("subject_id") or "anonymous"
    zone_id = auth_result.get("zone_id") or ROOT_ZONE_ID
    subject_id = auth_result.get("subject_id") or "anonymous"
    subject_type = auth_result.get("subject_type") or "user"

    def _delete() -> dict[str, Any]:
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

    try:
        return _run_zone_scoped_sync(request, _auth_zone(auth_result), _delete)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Failed to delete vault entry: %s", e)
        raise HTTPException(status_code=500, detail=f"Failed to delete vault entry: {e}") from e
