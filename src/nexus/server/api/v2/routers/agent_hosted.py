"""Hosted artifact management only. Import/inspection never starts a container."""

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError
from starlette.concurrency import run_in_threadpool

from nexus.contracts.agent_hosted import (
    DifyTextPlan,
    HostedArtifact,
    HostedArtifactPage,
    RegisterHostedArtifact,
)
from nexus.server.api.v2.routers.agent_execution import actor, body
from nexus.services.product_agent_registry.dify_import import DifyImportError, compile_dify_text
from nexus.services.product_agent_registry.store import RegistryError

router = APIRouter(prefix="/hosted", tags=["product-agent-hosted"])


def service(request: Request):
    from nexus.server.api.v2.routers.product_agent_registry import service as registry_service

    return registry_service(request).hosted


async def invoke(call):
    try:
        return await run_in_threadpool(call)
    except DifyImportError as error:
        raise HTTPException(400, detail={"code": error.code, "path": error.path}) from None
    except RegistryError as error:
        raise HTTPException(error.status, detail={"code": error.code}) from None
    except (SQLAlchemyError, ValidationError):
        raise HTTPException(503, detail={"code": "HOSTED_STORAGE_UNAVAILABLE"}) from None


@router.post("/dify/inspect", response_model=DifyTextPlan)
async def inspect_dify(request: Request, identity=Depends(actor), hosted=Depends(service)):
    await invoke(lambda: hosted.registry.inventory._authorize(identity))
    raw = bytearray()
    async for chunk in request.stream():
        if len(raw) + len(chunk) > 256000:
            raise HTTPException(413, detail={"code": "DIFY_SOURCE_SIZE"})
        raw.extend(chunk)
    return await invoke(lambda: compile_dify_text(bytes(raw)))


@router.post("/artifacts", response_model=HostedArtifact)
async def register(request: Request, identity=Depends(actor), hosted=Depends(service)):
    await invoke(lambda: hosted._authorize(identity))
    command = await body(request, RegisterHostedArtifact, limit=1024000)
    return await invoke(lambda: hosted.register(identity, command))


@router.get("/artifacts", response_model=HostedArtifactPage)
async def list_artifacts(
    asset_id: str = Query(..., max_length=150),
    after: str = Query("", max_length=150),
    identity=Depends(actor),
    hosted=Depends(service),
):
    return await invoke(lambda: hosted.list(identity, asset_id, after))


@router.get("/artifacts/{artifact_id}", response_model=HostedArtifact)
async def get_artifact(artifact_id: str, identity=Depends(actor), hosted=Depends(service)):
    return await invoke(lambda: hosted.get(identity, artifact_id))
