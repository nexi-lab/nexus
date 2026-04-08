"""Secrets REST API — General-purpose secret storage with versioning.

    PUT  /api/v2/secrets/{namespace}/{key}                     — Create/update secret
    GET  /api/v2/secrets/{namespace}/{key}                     — Get secret value
    DELETE /api/v2/secrets/{namespace}/{key}                   — Soft delete secret
    GET  /api/v2/secrets                                      — List secrets
    POST /api/v2/secrets/batch                                 — Batch operations
    PUT  /api/v2/secrets/{namespace}/{key}/enable             — Enable secret
    PUT  /api/v2/secrets/{namespace}/{key}/disable            — Disable secret
    POST /api/v2/secrets/{namespace}/{key}/restore             — Restore deleted secret
    GET  /api/v2/secrets/{namespace}/{key}/versions          — List versions
    DELETE /api/v2/secrets/{namespace}/{key}/versions/{version} — Delete version
    PUT  /api/v2/secrets/{namespace}/{key}/description        — Update description

Performance: All endpoints use plain ``def`` (not ``async def``) so
FastAPI auto-dispatches to a threadpool. This prevents blocking
the asyncio event loop during synchronous SQLAlchemy I/O.
"""

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.contracts.protocols.secrets_audit_log import SecretsAuditLogProtocol
from nexus.server.dependencies import require_auth

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v2/secrets", tags=["secrets"])


# --------------------------------------------------------------------------
# Dependency — injected by fastapi_server.py
# --------------------------------------------------------------------------


def get_secrets_service() -> Any:
    """Placeholder dependency — overridden by fastapi_server.py."""
    raise HTTPException(status_code=500, detail="Secrets service not configured")


def get_secrets_audit_logger() -> tuple[SecretsAuditLogProtocol, str]:
    """Placeholder dependency — overridden by fastapi_server.py."""
    raise HTTPException(status_code=500, detail="Secrets audit not configured")


# --------------------------------------------------------------------------
# Write / Update
# --------------------------------------------------------------------------


@router.put("/{namespace}/{key}")
def put_secret(
    namespace: str,
    key: str,
    body: dict[str, Any],
    auth_result: dict[str, Any] = Depends(require_auth),
    service: Any = Depends(get_secrets_service),
) -> dict[str, Any]:
    """Create or update a secret (creates a new version).

    PUT /api/v2/secrets/{namespace}/{key}
    """
    actor_id = auth_result.get("subject_id") or "anonymous"
    zone_id = auth_result.get("zone_id") or ROOT_ZONE_ID
    value = body.get("value")
    description = body.get("description")
    subject_id = auth_result.get("subject_id") or "anonymous"
    subject_type = auth_result.get("subject_type") or "user"

    if not value:
        raise HTTPException(status_code=400, detail="value is required")

    try:
        return service.put_secret(
            namespace=namespace,
            key=key,
            value=value,
            description=description,
            actor_id=actor_id,
            zone_id=zone_id,
            subject_id=subject_id,
            subject_type=subject_type,
        )
    except Exception as e:
        logger.error("Failed to put secret: %s", e)
        raise HTTPException(status_code=500, detail=f"Failed to put secret: {e}") from e


@router.put("/{namespace}/{key}/description")
def update_description(
    namespace: str,
    key: str,
    body: dict[str, Any],
    auth_result: dict[str, Any] = Depends(require_auth),
    service: Any = Depends(get_secrets_service),
) -> dict[str, Any]:
    """Update secret description.

    PUT /api/v2/secrets/{namespace}/{key}/description
    """
    actor_id = auth_result.get("subject_id") or "anonymous"
    zone_id = auth_result.get("zone_id") or ROOT_ZONE_ID
    description = body.get("description", "")
    subject_id = auth_result.get("subject_id") or "anonymous"
    subject_type = auth_result.get("subject_type") or "user"

    try:
        success = service.update_description(
            namespace=namespace,
            key=key,
            description=description,
            actor_id=actor_id,
            zone_id=zone_id,
            subject_id=subject_id,
            subject_type=subject_type,
        )
        if not success:
            raise HTTPException(status_code=404, detail="Secret not found")
        return {"namespace": namespace, "key": key, "description": description}
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Failed to update description: %s", e)
        raise HTTPException(status_code=500, detail=f"Failed to update description: {e}") from e


