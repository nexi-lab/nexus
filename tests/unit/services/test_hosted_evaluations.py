"""Durable one-attempt evaluation against real Registry/IAM, not evaluator score assertions."""

import asyncio
import hashlib
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from sqlalchemy import event, select

from nexus.contracts.agent_evaluation import (
    CompleteHostedEvaluationCase,
    CreateMigrationPolicy,
    EvaluationCaseLease,
    EvaluationDatasetInput,
    ObserveSourceEvaluation,
    RegisterEvaluationDataset,
    SourceEvaluationRuntime,
    StartHostedEvaluation,
)
from nexus.contracts.agent_hosted import HostedImage, RegisterHostedArtifact
from nexus.services.product_agent_registry.hosted_evaluations import HostedEvaluations, cases, runs
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


def source_request(service, request):
    artifact = service.registry.hosted.get(ADMIN, request.artifact_id)
    return request.model_copy(
        update={
            "role": "source_baseline",
            "source_runtime": SourceEvaluationRuntime(
                profile="dify-service-api-v1",
                source_digest=artifact.image.source_digest,
                runtime_digest="sha256:" + "c" * 64,
                model_name=artifact.image.model_name,
                revision_strength="observed_source_snapshot",
            ),
        }
    )


def complete_run(service, run, answers):
    for answer in answers:
        claim = service.claim(ADMIN, run.id)
        asyncio.run(service.issue_grant(ADMIN, run.id, lease(claim)))
        if run.role == "source_baseline":
            service.observe_source(ADMIN, run.id, source_observation(claim))
        service.complete(ADMIN, run.id, result(claim, answer=answer))


def source_observation(claim, **patch):
    return ObserveSourceEvaluation(
        **lease(claim).model_dump(),
        source_execution={
            "profile": "dify-service-api-v1",
            "request_id": claim.case.id,
            "workflow_run_id": "workflow-run",
            "message_id": "message",
            **patch,
        },
    )


def policy_request(baseline, **patch):
    return CreateMigrationPolicy(
        **{
            "request_id": uuid4().hex,
            "baseline_run_id": baseline.id,
            "minimum_pass_bps": 10000,
            "maximum_drop_bps": 0,
            "initiated_by": "alice",
            **patch,
        }
    )


def test_source_baseline_policy_and_owner_comparison_survive_restart(execution):
    service, request = setup(execution)
    baseline = service.start(ADMIN, source_request(service, request))
    with pytest.raises(RegistryError, match="NOT_SETTLED"):
        service.create_policy(ADMIN, policy_request(baseline))
    complete_run(service, baseline, ["42", "42"])
    command = policy_request(baseline)
    policy = service.create_policy(ADMIN, command)
    assert service.create_policy(ADMIN, command) == policy
    with pytest.raises(RegistryError, match="IDEMPOTENCY_CONFLICT"):
        service.create_policy(ADMIN, command.model_copy(update={"maximum_drop_bps": 10000}))
    candidate = service.start(
        ADMIN,
        request.model_copy(
            update={
                "request_id": uuid4().hex,
                "comparison_policy_id": policy.id,
            }
        ),
    )
    complete_run(service, candidate, ["42", "wrong"])
    comparison = service.compare(ADMIN, candidate.id)
    assert (comparison.total_cases, comparison.baseline_passed, comparison.candidate_passed) == (
        2,
        2,
        1,
    )
    assert not comparison.passed
    assert service.compare(ADMIN, candidate.id) == comparison
    restarted = HostedEvaluations(service.registry, execution)
    assert restarted.get_policy(ADMIN, policy.id) == policy
    assert restarted.get_comparison(ADMIN, comparison.id) == comparison
    assert restarted.list_policies(ADMIN, request.asset_id).items == [policy]
    assert service.registry.inventory.get(ADMIN, request.asset_id).actual_level == "L0"


def test_comparison_cannot_use_hosted_as_source_or_attach_later_policy(execution):
    service, request = setup(execution)
    old_hosted = service.start(ADMIN, request)
    complete_run(service, old_hosted, ["42", "42"])
    with pytest.raises(RegistryError, match="SOURCE_BASELINE_REQUIRED"):
        service.create_policy(ADMIN, policy_request(old_hosted))
    with pytest.raises(RegistryError, match="POLICY_REQUIRED"):
        service.compare(ADMIN, old_hosted.id)
    source = source_request(service, request.model_copy(update={"request_id": uuid4().hex}))
    invalid = source.model_copy(
        update={
            "source_runtime": source.source_runtime.model_copy(update={"model_name": "other-model"})
        }
    )
    with pytest.raises(RegistryError, match="SOURCE_MISMATCH"):
        service.start(ADMIN, invalid)
    baseline = service.start(ADMIN, source)
    claim = service.claim(ADMIN, baseline.id)
    service.complete(
        ADMIN,
        baseline.id,
        result(
            claim,
            status="unknown",
            answer=None,
            error_code="UNKNOWN",
            cleanup_confirmed=False,
            model_call_ids=[],
        ),
    )
    service.stop(ADMIN, baseline.id)
    with pytest.raises(RegistryError, match="NOT_SETTLED"):
        service.create_policy(ADMIN, policy_request(baseline))
    with pytest.raises(RegistryError):
        service.get_policy(MEMBER, "not-found")


