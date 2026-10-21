"""Owner source/target isolation using real Registry, IAM and evaluation records.

Model answers and image IDs are fixtures here, never native qualification proof.
"""

import base64
import json
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import event, select, update

from nexus.contracts.agent_hosted import RegisterHostedArtifact
from nexus.contracts.agent_native import (
    NativeDefinition,
    NativeImage,
    NativeRuntime,
    RegisterNativeConversion,
)
from nexus.contracts.product_agent_registry import canonical_bytes
from nexus.services.product_agent_registry.hosted_build import BASE
from nexus.services.product_agent_registry.native_conversions import conversions
from nexus.services.product_agent_registry.store import RegistryActor, RegistryError, digest
from tests.unit.services.test_agent_execution import execution as execution
from tests.unit.services.test_agent_execution_api import api as api
from tests.unit.services.test_agent_inventory import EVALUATOR, cmd
from tests.unit.services.test_hosted_evaluations import (
    complete_run,
    policy_request,
    setup,
    source_request,
)
from tests.unit.services.test_product_agent_registry import ADMIN, MEMBER


def prepare(execution, *, approve_source=True):
    service, request = setup(execution, count=1)
    registry = service.registry
    source = registry.hosted.get(ADMIN, request.artifact_id)
    original = registry.inventory.get(ADMIN, request.asset_id)
    if approve_source:
        baseline = service.start(ADMIN, source_request(service, request))
        complete_run(service, baseline, ["42"])
        policy = service.create_policy(ADMIN, policy_request(baseline))
        candidate = service.start(
            ADMIN,
            request.model_copy(
                update={
                    "request_id": uuid4().hex,
                    "comparison_policy_id": policy.id,
                }
            ),
        )
        complete_run(service, candidate, ["42"])
        comparison = service.compare(ADMIN, candidate.id)
        registry.inventory.attestors.add(EVALUATOR.subject)
        submitted = registry.inventory.command(
            EVALUATOR,
            cmd(
                "RecordAcceptance",
                original.revision,
                report={
                    "asset_id": original.id,
                    "metadata_digest": original.metadata_digest,
                    "spec_digest": source.spec_digest,
                    "level": "L2",
                    "expires_at": registry.clock() + 60000,
                    "execution_revision": source.image.image_id,
                    "evidence_refs": [digest(canonical_bytes(comparison.model_dump()))],
                    "checks": [
                        "entry",
                        "identity",
                        "router",
                        "revoke",
                        "tool",
                        "isolation",
                        "evaluation",
                        "trace",
                    ],
                    "evaluation_id": comparison.id,
                    "dataset_digest": comparison.dataset_digest,
                    "rubric_digest": comparison.rubric_digest,
                    "before_score": 1.0,
                    "after_score": 1.0,
                    "allowed_drop": 0.0,
                },
            ),
        )
        original = registry.inventory.command(
            ADMIN,
            cmd(
                "ReviewAsset",
                submitted.revision,
                decision="approve",
                reason="Independent source review",
            ),
        )
        assert original.actual_level == "L2"
    target = registry.inventory.command(
        ADMIN,
        cmd(
            "SaveAsset",
            asset_id="native-target",
            metadata={
                **original.metadata.model_dump(),
                "source_platform": "native",
                "source_agent_id": "target",
                "requested_level": "L3",
                "tool_refs": [],
                "data_connections": [],
            },
        ),
    )
    runtime = NativeRuntime(
        commit="a" * 40,
        executable_digest="sha256:" + "b" * 64,
        runner_digest="sha256:" + "c" * 64,
        platform="linux/amd64",
    )
    image = NativeImage(
        image_id="sha256:" + "d" * 64,
        source_digest=source.image.source_digest,
        dependency_lock_digest=digest(
            canonical_bytes({"runtime": runtime.model_dump(), "base_image": BASE})
        ),
        model_resource=source.image.model_resource,
        model_name=source.image.model_name,
        definition_digest=digest(
            canonical_bytes(NativeDefinition(plan=source.dify_plan).model_dump())
        ),
        runtime=runtime,
    )
    command = RegisterNativeConversion(
        request_id=uuid4().hex,
        source_artifact_id=source.id,
        asset_id=target.id,
        metadata_digest=target.metadata_digest,
        image=image,
        initiated_by="alice",
    )
    return registry, command, original, target


