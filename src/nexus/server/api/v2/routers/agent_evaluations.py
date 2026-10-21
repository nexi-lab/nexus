"""Evaluator-only lifecycle. Public managers call Moss; leases never go to browsers."""

from fastapi import APIRouter, Depends, Query, Request, Response
from starlette.concurrency import run_in_threadpool

from nexus.contracts.agent_evaluation import (
    CompleteHostedEvaluationCase,
    CreateMigrationPolicy,
    EvaluationCaseLease,
    HostedEvaluationCase,
    HostedEvaluationCasePage,
    HostedEvaluationClaim,
    HostedEvaluationPage,
    HostedEvaluationRun,
    MigrationComparison,
    MigrationPolicy,
    MigrationPolicyPage,
    ObserveSourceEvaluation,
    StartHostedEvaluation,
)
from nexus.contracts.agent_execution import ExecutionGrantResult
from nexus.server.api.v2.routers.agent_execution import actor, body, invoke
from nexus.server.api.v2.routers.agent_execution import service as execution_service
from nexus.services.product_agent_registry.hosted_evaluations import HostedEvaluations

router = APIRouter(prefix="/hosted/evaluations", tags=["product-agent-evaluations"])
migration_router = APIRouter(prefix="/hosted/migration", tags=["product-agent-evaluations"])


def service(response: Response, execution=Depends(execution_service)):
    response.headers["Cache-Control"] = "no-store"
    return HostedEvaluations(execution.registry, execution)


@router.post("", response_model=HostedEvaluationRun)
async def start(request: Request, identity=Depends(actor), evaluations=Depends(service)):
    await invoke(run_in_threadpool(evaluations._authorize, identity))
    command = await body(request, StartHostedEvaluation)
    return await invoke(run_in_threadpool(evaluations.start, identity, command))


@router.get("", response_model=HostedEvaluationPage)
async def list_runs(
    asset_id: str = Query(..., max_length=150),
    after: str = Query("", max_length=150),
    identity=Depends(actor),
    evaluations=Depends(service),
):
    return await invoke(run_in_threadpool(evaluations.list, identity, asset_id, after))


@router.get("/{run_id}", response_model=HostedEvaluationRun)
async def get(run_id: str, identity=Depends(actor), evaluations=Depends(service)):
    return await invoke(run_in_threadpool(evaluations.get, identity, run_id))


@router.get("/{run_id}/cases", response_model=HostedEvaluationCasePage)
async def list_cases(
    run_id: str,
    after: str = Query("", max_length=150),
    identity=Depends(actor),
    evaluations=Depends(service),
):
    return await invoke(run_in_threadpool(evaluations.list_cases, identity, run_id, after))


@router.post("/{run_id}/claim", response_model=HostedEvaluationClaim | None)
async def claim(run_id: str, identity=Depends(actor), evaluations=Depends(service)):
    return await invoke(run_in_threadpool(evaluations.claim, identity, run_id))


@router.post("/{run_id}/grant", response_model=ExecutionGrantResult)
async def grant(
    run_id: str, request: Request, identity=Depends(actor), evaluations=Depends(service)
):
    await invoke(run_in_threadpool(evaluations._authorize, identity))
    command = await body(request, EvaluationCaseLease)
    return await invoke(evaluations.issue_grant(identity, run_id, command))


@router.post("/{run_id}/source", response_model=HostedEvaluationCase)
async def observe_source(
    run_id: str, request: Request, identity=Depends(actor), evaluations=Depends(service)
):
    await invoke(run_in_threadpool(evaluations._authorize, identity))
    command = await body(request, ObserveSourceEvaluation)
    return await invoke(run_in_threadpool(evaluations.observe_source, identity, run_id, command))


@router.post("/{run_id}/complete", response_model=HostedEvaluationCase)
async def complete(
    run_id: str, request: Request, identity=Depends(actor), evaluations=Depends(service)
):
    await invoke(run_in_threadpool(evaluations._authorize, identity))
    command = await body(request, CompleteHostedEvaluationCase, limit=600000)
    return await invoke(run_in_threadpool(evaluations.complete, identity, run_id, command))


@router.post("/{run_id}/reconcile", response_model=HostedEvaluationRun)
async def reconcile(run_id: str, identity=Depends(actor), evaluations=Depends(service)):
    return await invoke(run_in_threadpool(evaluations.reconcile, identity, run_id))


@router.post("/{run_id}/stop", response_model=HostedEvaluationRun)
async def stop(run_id: str, identity=Depends(actor), evaluations=Depends(service)):
    return await invoke(run_in_threadpool(evaluations.stop, identity, run_id))


@migration_router.post("/policies", response_model=MigrationPolicy)
async def create_policy(request: Request, identity=Depends(actor), evaluations=Depends(service)):
    await invoke(run_in_threadpool(evaluations._authorize, identity))
    command = await body(request, CreateMigrationPolicy)
    return await invoke(run_in_threadpool(evaluations.create_policy, identity, command))


@migration_router.get("/policies", response_model=MigrationPolicyPage)
async def list_policies(
    asset_id: str = Query(..., max_length=150),
    after: str = Query("", max_length=150),
    identity=Depends(actor),
    evaluations=Depends(service),
):
    return await invoke(run_in_threadpool(evaluations.list_policies, identity, asset_id, after))


@migration_router.get("/policies/{policy_id}", response_model=MigrationPolicy)
async def get_policy(policy_id: str, identity=Depends(actor), evaluations=Depends(service)):
    return await invoke(run_in_threadpool(evaluations.get_policy, identity, policy_id))


@router.post("/{run_id}/compare", response_model=MigrationComparison)
async def compare(run_id: str, identity=Depends(actor), evaluations=Depends(service)):
    return await invoke(run_in_threadpool(evaluations.compare, identity, run_id))


@migration_router.get("/comparisons/{comparison_id}", response_model=MigrationComparison)
async def get_comparison(comparison_id: str, identity=Depends(actor), evaluations=Depends(service)):
    return await invoke(run_in_threadpool(evaluations.get_comparison, identity, comparison_id))
