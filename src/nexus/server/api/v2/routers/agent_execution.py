"""Authenticated management/gateway boundary. Tokens never enter URLs or error responses."""

import json

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError
from starlette.concurrency import run_in_threadpool

from nexus.contracts.agent_execution import (
    CheckExecutionGrant,
    ExecutionAuthorization,
    ExecutionGrant,
    ExecutionGrantPage,
    ExecutionGrantResult,
    IssueExecutionGrant,
)
from nexus.contracts.product_agent_registry import MAX_BODY_BYTES
from nexus.server.dependencies import require_auth
from nexus.services.product_agent_registry.execution import AgentExecution
from nexus.services.product_agent_registry.store import RegistryActor, RegistryError

router = APIRouter(prefix="/execution", tags=["product-agent-execution"])


def actor(auth: dict = Depends(require_auth)) -> RegistryActor:
    from nexus.server.api.v2.routers.product_agent_registry import actor as registry_actor

    return registry_actor(auth)


def service(request: Request) -> AgentExecution:
    result = getattr(request.app.state, "product_agent_execution", None)
    if result is None:
        raise HTTPException(503, detail={"code": "EXECUTION_NOT_CONFIGURED"})
    return result


async def body(request, model, limit=MAX_BODY_BYTES):
    data = bytearray()
    async for chunk in request.stream():
        if len(data) + len(chunk) > limit:
            raise HTTPException(413, detail={"code": "EXECUTION_BODY_LIMIT"})
        data.extend(chunk)

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("DUPLICATE_KEY")
            result[key] = value
        return result

    try:
        return model.model_validate(json.loads(data, object_pairs_hook=unique))
    except (ValueError, RecursionError):
        raise HTTPException(400, detail={"code": "INVALID_EXECUTION_REQUEST"}) from None


async def invoke(operation):
    try:
        return await operation
    except RegistryError as error:
        raise HTTPException(error.status, detail={"code": error.code}) from None
    except (SQLAlchemyError, ValidationError):
        raise HTTPException(503, detail={"code": "EXECUTION_STORAGE_UNAVAILABLE"}) from None


@router.post("/grants", response_model=ExecutionGrantResult)
async def issue(
    request: Request,
    response: Response,
    identity: RegistryActor = Depends(actor),
    execution: AgentExecution = Depends(service),
):
    response.headers["Cache-Control"] = "no-store"
    command = await body(request, IssueExecutionGrant)
    return await invoke(execution.issue(identity, command))


@router.post("/authorize", response_model=ExecutionAuthorization)
async def authorize(
    request: Request,
    response: Response,
    identity: RegistryActor = Depends(actor),
    execution: AgentExecution = Depends(service),
):
    response.headers["Cache-Control"] = "no-store"
    command = await body(request, CheckExecutionGrant)
    return await invoke(execution.authorize(identity, command))


@router.get("/grants/{grant_id}", response_model=ExecutionGrant)
async def get(
    grant_id: str,
    response: Response,
    identity: RegistryActor = Depends(actor),
    execution: AgentExecution = Depends(service),
):
    response.headers["Cache-Control"] = "no-store"
    return await invoke(run_in_threadpool(execution.get, identity, grant_id))


@router.get("/grants", response_model=ExecutionGrantPage)
async def list_grants(
    response: Response,
    asset_id: str | None = Query(None, max_length=150),
    after: str = Query("", max_length=150),
    identity: RegistryActor = Depends(actor),
    execution: AgentExecution = Depends(service),
):
    response.headers["Cache-Control"] = "no-store"
    return await invoke(run_in_threadpool(execution.list, identity, asset_id, after))


@router.post("/grants/{grant_id}/revoke", response_model=ExecutionGrant)
async def revoke(
    grant_id: str,
    response: Response,
    identity: RegistryActor = Depends(actor),
    execution: AgentExecution = Depends(service),
):
    response.headers["Cache-Control"] = "no-store"
    return await invoke(run_in_threadpool(execution.revoke, identity, grant_id))