def test_native_mapping_preserves_source_and_never_grants_target_L3(execution):
    registry, command, source, target = prepare(execution)
    result = registry.native.register(ADMIN, command)
    assert result.source_asset_id == source.id and result.asset_id == target.id
    assert result.evidence == "registered_only"
    assert result.source_acceptance.level == "L2"
    assert digest(base64.b64decode(result.spec_payload_base64)) == result.spec_digest
    assert (
        digest(base64.b64decode(result.source_acceptance_payload_base64))
        == result.source_acceptance_digest
    )
    assert (
        digest(base64.b64decode(result.definition_payload_base64)) == result.image.definition_digest
    )
    assert registry.inventory.get(ADMIN, source.id) == source
    assert registry.inventory.get(ADMIN, target.id) == target
    assert registry.native.register(ADMIN, command) == result
    with pytest.raises(RegistryError, match="NATIVE_IDEMPOTENCY_CONFLICT"):
        registry.native.register(ADMIN, command.model_copy(update={"initiated_by": "different"}))
    with pytest.raises(ValidationError):
        RegisterHostedArtifact(
            request_id=uuid4().hex,
            asset_id=target.id,
            metadata_digest=target.metadata_digest,
            image=command.image,
            initiated_by="alice",
        )
    # The same caller cannot obtain L3 by copying source claims to its target.
    forged = result.source_acceptance.model_copy(
        update={
            "asset_id": target.id,
            "metadata_digest": target.metadata_digest,
            "spec_digest": result.spec_digest,
            "level": "L3",
        }
    )
    with pytest.raises(RegistryError, match="AGENT_NATIVE_CONVERSION_REQUIRED"):
        registry.inventory.command(
            EVALUATOR,
            cmd(
                "RecordAcceptance", target.revision, asset_id=target.id, report=forged.model_dump()
            ),
        )


def test_no_unreviewed_or_revoked_source_and_no_same_asset_conversion(execution):
    registry, command, source, _ = prepare(execution, approve_source=False)
    with pytest.raises(RegistryError, match="NATIVE_REVIEWED_L2_SOURCE_REQUIRED"):
        registry.native.register(ADMIN, command)


@pytest.mark.parametrize("mutation", ["suspended", "expired", "untrusted", "changed", "same_asset"])
def test_source_and_target_authority_is_rechecked(execution, mutation):
    registry, command, source, _ = prepare(execution)
    if mutation == "suspended":
        registry.inventory.command(ADMIN, cmd("SuspendAsset", source.revision, reason="Stop"))
    elif mutation == "expired":
        now = registry.clock()
        registry.clock = lambda: now + 120000
    elif mutation == "untrusted":
        registry.inventory.attestors.clear()
    elif mutation == "changed":
        registry.inventory.command(
            ADMIN,
            cmd(
                "SaveAsset",
                source.revision,
                metadata={**source.metadata.model_dump(), "purpose": "changed"},
            ),
        )
    else:
        command = command.model_copy(
            update={"asset_id": source.id, "metadata_digest": source.metadata_digest}
        )
    with pytest.raises(RegistryError):
        registry.native.register(ADMIN, command)


@pytest.mark.parametrize(
    "field",
    [
        "definition_digest",
        "dependency_lock_digest",
        "source_digest",
        "model_resource",
        "model_name",
        "max_output_tokens",
    ],
)
def test_native_material_mismatch_is_rejected(execution, field):
    registry, command, _, _ = prepare(execution)
    value = (
        32
        if field == "max_output_tokens"
        else "unapproved"
        if field.startswith("model_")
        else "sha256:" + "e" * 64
    )
    changed = command.model_copy(update={"image": command.image.model_copy(update={field: value})})
    with pytest.raises(RegistryError, match="NATIVE_MATERIAL_MISMATCH"):
        registry.native.register(ADMIN, changed)