# --------------------------------------------------------------------------
# Read
# --------------------------------------------------------------------------


@router.get("/{namespace}/{key}")
def get_secret(
    namespace: str,
    key: str,
    version: int | None = None,
    auth_result: dict[str, Any] = Depends(require_auth),
    service: Any = Depends(get_secrets_service),
) -> dict[str, Any]:
    """Get a secret value (decrypted).

    GET /api/v2/secrets/{namespace}/{key}?version=N
    """
    actor_id = auth_result.get("subject_id") or "anonymous"
    zone_id = auth_result.get("zone_id") or ROOT_ZONE_ID
    subject_id = auth_result.get("subject_id") or "anonymous"
    subject_type = auth_result.get("subject_type") or "user"

    try:
        from nexus.bricks.secrets.service import SecretDisabledError

        result = service.get_secret(
            namespace=namespace,
            key=key,
            actor_id=actor_id,
            version=version,
            zone_id=zone_id,
            subject_id=subject_id,
            subject_type=subject_type,
        )
        if result is None:
            raise HTTPException(status_code=404, detail="Secret not found")
        return {
            "namespace": namespace,
            "key": key,
            "value": result["value"],
            "version": result["version"],
        }
    except HTTPException:
        raise
    except SecretDisabledError as e:
        raise HTTPException(status_code=403, detail=str(e)) from e
    except Exception as e:
        logger.error("Failed to get secret: %s", e)
        raise HTTPException(status_code=500, detail=f"Failed to get secret: {e}") from e


@router.get("")
def list_secrets(
    namespace: str | None = None,
    include_deleted: bool = False,
    auth_result: dict[str, Any] = Depends(require_auth),
    service: Any = Depends(get_secrets_service),
) -> dict[str, Any]:
    """List secrets (metadata only, no encrypted values).

    GET /api/v2/secrets?namespace=...&include_deleted=true
    """
    subject_id = auth_result.get("subject_id") or "anonymous"
    subject_type = auth_result.get("subject_type") or "user"

    try:
        secrets = service.list_secrets(
            namespace=namespace,
            include_deleted=include_deleted,
            subject_id=subject_id,
            subject_type=subject_type,
        )
        return {"secrets": secrets, "count": len(secrets)}
    except Exception as e:
        logger.error("Failed to list secrets: %s", e)
        raise HTTPException(status_code=500, detail=f"Failed to list secrets: {e}") from e


@router.get("/{namespace}/{key}/versions")
def list_versions(
    namespace: str,
    key: str,
    auth_result: dict[str, Any] = Depends(require_auth),
    service: Any = Depends(get_secrets_service),
) -> dict[str, Any]:
    """List version history for a secret.

    GET /api/v2/secrets/{namespace}/{key}/versions
    """
    subject_id = auth_result.get("subject_id") or "anonymous"
    subject_type = auth_result.get("subject_type") or "user"

    try:
        versions = service.list_versions(
            namespace=namespace,
            key=key,
            subject_id=subject_id,
            subject_type=subject_type,
        )
        return {"namespace": namespace, "key": key, "versions": versions, "count": len(versions)}
    except Exception as e:
        logger.error("Failed to list versions: %s", e)
        raise HTTPException(status_code=500, detail=f"Failed to list versions: {e}") from e


# --------------------------------------------------------------------------
# Delete / Soft Delete
# --------------------------------------------------------------------------


