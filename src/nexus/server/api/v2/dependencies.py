"""Shared dependencies for API v2 endpoints.

Provides FastAPI dependency injection with proper authentication context.
All routers should import deps from here instead of duplicating inline helpers.

Issue #2138: Protocol return types replace ``Any`` for static type safety.
"""

import logging
from typing import TYPE_CHECKING, Any, cast

from fastapi import Depends, HTTPException, Request

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.contracts.protocols.write_back import WriteBackProtocol
from nexus.core.protocols.vfs_core import VFSCoreProtocol
from nexus.server.dependencies import get_operation_context, require_auth

if TYPE_CHECKING:
    from nexus.contracts.types import OperationContext

logger = logging.getLogger(__name__)

# =============================================================================
# Router-facing re-exports (#2138)
# =============================================================================
# Routers import these names; the implementation now delegates directly
# to server/dependencies instead of using lazy imports.


def _get_require_auth() -> Any:
    """Return the ``require_auth`` dependency for ``Depends()``."""
    return require_auth


def _get_operation_context(auth_result: dict[str, Any]) -> "OperationContext":
    """Build an OperationContext from *auth_result*."""
    return cast("OperationContext", get_operation_context(auth_result))


# =============================================================================
# Core dependencies
# =============================================================================


async def get_nexus_fs(request: Request) -> VFSCoreProtocol:
    """Get NexusFS instance, raising 503 if not initialized.

    All deps that need NexusFS should accept this via Depends()
    rather than repeating the guard inline.
    """
    if not request.app.state.nexus_fs:
        raise HTTPException(status_code=503, detail="NexusFS not initialized")
    return cast(VFSCoreProtocol, request.app.state.nexus_fs)


async def get_record_store(request: Request) -> Any:
    """Get RecordStoreABC instance from app state (Issue #2200).

    Raises 503 if record_store is not initialized. Prefer this over
    get_session_factory() for new endpoints.
    """
    record_store = getattr(request.app.state, "record_store", None)
    if record_store is None:
        raise HTTPException(status_code=503, detail="RecordStore not initialized")
    return record_store


async def get_auth_result(
    auth_result: dict[str, Any] | None = Depends(require_auth),
) -> dict[str, Any]:
    """Get authenticated user context."""
    if auth_result is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    return auth_result


# =============================================================================
# Service dependencies
# =============================================================================


async def get_conflict_log_store(request: Request) -> Any:
    """Get ConflictLogStore instance from app state."""
    store = getattr(request.app.state, "conflict_log_store", None)
    if store is None:
        raise HTTPException(status_code=503, detail="Conflict log store not initialized")
    return store


async def get_write_back_service(request: Request) -> WriteBackProtocol:
    """Get WriteBackService instance from app state."""
    service = getattr(request.app.state, "write_back_service", None)
    if service is None:
        raise HTTPException(
            status_code=503,
            detail="Write-back service not initialized (set NEXUS_WRITE_BACK=true)",
        )
    return cast(WriteBackProtocol, service)


async def get_operation_logger(
    nexus_fs: Any = Depends(get_nexus_fs),
    auth_result: dict[str, Any] = Depends(require_auth),
) -> Any:
    """Get OperationLogger scoped to the authenticated user's zone.

    Returns a tuple of (OperationLogger, zone_id) for zone-scoped queries.
    Uses an async generator so FastAPI closes the session after the request.
    """
    from nexus.storage.operation_logger import OperationLogger

    context = get_operation_context(auth_result)
    _record_store = getattr(nexus_fs, "_record_store", None)
    session_factory = (
        _record_store.session_factory if _record_store is not None else nexus_fs.SessionLocal
    )
    session = session_factory()
    zone_id = context.zone_id or ROOT_ZONE_ID

    try:
        yield OperationLogger(session=session), zone_id
    finally:
        session.close()


