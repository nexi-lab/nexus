"""Authenticated product Registry API; open-access identity headers are never accepted."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError
from starlette.concurrency import run_in_threadpool

from nexus.contracts.agent_inventory import (
    AssetHistory,
    AssetPage,
    AssetRecord,
    parse_asset_command,
)
from nexus.contracts.product_agent_registry import (
    MAX_BODY_BYTES,
    CommandResult,
    PrincipalRef,
    Resolution,
    VersionItem,
    VersionsPage,
    parse_command,
)
from nexus.server.api.v2.routers.agent_evaluations import migration_router
from nexus.server.api.v2.routers.agent_evaluations import router as evaluations_router
from nexus.server.api.v2.routers.agent_execution import router as execution_router
from nexus.server.api.v2.routers.agent_hosted import router as hosted_router
from nexus.server.api.v2.routers.agent_native import router as native_router
from nexus.server.dependencies import require_auth
from nexus.services.product_agent_registry.store import (
    ProductAgentRegistry,
    RegistryActor,
    RegistryError,
)

router = APIRouter(prefix="/api/v2/product-agents", tags=["product-agent-registry"])

router.include_router(execution_router)
router.include_router(hosted_router)
router.include_router(native_router)

router.include_router(evaluations_router)
router.include_router(migration_router)


def actor(auth: dict[str, Any] = Depends(require_auth)) -> RegistryActor:
    if (
        auth.get("metadata", {}).get("open_access")
        or not auth.get("subject_id")
        or not auth.get("zone_id")
    ):
        raise HTTPException(401, detail={"code": "REGISTRY_IDENTITY_REQUIRED"})
    return RegistryActor(
        subject=f"{auth.get('subject_type') or 'user'}:{auth['subject_id']}",
        zone=auth["zone_id"],
        is_admin=auth.get("is_admin") is True,
    )


def service(request: Request) -> ProductAgentRegistry:
    result = getattr(request.app.state, "product_agent_registry", None)
    if result is None:
        raise HTTPException(503, detail={"code": "REGISTRY_NOT_CONFIGURED"})
    return result


async def invoke(operation: Callable):
    try:
        return await run_in_threadpool(operation)
    except RegistryError as error:
        raise HTTPException(error.status, detail={"code": error.code}) from None
    except (SQLAlchemyError, ValidationError):
        raise HTTPException(503, detail={"code": "REGISTRY_STORAGE_UNAVAILABLE"}) from None


@router.post("/commands", response_model=CommandResult)
async def execute(
    request: Request,
    identity: RegistryActor = Depends(actor),
    registry: ProductAgentRegistry = Depends(service),
):
    if not identity.is_admin:
        raise HTTPException(403, detail={"code": "REGISTRY_FORBIDDEN"})
    data = bytearray()
    async for chunk in request.stream():
        if len(data) + len(chunk) > MAX_BODY_BYTES:
            raise HTTPException(413, detail={"code": "REGISTRY_BODY_LIMIT"})
        data.extend(chunk)
    try:
        command = parse_command(bytes(data))
    except (ValueError, RecursionError):
        raise HTTPException(400, detail={"code": "INVALID_REGISTRY_COMMAND"}) from None
    return await invoke(lambda: registry.command(identity, command))


@router.post("/inventory/commands", response_model=AssetRecord)
async def inventory_command(
    request: Request,
    identity: RegistryActor = Depends(actor),
    registry: ProductAgentRegistry = Depends(service),
):
    if not identity.is_admin:
        raise HTTPException(403, detail={"code": "REGISTRY_INVENTORY_FORBIDDEN"})
    data = bytearray()
    async for chunk in request.stream():
        if len(data) + len(chunk) > MAX_BODY_BYTES:
            raise HTTPException(413, detail={"code": "REGISTRY_BODY_LIMIT"})
        data.extend(chunk)
    try:
        command = parse_asset_command(bytes(data))
    except (ValueError, RecursionError):
        raise HTTPException(400, detail={"code": "INVALID_INVENTORY_COMMAND"}) from None
    return await invoke(lambda: registry.inventory.command(identity, command))


@router.get("/inventory/assets", response_model=AssetPage)
async def inventory_list(
    after: str = Query("", max_length=150),
    owner: str | None = Query(None, max_length=150),
    identity: RegistryActor = Depends(actor),
    registry: ProductAgentRegistry = Depends(service),
):
    return await invoke(lambda: registry.inventory.list(identity, after, owner))


@router.get("/inventory/assets/{asset_id}", response_model=AssetRecord)
async def inventory_get(
    asset_id: str,
    identity: RegistryActor = Depends(actor),
    registry: ProductAgentRegistry = Depends(service),
):
    return await invoke(lambda: registry.inventory.get(identity, asset_id))


@router.get("/inventory/assets/{asset_id}/history", response_model=AssetHistory)
async def inventory_history(
    asset_id: str,
    after: int = Query(0, ge=0),
    identity: RegistryActor = Depends(actor),
    registry: ProductAgentRegistry = Depends(service),
):
    return await invoke(lambda: registry.inventory.history(identity, asset_id, after))


def principal(domain: str, name: str) -> PrincipalRef:
    try:
        return PrincipalRef(trust_domain=domain, agent_name=name)
    except ValueError:
        raise HTTPException(400, detail={"code": "INVALID_AGENT_PRINCIPAL"}) from None


@router.get("/{domain}/{name}/versions", response_model=VersionsPage)
async def versions(
    domain: str,
    name: str,
    after: str = Query(""),
    identity: RegistryActor = Depends(actor),
    registry: ProductAgentRegistry = Depends(service),
):
    ref = principal(domain, name)
    return await invoke(lambda: registry.list_versions(identity, ref, after))


@router.get("/{domain}/{name}/admitted-asset", response_model=AssetRecord)
async def admitted_asset(
    domain: str,
    name: str,
    spec_digest: str = Query(..., pattern=r"^sha256:[a-f0-9]{64}$"),
    identity: RegistryActor = Depends(actor),
    registry: ProductAgentRegistry = Depends(service),
):
    ref = principal(domain, name)
    return await invoke(lambda: registry.inventory.admitted_asset(identity, ref, spec_digest))


@router.get("/{domain}/{name}/resolve", response_model=Resolution)
async def resolve(
    domain: str,
    name: str,
    version: str | None = Query(None),
    channel: str | None = Query(None),
    identity: RegistryActor = Depends(actor),
    registry: ProductAgentRegistry = Depends(service),
):
    ref = principal(domain, name)
    return await invoke(lambda: registry.resolve(identity, ref, version, channel))


@router.get("/{domain}/{name}/versions/{version}", response_model=VersionItem)
async def version(
    domain: str,
    name: str,
    version: str,
    identity: RegistryActor = Depends(actor),
    registry: ProductAgentRegistry = Depends(service),
):
    ref = principal(domain, name)
    return await invoke(lambda: registry.version(identity, ref, version))
