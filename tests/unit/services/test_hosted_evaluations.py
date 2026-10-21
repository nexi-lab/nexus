"""Durable one-attempt evaluation against real Registry/IAM, not evaluator score assertions."""

import asyncio
import hashlib
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from sqlalchemy import event, select

from nexus.contracts.agent_evaluation import (
    CompleteHostedEvaluationCase,
    EvaluationCaseLease,
    EvaluationDatasetInput,
    RegisterEvaluationDataset,
    StartHostedEvaluation,
)
from nexus.contracts.agent_hosted import HostedImage, RegisterHostedArtifact
from nexus.services.product_agent_registry.hosted_evaluations import HostedEvaluations, cases
from nexus.services.product_agent_registry.store import RegistryActor, RegistryError
from tests.unit.services.test_agent_execution import MODEL
from tests.unit.services.test_agent_execution import execution as execution
from tests.unit.services.test_agent_execution_api import api as api
from tests.unit.services.test_agent_inventory import cmd
from tests.unit.services.test_dify_import import SOURCE
from tests.unit.services.test_product_agent_registry import ADMIN, MEMBER


def setup(execution, count=2):
    registry = execution.registry
    asset = registry.inventory.get(ADMIN, "asset-a")
    asset = registry.inventory.command(
        ADMIN,
        cmd(
            "SaveAsset",
            asset.revision,
            metadata={
                **asset.metadata.model_dump(),
                "hosting": "internal_hosted",
                "requested_level": "L2",
            },
        ),
    )
    asset = registry.inventory.command(ADMIN, cmd("SubmitAsset", asset.revision))
    asset = registry.inventory.command(
        ADMIN, cmd("ReviewAsset", asset.revision, decision="approve", reason="Synthetic only")
    )
    artifact = registry.hosted.register(
        ADMIN,
        RegisterHostedArtifact(
            request_id=uuid4().hex,
            asset_id=asset.id,
            metadata_digest=asset.metadata_digest,
            initiated_by="alice",
            dify_source=SOURCE.read_text(),
            image=HostedImage(
                profile="dify-0.6.0-linear-text-v1",
                image_id="sha256:" + "a" * 64,
                source_digest="sha256:" + hashlib.sha256(SOURCE.read_bytes()).hexdigest(),
                dependency_lock_digest="sha256:" + "b" * 64,
                model_resource=MODEL.resource,
                model_name="gemini-3-flash-preview",
            ),
        ),
    )
    dataset = registry.evaluation.register(
        ADMIN,
        RegisterEvaluationDataset(
            request_id=uuid4().hex,
            asset_id=asset.id,
            metadata_digest=asset.metadata_digest,
            initiated_by="alice",
            dataset=EvaluationDatasetInput(
                name="Arithmetic",
                version="1",
                cases=[
                    {"id": f"sample-{i}", "query": "6*7", "expected": "42"} for i in range(count)
                ],
            ),
        ),
    )
    return HostedEvaluations(registry, execution), StartHostedEvaluation(
        request_id=uuid4().hex,
        asset_id=asset.id,
        metadata_digest=asset.metadata_digest,
        artifact_id=artifact.id,
        dataset_id=dataset.id,
        initiated_by="alice",
    )


def lease(claim):
    return EvaluationCaseLease(case_id=claim.case.id, lease_token=claim.lease_token)


def result(claim, **patch):
    return CompleteHostedEvaluationCase(
        **{
            **lease(claim).model_dump(),
            "status": "succeeded",
            "answer": " 42\n",
            "cleanup_confirmed": True,
            "trace": [{"node": "llm", "phase": "completed", "origin": "worker_reported"}],
            "model_call_ids": ["call-one"],
            **patch,
        }
    )


def test_success_wrong_answer_denominator_replay_and_restart(execution):
    service, req = setup(execution)
    run = service.start(ADMIN, req)
    assert service.start(ADMIN, req).id == run.id
    for answer in ["42\t", "\u00a042"]:
        claim = service.claim(ADMIN, run.id)
        grant = asyncio.run(service.issue_grant(ADMIN, run.id, lease(claim)))
        assert grant.api_key
        assert asyncio.run(service.issue_grant(ADMIN, run.id, lease(claim))).api_key is None
        completed = service.complete(ADMIN, run.id, result(claim, answer=answer))
        assert completed.iam_cleanup_confirmed and completed.cleanup_confirmed
        assert not asyncio.run(execution.auth.authenticate(grant.api_key)).authenticated
        assert service.complete(ADMIN, run.id, result(claim, answer=answer)) == completed
        with pytest.raises(RegistryError, match="CONFLICT"):
            service.complete(ADMIN, run.id, result(claim, answer="changed"))
    restarted = HostedEvaluations(execution.registry, execution)
    finished = restarted.get(ADMIN, run.id)
    assert finished.status == "finished" and finished.total_cases == 2
    assert finished.passed_cases == 1 and finished.finished_cases == 2
    assert restarted.claim(ADMIN, run.id) is None
    assert execution.registry.inventory.get(ADMIN, req.asset_id).actual_level == "L0"