def test_policy_comparison_uses_integer_all_case_thresholds(api, execution):
    service, request = setup(execution, count=3)
    baseline = service.start(ADMIN, source_request(service, request))
    complete_run(service, baseline, ["42", "42", "42"])
    client, credentials = api
    headers = credentials["operator"]
    base = "/api/v2/product-agents/hosted/migration"
    command = policy_request(baseline, minimum_pass_bps=6667, maximum_drop_bps=3334)
    response = client.post(base + "/policies", headers=headers, json=command.model_dump())
    assert response.status_code == 200
    policy = response.json()
    assert response.headers["cache-control"] == "no-store"
    assert client.get(base + "/policies/" + policy["id"], headers=headers).json() == policy
    assert client.get(
        base + "/policies", headers=headers, params={"asset_id": request.asset_id}
    ).json()["items"] == [policy]
    assert (
        client.post(
            base + "/policies", headers=headers, json={**command.model_dump(), "before_score": 1}
        ).status_code
        == 400
    )
    for answers, passed in [(["42", "42", "no"], False), (["42", "42", "42"], True)]:
        candidate = service.start(
            ADMIN,
            request.model_copy(
                update={
                    "request_id": uuid4().hex,
                    "comparison_policy_id": policy["id"],
                }
            ),
        )
        complete_run(service, candidate, answers)
        result = client.post(
            "/api/v2/product-agents/hosted/evaluations/" + candidate.id + "/compare",
            headers=headers,
        )
        assert result.status_code == 200
        assert result.json()["passed"] == passed
        assert (
            client.get(base + "/comparisons/" + result.json()["id"], headers=headers).json()
            == result.json()
        )
    assert (
        client.get(base + "/policies/" + policy["id"], headers=credentials["member"]).status_code
        == 403
    )


def test_legacy_hosted_start_fingerprint_is_preserved(execution):
    from nexus.contracts.product_agent_registry import canonical_bytes
    from nexus.services.product_agent_registry.store import digest

    service, request = setup(execution)
    run = service.start(ADMIN, request)
    legacy_command = request.model_dump(exclude={"role", "source_runtime", "comparison_policy_id"})
    expected = digest(canonical_bytes({"actor": ADMIN.subject, "command": legacy_command}))
    with service.engine.connect() as con:
        actual = con.execute(select(runs.c.request_digest).where(runs.c.id == run.id)).scalar_one()
    assert actual == expected
    assert service.start(ADMIN, request).id == run.id


def test_source_references_survive_failure_and_reject_overwrite(execution):
    service, request = setup(execution)
    run = service.start(ADMIN, source_request(service, request))
    claim = service.claim(ADMIN, run.id)
    asyncio.run(service.issue_grant(ADMIN, run.id, lease(claim)))
    with pytest.raises(RegistryError, match="SOURCE_EVIDENCE_REQUIRED"):
        service.complete(ADMIN, run.id, result(claim))
    command = source_observation(claim)
    observed = service.observe_source(ADMIN, run.id, command)
    assert service.observe_source(ADMIN, run.id, command) == observed
    with pytest.raises(RegistryError, match="REFERENCE_CHANGED"):
        service.observe_source(ADMIN, run.id, source_observation(claim, message_id="another"))
    with pytest.raises(RegistryError, match="SOURCE_MISMATCH"):
        service.observe_source(ADMIN, run.id, source_observation(claim, request_id="another"))
    with pytest.raises(RegistryError):
        service.observe_source(MEMBER, run.id, command)
    completed = service.complete(
        ADMIN,
        run.id,
        result(
            claim,
            status="unknown",
            answer=None,
            error_code="SOURCE_CHANGED",
            cleanup_confirmed=False,
        ),
    )
    assert completed.source_execution == observed.source_execution
    restarted = HostedEvaluations(service.registry, execution)
    assert restarted.list_cases(ADMIN, run.id).items[0].source_execution is not None
    with pytest.raises(RegistryError, match="EXPIRED"):
        service.observe_source(ADMIN, run.id, command)