@router.delete("/{namespace}/{key}")
def delete_secret(
    namespace: str,
    key: str,
    auth_result: dict[str, Any] = Depends(require_auth),
    service: Any = Depends(get_secrets_service),
) -> dict[str, Any]:
    """Soft delete a secret.

    DELETE /api/v2/secrets/{namespace}/{key}
    """
    actor_id = auth_result.get("subject_id") or "anonymous"
    zone_id = auth_result.get("zone_id") or ROOT_ZONE_ID
    subject_id = auth_result.get("subject_id") or "anonymous"
    subject_type = auth_result.get("subject_type") or "user"

    try:
        success = service.delete_secret(
            namespace=namespace,
            key=key,
            actor_id=actor_id,
            zone_id=zone_id,
            subject_id=subject_id,
            subject_type=subject_type,
        )
        if not success:
            raise HTTPException(status_code=404, detail="Secret not found")
        return {"namespace": namespace, "key": key, "deleted": True}
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Failed to delete secret: %s", e)
        raise HTTPException(status_code=500, detail=f"Failed to delete secret: {e}") from e


@router.post("/{namespace}/{key}/restore")
def restore_secret(
    namespace: str,
    key: str,
    auth_result: dict[str, Any] = Depends(require_auth),
    service: Any = Depends(get_secrets_service),
) -> dict[str, Any]:
    """Restore a soft-deleted secret.

    POST /api/v2/secrets/{namespace}/{key}/restore
    """
    actor_id = auth_result.get("subject_id") or "anonymous"
    zone_id = auth_result.get("zone_id") or ROOT_ZONE_ID
    subject_id = auth_result.get("subject_id") or "anonymous"
    subject_type = auth_result.get("subject_type") or "user"

    try:
        success = service.restore_secret(
            namespace=namespace,
            key=key,
            actor_id=actor_id,
            zone_id=zone_id,
            subject_id=subject_id,
            subject_type=subject_type,
        )
        if not success:
            raise HTTPException(status_code=404, detail="Secret not found")
        return {"namespace": namespace, "key": key, "restored": True}
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Failed to restore secret: %s", e)
        raise HTTPException(status_code=500, detail=f"Failed to restore secret: {e}") from e


@router.delete("/{namespace}/{key}/versions/{version}")
def delete_version(
    namespace: str,
    key: str,
    version: int,
    auth_result: dict[str, Any] = Depends(require_auth),
    service: Any = Depends(get_secrets_service),
) -> dict[str, Any]:
    """Delete a specific version (must keep at least one version).

    DELETE /api/v2/secrets/{namespace}/{key}/versions/{version}
    """
    actor_id = auth_result.get("subject_id") or "anonymous"
    zone_id = auth_result.get("zone_id") or ROOT_ZONE_ID
    subject_id = auth_result.get("subject_id") or "anonymous"
    subject_type = auth_result.get("subject_type") or "user"

    try:
        success = service.delete_version(
            namespace=namespace,
            key=key,
            version=version,
            actor_id=actor_id,
            zone_id=zone_id,
            subject_id=subject_id,
            subject_type=subject_type,
        )
        if not success:
            raise HTTPException(
                status_code=400, detail="Cannot delete version (not found or last version)"
            )
        return {"namespace": namespace, "key": key, "version": version, "deleted": True}
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Failed to delete version: %s", e)
        raise HTTPException(status_code=500, detail=f"Failed to delete version: {e}") from e


# --------------------------------------------------------------------------
# Enable / Disable
# --------------------------------------------------------------------------