def test_native_reads_are_pure_scoped_and_concurrent_registration_is_idempotent(execution):
    registry, command, _, target = prepare(execution)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: registry.native.register(ADMIN, command), range(2)))
    assert results[0] == results[1]
    result = results[0]
    sql = []

    def capture(_con, _cursor, statement, *_args):
        sql.append(statement.strip().split()[0].upper())

    event.listen(registry.engine, "before_cursor_execute", capture)
    try:
        assert registry.native.get(ADMIN, result.id) == result
        assert registry.native.list(ADMIN, target.id).items == [result]
        assert registry.native.profile(ADMIN, result.id).conversion_id == result.id
    finally:
        event.remove(registry.engine, "before_cursor_execute", capture)
    assert not set(sql) & {"INSERT", "UPDATE", "DELETE", "CREATE", "ALTER"}
    with pytest.raises(RegistryError):
        registry.native.get(MEMBER, result.id)
    with pytest.raises(RegistryError, match="NATIVE_CONVERSION_NOT_FOUND"):
        registry.native.get(RegistryActor(ADMIN.subject, "zone-b", True), result.id)


def test_native_api_requires_admin_and_rejects_unreviewed_source(api, execution):
    client, credentials = api
    registry, command, _, target = prepare(execution)
    path = "/api/v2/product-agents/native/conversions"
    assert (
        client.post(path, headers=credentials["member"], json=command.model_dump()).status_code
        == 403
    )
    assert (
        client.post(path, headers=credentials["foreign"], json=command.model_dump()).status_code
        == 404
    )
    response = client.post(path, headers=credentials["operator"], json=command.model_dump())
    assert response.status_code == 200, response.text
    value = response.json()
    assert value["kind"] == "NativeConversion" and value["evidence"] == "registered_only"
    assert client.get(path + "/" + value["id"], headers=credentials["operator"]).json() == value
    profile_url = path + "/" + value["id"] + "/profile"
    profile = client.get(profile_url, headers=credentials["operator"])
    assert profile.status_code == 200
    assert profile.json()["tool_policy"] == "deny_all"
    assert client.get(profile_url, headers=credentials["member"]).status_code == 403
    assert client.get(profile_url, headers=credentials["foreign"]).status_code == 404
    assert client.get(path, params={"asset_id": target.id}, headers=credentials["operator"]).json()[
        "items"
    ] == [value]
    # Reusing the generic import path cannot launder a native image as L2.
    assert (
        client.post(
            "/api/v2/product-agents/hosted/artifacts",
            headers=credentials["operator"],
            json={
                "request_id": uuid4().hex,
                "asset_id": target.id,
                "metadata_digest": target.metadata_digest,
                "image": command.image.model_dump(),
                "initiated_by": "alice",
            },
        ).status_code
        == 400
    )


def test_old_native_material_proofs_are_derived_without_rewriting_record(execution):
    registry, command, _, _ = prepare(execution)
    record = registry.native.register(ADMIN, command)
    legacy = record.model_dump(
        exclude={"definition_payload_base64", "source_acceptance_payload_base64"}
    )
    encoded = json.dumps(legacy)
    with registry.engine.begin() as con:
        con.execute(
            update(conversions).where(conversions.c.id == record.id).values(document=encoded)
        )
    assert registry.native.get(ADMIN, record.id) == record
    with registry.engine.connect() as con:
        assert (
            con.execute(
                select(conversions.c.document).where(conversions.c.id == record.id)
            ).scalar_one()
            == encoded
        )
    legacy["definition"]["plan"]["temperature"] = 0.5
    with registry.engine.begin() as con:
        con.execute(
            update(conversions)
            .where(conversions.c.id == record.id)
            .values(document=json.dumps(legacy))
        )
    with pytest.raises(RegistryError, match="NATIVE_STORED_MATERIAL_MISMATCH"):
        registry.native.get(ADMIN, record.id)