async def get_exchange_audit_logger(
    nexus_fs: Any = Depends(get_nexus_fs),
    auth_result: dict[str, Any] = Depends(require_auth),
) -> Any:
    """Get ExchangeAuditLogger scoped to the authenticated user's zone.

    Returns a tuple of (ExchangeAuditLogger, zone_id) for zone-scoped queries.
    Issue #1360.
    """
    from nexus.storage.exchange_audit_logger import ExchangeAuditLogger

    context = _get_operation_context(auth_result)
    zone_id = context.zone_id or ROOT_ZONE_ID

    _record_store = getattr(nexus_fs, "_record_store", None)
    if _record_store is None:
        raise HTTPException(status_code=503, detail="RecordStore not initialized")
    return ExchangeAuditLogger(record_store=_record_store), zone_id


# =============================================================================
# Reputation & Trust dependencies (Issue #1356)
# =============================================================================


async def get_reputation_context(
    nexus_fs: Any = Depends(get_nexus_fs),
    auth_result: dict[str, Any] = Depends(require_auth),
) -> tuple[Any, Any, dict[str, Any]]:
    """Get ReputationService + DisputeService + auth context.

    Prefers the singleton ReputationService on app.state (#1619),
    falling back to per-request instantiation for backward compat.

    Returns:
        Tuple of (ReputationService, DisputeService, auth_context dict).
    """
    from nexus.bricks.reputation.dispute_service import DisputeService
    from nexus.bricks.reputation.reputation_service import ReputationService

    _record_store = getattr(nexus_fs, "_record_store", None)
    if _record_store is None:
        raise HTTPException(status_code=503, detail="RecordStore not initialized")

    # Per-request instantiation (singleton DI via app.state planned in #1619)
    reputation_service = ReputationService(
        record_store=_record_store,
    )
    dispute_service = DisputeService(record_store=_record_store)

    context = get_operation_context(auth_result)
    auth_ctx = {
        "user_id": context.user_id or "",
        "subject_id": getattr(context, "subject_id", ""),
        "subject_type": getattr(context, "subject_type", ""),
        "is_admin": getattr(context, "is_admin", False),
        "zone_id": context.zone_id,
    }

    return reputation_service, dispute_service, auth_ctx


# =============================================================================
# Aspect & Catalog dependencies (Issue #2930)
# =============================================================================


async def get_aspect_service(
    nexus_fs: Any = Depends(get_nexus_fs),
    auth_result: dict[str, Any] = Depends(require_auth),
) -> Any:
    """Get AspectService scoped to the authenticated user's zone.

    Returns a tuple of (AspectService, zone_id) for zone-scoped operations.
    Issue #2930.
    """
    from nexus.storage.aspect_service import AspectService

    context = get_operation_context(auth_result)
    _record_store = getattr(nexus_fs, "_record_store", None)
    session_factory = (
        _record_store.session_factory if _record_store is not None else nexus_fs.SessionLocal
    )
    session = session_factory()
    zone_id = context.zone_id or ROOT_ZONE_ID

    try:
        yield AspectService(session=session), zone_id
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


async def get_catalog_service(
    nexus_fs: Any = Depends(get_nexus_fs),
    auth_result: dict[str, Any] = Depends(require_auth),
) -> Any:
    """Get CatalogService scoped to the authenticated user's zone.

    Returns a tuple of (CatalogService, zone_id) for zone-scoped operations.
    Issue #2930.
    """
    from nexus.bricks.catalog.protocol import CatalogService
    from nexus.storage.aspect_service import AspectService

    context = get_operation_context(auth_result)
    _record_store = getattr(nexus_fs, "_record_store", None)
    session_factory = (
        _record_store.session_factory if _record_store is not None else nexus_fs.SessionLocal
    )
    session = session_factory()
    zone_id = context.zone_id or ROOT_ZONE_ID

    try:
        aspect_svc = AspectService(session=session)
        yield CatalogService(aspect_svc), zone_id
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
