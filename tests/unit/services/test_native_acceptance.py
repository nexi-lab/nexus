from uuid import uuid4

import pytest
from sqlalchemy import event, select, update

from nexus.contracts.agent_inventory import AcceptanceReport
from nexus.contracts.product_agent_registry import canonical_bytes
from nexus.services.product_agent_registry.hosted_evaluations import cases
from nexus.services.product_agent_registry.native_execution import execution_profile
from nexus.services.product_agent_registry.store import RegistryError, digest
from tests.unit.services.test_agent_execution import execution as execution
from tests.unit.services.test_agent_inventory import EVALUATOR, cmd
from tests.unit.services.test_hosted_evaluations import policy_request
from tests.unit.services.test_native_evaluations import complete, native_setup
from tests.unit.services.test_product_agent_registry import ADMIN


def qualified(execution):
    service, request, converted = native_setup(execution)
    baseline = service.start(ADMIN, request)
    complete(service, baseline, "42")
    policy = service.create_policy(ADMIN, policy_request(baseline))
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
    complete(service, candidate, "42")
    comparison = service.compare(ADMIN, candidate.id)
    profile_digest = digest(canonical_bytes(execution_profile(converted).model_dump()))
    report = AcceptanceReport(
        asset_id=converted.asset_id,
        metadata_digest=converted.metadata_digest,
        spec_digest=converted.spec_digest,
        level="L3",
        expires_at=converted.source_acceptance.expires_at,
        execution_revision=converted.image.image_id,
        evidence_refs=[
            profile_digest,
            converted.spec_digest,
            converted.image.runtime.executable_digest,
            converted.image.runtime.runner_digest,
            digest(canonical_bytes(comparison.model_dump())),
        ],
        checks=[
            "entry",
            "identity",
            "router",
            "revoke",
            "tool",
            "isolation",
            "evaluation",
            "trace",
            "native",
        ],
        evaluation_id=comparison.id,
        dataset_digest=comparison.dataset_digest,
        rubric_digest=comparison.rubric_digest,
        before_score=1,
        after_score=1,
        allowed_drop=0,
        native_binding={"conversion_id": converted.id, "profile_digest": profile_digest},
    )
    return service, converted, candidate, report


def test_native_l3_requires_independent_review_and_get_remains_read_only(execution):
    service, converted, _, report = qualified(execution)
    inventory = service.registry.inventory
    target = inventory.get(ADMIN, converted.asset_id)
    submitted = inventory.command(
        EVALUATOR,
        cmd("RecordAcceptance", target.revision, asset_id=target.id, report=report.model_dump()),
    )
    assert submitted.status == "submitted" and submitted.actual_level == "L0"
    reviewed = inventory.command(
        ADMIN,
        cmd(
            "ReviewAsset",
            submitted.revision,
            asset_id=target.id,
            decision="approve",
            reason="Exact native runtime, isolation, six profile bindings and actual comparison independently reviewed",
        ),
    )
    assert reviewed.actual_level == "L3" and reviewed.evidence_status == "current"
    statements = []

    def observe(_con, _cursor, statement, _params, _context, _many):
        statements.append(statement)

    event.listen(service.engine, "before_cursor_execute", observe)
    try:
        assert inventory.get(ADMIN, target.id).actual_level == "L3"
    finally:
        event.remove(service.engine, "before_cursor_execute", observe)
    assert not any(
        s.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE", "CREATE")) for s in statements
    )


@pytest.mark.parametrize(
    "patch",
    [
        {"native_binding": None},
        {"level": "L2"},
        {"execution_revision": "sha256:" + "0" * 64},
        {
            "native_binding": {
                "conversion_id": "other-conversion",
                "profile_digest": "sha256:" + "a" * 64,
            }
        },
        {"evidence_refs": ["sha256:" + "a" * 64]},
    ],
)
def test_native_acceptance_rejects_unbound_runtime_or_grade(execution, patch):
    service, converted, _, report = qualified(execution)
    target = service.registry.inventory.get(ADMIN, converted.asset_id)
    with pytest.raises(RegistryError):
        service.registry.inventory.command(
            EVALUATOR,
            cmd(
                "RecordAcceptance",
                target.revision,
                asset_id=target.id,
                report={**report.model_dump(), **patch},
            ),
        )
    assert service.registry.inventory.get(ADMIN, target.id).actual_level == "L0"


def test_native_runtime_trace_and_cleanup_are_not_replaceable_by_attestor_check_names(execution):
    service, converted, candidate, report = qualified(execution)
    target = service.registry.inventory.get(ADMIN, converted.asset_id)
    import json

    with service.engine.begin() as con:
        row = con.execute(select(cases).where(cases.c.run_id == candidate.id)).mappings().one()
        value = json.loads(row["document"])
        value["trace"] = [{"node": "llm", "phase": "completed", "origin": "worker_reported"}]
        con.execute(update(cases).where(cases.c.id == row["id"]).values(document=json.dumps(value)))
    with pytest.raises(RegistryError, match="AGENT_NATIVE_VERIFICATION_REQUIRED"):
        service.registry.inventory.command(
            EVALUATOR,
            cmd(
                "RecordAcceptance", target.revision, asset_id=target.id, report=report.model_dump()
            ),
        )


@pytest.mark.parametrize("invalidate", ["suspend", "metadata", "untrust", "expire"])
def test_native_admission_rechecks_source_without_mutating_target(execution, invalidate):
    service, converted, _, report = qualified(execution)
    inventory = service.registry.inventory
    target = inventory.get(ADMIN, converted.asset_id)
    submitted = inventory.command(
        EVALUATOR,
        cmd("RecordAcceptance", target.revision, asset_id=target.id, report=report.model_dump()),
    )
    reviewed = inventory.command(
        ADMIN,
        cmd(
            "ReviewAsset",
            submitted.revision,
            asset_id=target.id,
            decision="approve",
            reason="Independent native review",
        ),
    )
    assert reviewed.actual_level == "L3"
    source = inventory.get(ADMIN, converted.source_asset_id)
    if invalidate == "suspend":
        inventory.command(
            ADMIN, cmd("SuspendAsset", source.revision, asset_id=source.id, reason="Source revoked")
        )
    elif invalidate == "metadata":
        inventory.command(
            ADMIN,
            cmd(
                "SaveAsset",
                source.revision,
                asset_id=source.id,
                metadata={**source.metadata.model_dump(), "purpose": "Changed source scope"},
            ),
        )
    elif invalidate == "untrust":
        inventory.attestors.discard(source.acceptance_issuer)
    else:
        service.registry.clock = lambda: report.expires_at + 1
    projected = inventory.get(ADMIN, target.id)
    assert projected.actual_level == "L0" and projected.evidence_status != "current"
    assert projected.revision == reviewed.revision
    with service.engine.connect() as con, pytest.raises(RegistryError):
        inventory._validate_acceptance(reviewed, report, con=con, zone=ADMIN.zone)
