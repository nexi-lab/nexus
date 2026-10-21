import asyncio
from uuid import uuid4

import pytest
from pydantic import ValidationError

from nexus.contracts.agent_evaluation import (
    MigrationComparison,
    RegisterEvaluationDataset,
    SourceEvaluationRuntime,
    StartHostedEvaluation,
)
from nexus.services.product_agent_registry.hosted_evaluations import HostedEvaluations
from nexus.services.product_agent_registry.store import RegistryError
from tests.unit.services.test_agent_execution import execution as execution
from tests.unit.services.test_agent_inventory import cmd
from tests.unit.services.test_hosted_evaluations import lease, policy_request, result
from tests.unit.services.test_native_conversions import prepare
from tests.unit.services.test_product_agent_registry import ADMIN


def native_setup(execution):
    registry, command, _, target = prepare(execution)
    conversion = registry.native.register(ADMIN, command)
    submitted = registry.inventory.command(
        ADMIN, cmd("SubmitAsset", target.revision, asset_id=target.id)
    )
    approved = registry.inventory.command(
        ADMIN,
        cmd(
            "ReviewAsset",
            submitted.revision,
            asset_id=target.id,
            decision="approve",
            reason="Evaluate synthetic text under target permissions",
        ),
    )
    assert approved.actual_level == "L0"
    dataset = registry.evaluation.register(
        ADMIN,
        RegisterEvaluationDataset(
            request_id=uuid4().hex,
            asset_id=target.id,
            metadata_digest=target.metadata_digest,
            initiated_by="alice",
            dataset={
                "name": "Native comparison",
                "version": "1",
                "cases": [{"id": "one", "query": "6*7", "expected": "42"}],
            },
        ),
    )
    request = StartHostedEvaluation(
        request_id=uuid4().hex,
        asset_id=target.id,
        metadata_digest=target.metadata_digest,
        artifact_id=conversion.id,
        dataset_id=dataset.id,
        initiated_by="alice",
        execution_kind="native",
        role="source_baseline",
        source_runtime=SourceEvaluationRuntime(
            profile="registry-hosted-artifact-v1",
            source_digest=conversion.image.source_digest,
            runtime_digest=conversion.source_spec_digest,
            model_name=conversion.image.model_name,
            revision_strength="local_source_digest",
        ),
    )
    return HostedEvaluations(registry, execution), request, conversion


def complete(service, run, answer):
    claim = service.claim(ADMIN, run.id)
    grant = asyncio.run(service.issue_grant(ADMIN, run.id, lease(claim)))
    assert grant.grant.request.asset_id == run.asset_id
    assert grant.grant.request.spec_digest == run.spec_digest
    command = result(claim, answer=answer)
    if run.execution_kind == "native" and run.role == "hosted":
        from nexus.contracts.agent_evaluation import EvaluationTrace

        command = command.model_copy(
            update={
                "trace": [
                    EvaluationTrace(node="native-sudocode", phase="started"),
                    EvaluationTrace(node="native-sudocode", phase="completed"),
                ]
            }
        )
    return service.complete(ADMIN, run.id, command)


def test_native_source_candidate_policy_and_comparison_are_explicit(execution):
    service, request, conversion = native_setup(execution)
    baseline = service.start(ADMIN, request)
    assert (
        baseline.execution_kind == "native"
        and baseline.source_runtime.runtime_digest == conversion.source_spec_digest
    )
    complete(service, baseline, "42")
    policy = service.create_policy(ADMIN, policy_request(baseline))
    assert policy.execution_kind == "native" and policy.model_dump()["execution_kind"] == "native"
    candidate = service.start(
        ADMIN,
        request.model_copy(
            update={
                "request_id": uuid4().hex,
                "role": "hosted",
                "source_runtime": None,
                "comparison_policy_id": policy.id,
            }
        ),
    )
    completed = complete(service, candidate, "wrong")
    assert completed.native_verification.verdict == "passed"
    assert not completed.passed  # Runtime verification does not replace dataset grading.
    comparison = service.compare(ADMIN, candidate.id)
    assert comparison.execution_kind == "native"
    assert (
        comparison.baseline_passed == 1
        and comparison.candidate_passed == 0
        and not comparison.passed
    )
    assert service.registry.inventory.get(ADMIN, request.asset_id).actual_level == "L0"
    assert (
        HostedEvaluations(service.registry, service.execution).get_comparison(ADMIN, comparison.id)
        == comparison
    )


def test_native_source_cannot_substitute_another_runtime_or_model(execution):
    service, request, _ = native_setup(execution)
    for changed in [
        {"runtime_digest": "sha256:" + "e" * 64},
        {"source_digest": "sha256:" + "e" * 64},
        {"model_name": "other"},
        {"revision_strength": "observed_source_snapshot"},
    ]:
        command = request.model_copy(
            update={"source_runtime": request.source_runtime.model_copy(update=changed)}
        )
        with pytest.raises(RegistryError, match="EVALUATION_SOURCE_MISMATCH"):
            service.start(ADMIN, command)
    with pytest.raises(ValidationError):
        StartHostedEvaluation.model_validate({**request.model_dump(), "execution_kind": "hosted"})
    with pytest.raises(ValidationError):
        StartHostedEvaluation.model_validate({**request.model_dump(), "max_model_calls": 2})


def test_native_cannot_report_success_without_model_evidence(execution):
    service, request, _ = native_setup(execution)
    run = service.start(ADMIN, request)
    claim = service.claim(ADMIN, run.id)
    asyncio.run(service.issue_grant(ADMIN, run.id, lease(claim)))
    with pytest.raises(RegistryError, match="NATIVE_MODEL_EVIDENCE_REQUIRED"):
        service.complete(ADMIN, run.id, result(claim, model_call_ids=[]))
    assert service.get(ADMIN, run.id).passed_cases == 0


def test_old_hosted_comparison_wire_and_evidence_hashes_are_unchanged():
    fields = {
        "kind": "MigrationComparison",
        "id": "comparison",
        "zone": "zone-a",
        "asset_id": "asset",
        "policy_id": "policy",
        "baseline_run_id": "before",
        "candidate_run_id": "after",
        "metadata_digest": "sha256:" + "a" * 64,
        "spec_digest": "sha256:" + "b" * 64,
        "dataset_digest": "sha256:" + "c" * 64,
        "rubric_digest": "sha256:" + "d" * 64,
        "total_cases": 1,
        "baseline_passed": 1,
        "candidate_passed": 1,
        "minimum_pass_bps": 10000,
        "maximum_drop_bps": 0,
        "passed": True,
        "created_by": "reviewer",
        "created_at": 1000,
    }
    assert MigrationComparison.model_validate(fields).model_dump() == fields