@router.put("/{namespace}/{key}/enable")
def enable_secret(
    namespace: str,
    key: str,
    auth_result: dict[str, Any] = Depends(require_auth),
    service: Any = Depends(get_secrets_service),
) -> dict[str, Any]:
    """Enable a secret.

    PUT /api/v2/secrets/{namespace}/{key}/enable
    """
    actor_id = auth_result.get("subject_id") or "anonymous"
    zone_id = auth_result.get("zone_id") or ROOT_ZONE_ID
    subject_id = auth_result.get("subject_id") or "anonymous"
    subject_type = auth_result.get("subject_type") or "user"

    try:
        success = service.enable_secret(
            namespace=namespace,
            key=key,
            actor_id=actor_id,
            zone_id=zone_id,
            subject_id=subject_id,
            subject_type=subject_type,
        )
        if not success:
            raise HTTPException(status_code=404, detail="Secret not found")
        return {"namespace": namespace, "key": key, "enabled": True}
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Failed to enable secret: %s", e)
        raise HTTPException(status_code=500, detail=f"Failed to enable secret: {e}") from e


@router.put("/{namespace}/{key}/disable")
def disable_secret(
    namespace: str,
    key: str,
    auth_result: dict[str, Any] = Depends(require_auth),
    service: Any = Depends(get_secrets_service),
) -> dict[str, Any]:
    """Disable a secret.

    PUT /api/v2/secrets/{namespace}/{key}/disable
    """
    actor_id = auth_result.get("subject_id") or "anonymous"
    zone_id = auth_result.get("zone_id") or ROOT_ZONE_ID
    subject_id = auth_result.get("subject_id") or "anonymous"
    subject_type = auth_result.get("subject_type") or "user"

    try:
        success = service.disable_secret(
            namespace=namespace,
            key=key,
            actor_id=actor_id,
            zone_id=zone_id,
            subject_id=subject_id,
            subject_type=subject_type,
        )
        if not success:
            raise HTTPException(status_code=404, detail="Secret not found")
        return {"namespace": namespace, "key": key, "enabled": False}
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Failed to disable secret: %s", e)
        raise HTTPException(status_code=500, detail=f"Failed to disable secret: {e}") from e


# --------------------------------------------------------------------------
# Batch Operations
# --------------------------------------------------------------------------
# Note: Batch operations use POST with a different path pattern


@router.post("/batch")
def batch_put(
    secrets: list[dict[str, Any]],
    auth_result: dict[str, Any] = Depends(require_auth),
    service: Any = Depends(get_secrets_service),
) -> dict[str, Any]:
    """Batch create/update secrets.

    POST /api/v2/secrets/batch
    """
    actor_id = auth_result.get("subject_id") or "anonymous"
    zone_id = auth_result.get("zone_id") or ROOT_ZONE_ID
    subject_id = auth_result.get("subject_id") or "anonymous"
    subject_type = auth_result.get("subject_type") or "user"

    try:
        results = service.batch_put(
            secrets=secrets,
            actor_id=actor_id,
            zone_id=zone_id,
            subject_id=subject_id,
            subject_type=subject_type,
        )
        return {"secrets": results, "count": len(results)}
    except Exception as e:
        logger.error("Failed to batch put secrets: %s", e)
        raise HTTPException(status_code=500, detail=f"Failed to batch put secrets: {e}") from e


@router.post("/batch/get")
def batch_get(
    queries: list[dict[str, Any]],
    auth_result: dict[str, Any] = Depends(require_auth),
    service: Any = Depends(get_secrets_service),
) -> dict[str, Any]:
    """Batch get secrets.

    POST /api/v2/secrets/batch/get
    """
    actor_id = auth_result.get("subject_id") or "anonymous"
    zone_id = auth_result.get("zone_id") or ROOT_ZONE_ID
    subject_id = auth_result.get("subject_id") or "anonymous"
    subject_type = auth_result.get("subject_type") or "user"

    try:
        results = service.batch_get(
            queries=queries,
            actor_id=actor_id,
            zone_id=zone_id,
            subject_id=subject_id,
            subject_type=subject_type,
        )
        return {"secrets": results, "count": len(results)}
    except Exception as e:
        logger.error("Failed to batch get secrets: %s", e)
        raise HTTPException(status_code=500, detail=f"Failed to batch get secrets: {e}") from e