def test_claim_race_secret_scoping_and_pure_reads(execution):
    service, req = setup(execution)
    run = service.start(ADMIN, req)

    def claim(_):
        try:
            return service.claim(ADMIN, run.id)
        except RegistryError as error:
            assert error.code == "EVALUATION_CASE_IN_PROGRESS"
            return None

    with ThreadPoolExecutor(max_workers=4) as pool:
        wins = [x for x in pool.map(claim, range(4)) if x]
    assert len(wins) == 1
    assert wins[0].lease_token not in repr(wins[0])
    with execution.engine.connect() as con:
        assert wins[0].lease_token not in str(con.execute(select(cases)).all())
    for actor in [
        MEMBER,
        RegistryActor(ADMIN.subject, "zone-b", True),
        RegistryActor("user:fake", "zone-a", True),
    ]:
        with pytest.raises(RegistryError):
            service.get(actor, run.id)
    with pytest.raises(RegistryError, match="CLAIM_DENIED"):
        service.complete(ADMIN, run.id, result(wins[0], lease_token="x" * 32))
    sql = []

    def capture(_con, _cursor, statement, *_args):
        sql.append(statement.strip().split()[0].upper())

    event.listen(execution.engine, "before_cursor_execute", capture)
    try:
        service.get(ADMIN, run.id)
        service.list(ADMIN, req.asset_id)
        service.list_cases(ADMIN, run.id)
    finally:
        event.remove(execution.engine, "before_cursor_execute", capture)
    assert not set(sql) & {"UPDATE", "INSERT", "DELETE", "CREATE", "ALTER"}


def test_timeout_reconcile_revokes_iam_without_retry_or_cleanup_lie(execution):
    service, req = setup(execution)
    run = service.start(ADMIN, req)
    claim = service.claim(ADMIN, run.id)
    grant = asyncio.run(service.issue_grant(ADMIN, run.id, lease(claim)))
    execution.registry.clock = lambda: claim.case.deadline_at + 1
    # GET cannot close a case or issue schema/publication writes.
    assert service.get(ADMIN, run.id).running_cases == 1
    with pytest.raises(RegistryError, match="EXPIRED"):
        service.complete(ADMIN, run.id, result(claim))
    service.reconcile(ADMIN, run.id)
    rows = service.list_cases(ADMIN, run.id).items
    unknown = next(x for x in rows if x.status == "unknown")
    assert not unknown.cleanup_confirmed and unknown.iam_cleanup_confirmed
    assert unknown.grant_id == grant.grant.id
    assert not asyncio.run(execution.auth.authenticate(grant.api_key)).authenticated
    with pytest.raises(RegistryError, match="CLEANUP_UNCONFIRMED"):
        service.claim(ADMIN, run.id)
    assert service.reconcile(ADMIN, run.id).passed_cases == 0


def test_failure_all_samples_count_and_asset_stop_still_allows_recording(execution):
    service, req = setup(execution)
    run = service.start(ADMIN, req)
    claim = service.claim(ADMIN, run.id)
    asset = execution.registry.inventory.get(ADMIN, req.asset_id)
    execution.registry.inventory.command(ADMIN, cmd("SuspendAsset", asset.revision, reason="stop"))
    failed = service.complete(
        ADMIN,
        run.id,
        result(
            claim,
            status="failed",
            answer=None,
            model_call_ids=[],
            trace=[],
            error_code="ASSET_STOPPED",
        ),
    )
    assert not failed.passed and failed.iam_cleanup_confirmed
    with pytest.raises(RegistryError, match="NOT_ADMITTED"):
        service.claim(ADMIN, run.id)
    assert service.get(ADMIN, run.id).total_cases == 2


def test_capacity_idempotency_policy_and_no_unbound_success(execution):
    service, req = setup(execution, count=100)
    run = service.start(ADMIN, req)
    service.start(ADMIN, req.model_copy(update={"request_id": uuid4().hex}))
    with pytest.raises(RegistryError, match="CAPACITY_LIMIT"):
        service.start(ADMIN, req.model_copy(update={"request_id": uuid4().hex}))
    with pytest.raises(RegistryError, match="IDEMPOTENCY_CONFLICT"):
        service.start(ADMIN, req.model_copy(update={"initiated_by": "different"}))
    claim = service.claim(ADMIN, run.id)
    with pytest.raises(RegistryError, match="GRANT_REQUIRED"):
        service.complete(ADMIN, run.id, result(claim))
    assert service.get(ADMIN, run.id).passed_cases == 0


def test_evaluator_http_boundary_never_exposes_lease_in_get(api, execution):
    service, req = setup(execution)
    client, credentials = api
    headers = credentials["operator"]
    url = "/api/v2/product-agents/hosted/evaluations"
    r = client.post(url, headers=headers, json=req.model_dump())
    assert r.status_code == 200, r.text
    assert r.headers["cache-control"] == "no-store"
    run_id = r.json()["id"]
    for name in ["member", "foreign"]:
        assert client.post(url, headers=credentials[name], json=req.model_dump()).status_code == 403
    assert client.post(url, json=req.model_dump()).status_code == 401
    claim = client.post(url + f"/{run_id}/claim", headers=headers).json()
    r = client.get(url + f"/{run_id}/cases", headers=headers)
    assert claim["lease_token"] not in r.text and "lease_token" not in r.text
    r = client.post(
        url + f"/{run_id}/grant",
        headers=headers,
        json={"case_id": claim["case"]["id"], "lease_token": claim["lease_token"]},
    )
    assert r.status_code == 200 and r.json()["api_key"]
    assert (
        client.post(
            url, headers=headers, json={**req.model_dump(), "passed_cases": 100}
        ).status_code
        == 400
    )


def test_stop_revokes_running_and_closes_queued_without_replaying(execution):
    service, req = setup(execution)
    run = service.start(ADMIN, req)
    claim = service.claim(ADMIN, run.id)
    grant = asyncio.run(service.issue_grant(ADMIN, run.id, lease(claim)))
    stopped = service.stop(ADMIN, run.id)
    assert stopped.status == "finished" and stopped.finished_cases == 2
    assert stopped.passed_cases == 0
    assert not asyncio.run(execution.auth.authenticate(grant.api_key)).authenticated
    with pytest.raises(RegistryError, match="CONFLICT"):
        service.complete(ADMIN, run.id, result(claim))
    assert service.stop(ADMIN, run.id) == stopped
