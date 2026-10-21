"""Real inventory transactions and Registry admission; no mocked database or privilege flags."""

from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from sqlalchemy import event

from nexus.contracts.agent_inventory import ASSET_COMMAND, parse_asset_command
from nexus.services.product_agent_registry.inventory import AgentInventory
from nexus.services.product_agent_registry.store import RegistryActor, RegistryError
from tests.unit.services.test_product_agent_registry import (
    ADMIN,
    MEMBER,
    REF,
    command,
    publish,
    register,
)
from tests.unit.services.test_product_agent_registry import registry as registry

SPEC = "sha256:" + "a" * 64
EVALUATOR = RegistryActor(subject="agent:acceptance-runner", zone="zone-a", is_admin=True)


def values(**patch):
    return dict(
        name="Support",
        source_platform="dify",
        source_instance="platform-1",
        source_agent_id="app-1",
        owner_user_id="alice",
        department_id="support",
        purpose="Synthetic support answers",
        domain="office",
        hosting="external_hosted",
        requested_level="L1",
        **patch,
    )


def cmd(kind, revision=0, asset_id="asset-a", **patch):
    return ASSET_COMMAND.validate_python(
        dict(
            api_version="agent-registry.sudo.dev/v1alpha1",
            kind=kind,
            request_id=uuid4().hex,
            asset_id=asset_id,
            expected_revision=revision,
            initiated_by="alice",
            **patch,
        )
    )


def save(registry, **patch):
    return registry.inventory.command(ADMIN, cmd("SaveAsset", metadata={**values(), **patch}))


def report(record, spec=SPEC, **patch):
    return dict(
        asset_id=record.id,
        metadata_digest=record.metadata_digest,
        spec_digest=spec,
        level="L1",
        expires_at=2000,
        evidence_refs=[SPEC],
        checks=["entry", "identity", "router", "revoke"],
        **patch,
    )


def accept(registry, asset, **patch):
    registry.inventory.attestors.add(EVALUATOR.subject)
    result = registry.inventory.command(
        EVALUATOR, cmd("RecordAcceptance", asset.revision, report=report(asset, **patch))
    )
    return registry.inventory.command(
        ADMIN,
        cmd(
            "ReviewAsset",
            result.revision,
            decision="approve",
            reason="Reviewed acceptance evidence",
        ),
    )


def bind(registry, asset):
    register(registry)
    return registry.inventory.command(
        ADMIN, cmd("LinkAsset", asset.revision, principal=REF.model_dump())
    )


def test_registration_return_resubmit_approve_is_only_L0(registry):
    asset = save(registry)
    assert asset.actual_level == "L0" and asset.recommended_level == "L1"
    submitted = registry.inventory.command(ADMIN, cmd("SubmitAsset", asset.revision))
    returned = registry.inventory.command(
        ADMIN,
        cmd(
            "ReviewAsset",
            submitted.revision,
            decision="return",
            reason="Clarify owner and data scope",
        ),
    )
    assert returned.status == "returned"
    edited = registry.inventory.command(
        ADMIN,
        cmd(
            "SaveAsset",
            returned.revision,
            metadata={**values(), "purpose": "Clarified synthetic inputs"},
        ),
    )
    submitted = registry.inventory.command(ADMIN, cmd("SubmitAsset", edited.revision))
    approved = registry.inventory.command(
        ADMIN,
        cmd("ReviewAsset", submitted.revision, decision="approve", reason="Registration approved"),
    )
    assert approved.status == "approved" and approved.actual_level == "L0"
    assert len(registry.inventory.history(ADMIN, asset.id).events) == 6
    assert all(
        e.actor == ADMIN.subject and e.initiated_by == "alice"
        for e in registry.inventory.history(ADMIN, asset.id).events
    )


def test_discovery_deduplicates_evidence_and_source_identity(registry):
    obs = {
        "source": "router",
        "source_id": "router-1",
        "record_id": "request-1",
        "snapshot_digest": SPEC,
        "observed_at": 1000,
        "external_domain": True,
    }
    created = registry.inventory.command(
        ADMIN, cmd("ObserveAsset", metadata=values(), observation=obs)
    )
    repeated = registry.inventory.command(
        ADMIN, cmd("ObserveAsset", created.revision, metadata=values(), observation=obs)
    )
    assert (
        repeated.external_domain and repeated.risk == "external" and len(repeated.observations) == 1
    )
    with pytest.raises(RegistryError, match="IDENTITY_CONFLICT"):
        registry.inventory.command(ADMIN, cmd("SaveAsset", asset_id="duplicate", metadata=values()))
    assert len(registry.inventory.list(ADMIN).items) == 1


def test_untrusted_member_foreign_zone_and_field_escalation_are_refused(registry):
    created = save(registry)
    with pytest.raises(RegistryError, match="FORBIDDEN"):
        registry.inventory.get(MEMBER, created.id)
    foreign = RegistryActor(subject=ADMIN.subject, zone="zone-b", is_admin=True)
    with pytest.raises(RegistryError, match="NOT_FOUND"):
        registry.inventory.get(foreign, created.id)
    assert registry.inventory.list(foreign).items == []
    for patch in [
        {"actual_level": "L2"},
        {"owner_zone": "zone-b"},
        {"is_admin": True},
        {"metadata": {**values(), "api_key": "not-accepted"}},
    ]:
        body = {**cmd("SaveAsset", metadata=values()).model_dump(), **patch}
        with pytest.raises(ValueError):
            ASSET_COMMAND.validate_python(body)
    with pytest.raises(ValueError):
        parse_asset_command(b'{"kind":"SaveAsset","kind":"RecordAcceptance"}')


def test_all_reads_are_read_only_and_restart_preserves_history(registry):
    asset = save(registry)
    statements = []

    def capture(_con, _cursor, sql, *_args):
        statements.append(sql.strip().split()[0].upper())

    event.listen(registry.engine, "before_cursor_execute", capture)
    try:
        registry.inventory.get(ADMIN, asset.id)
        registry.inventory.list(ADMIN)
        registry.inventory.history(ADMIN, asset.id)
        restarted = AgentInventory(registry)
        assert restarted.get(ADMIN, asset.id) == asset
    finally:
        event.remove(registry.engine, "before_cursor_execute", capture)
    assert not {"INSERT", "DELETE", "UPDATE", "CREATE", "ALTER"} & set(statements)


def test_optimistic_revision_and_exact_idempotency_under_concurrency(registry):
    request = cmd("SaveAsset", metadata=values())
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: registry.inventory.command(ADMIN, request), range(2)))
    assert results[0] == results[1]
    updated = registry.inventory.command(
        ADMIN, cmd("SaveAsset", 1, metadata={**values(), "name": "Changed"})
    )
    with pytest.raises(RegistryError, match="REVISION_CONFLICT"):
        registry.inventory.command(ADMIN, cmd("SubmitAsset", 1))
    changed = request.model_copy(update={"initiated_by": "mallory"})
    with pytest.raises(RegistryError, match="IDEMPOTENCY_CONFLICT"):
        registry.inventory.command(ADMIN, changed)
    assert updated.revision == 2


def test_merge_preserves_both_records_and_requires_new_review(registry):
    first = save(registry)
    second = registry.inventory.command(
        ADMIN,
        cmd("SaveAsset", asset_id="asset-b", metadata={**values(), "source_agent_id": "app-alias"}),
    )
    result = registry.inventory.command(
        ADMIN,
        cmd(
            "MergeAsset",
            first.revision,
            target_asset_id=second.id,
            target_revision=second.revision,
            reason="Same business agent, confirmed alias",
        ),
    )
    assert result.status == "merged" and result.merged_into == second.id
    assert registry.inventory.get(ADMIN, second.id).revision == 2
    assert len(registry.inventory.list(ADMIN).items) == 2
    with pytest.raises(RegistryError, match="MERGED"):
        registry.inventory.command(ADMIN, cmd("SubmitAsset", result.revision))


def test_linked_asset_cannot_be_merged_or_linked_to_another_principal(registry):
    asset = bind(registry, save(registry))
    second = registry.inventory.command(
        ADMIN,
        cmd("SaveAsset", asset_id="asset-b", metadata={**values(), "source_agent_id": "app-b"}),
    )
    with pytest.raises(RegistryError, match="MERGE_CONFLICT"):
        registry.inventory.command(
            ADMIN,
            cmd(
                "MergeAsset",
                asset.revision,
                target_asset_id=second.id,
                target_revision=second.revision,
                reason="Not allowed for live authorities",
            ),
        )


def test_public_saas_and_core_risk_are_explicit(registry):
    with pytest.raises(ValueError):
        save(registry, hosting="public_saas")
    asset = save(registry, hosting="public_saas", requested_level="L0")
    assert asset.actual_level == "L0" and asset.risk == "external"
    assert asset.recommendation_reasons == ["public_saas_outside_execution_control"]
    core = registry.inventory.command(
        ADMIN,
        cmd(
            "SaveAsset",
            asset_id="asset-core",
            metadata={
                **values(),
                "source_agent_id": "core-app",
                "data_connections": [
                    {"reference": "customer-ledger", "classification": "core", "actions": ["read"]}
                ],
            },
        ),
    )
    assert core.recommended_level == "L2" and core.risk == "high"


def test_acceptance_only_trusted_issuer_exact_metadata_and_review(registry):
    asset = save(registry)
    with pytest.raises(RegistryError, match="ISSUER_FORBIDDEN"):
        registry.inventory.command(
            ADMIN, cmd("RecordAcceptance", asset.revision, report=report(asset))
        )
    registry.inventory.attestors.add(EVALUATOR.subject)
    with pytest.raises(RegistryError, match="MISMATCH"):
        registry.inventory.command(
            EVALUATOR,
            cmd(
                "RecordAcceptance",
                asset.revision,
                report={**report(asset), "metadata_digest": "sha256:" + "f" * 64},
            ),
        )
    accepted = accept(registry, asset)
    assert accepted.actual_level == "L1" and accepted.acceptance_issuer == EVALUATOR.subject
    changed = registry.inventory.command(
        ADMIN,
        cmd("SaveAsset", accepted.revision, metadata={**values(), "model_paths": ["other-model"]}),
    )
    assert changed.actual_level == "L0" and changed.acceptance is None and changed.status == "draft"


def test_publication_approval_channel_and_resolve_use_same_admission(registry):
    asset = bind(registry, save(registry))
    with pytest.raises(RegistryError, match="NOT_ADMITTED"):
        registry.command(ADMIN, publish())
    asset = accept(registry, asset)
    registry.command(ADMIN, publish())
    registry.command(
        ADMIN,
        command(
            "DecideVersion",
            version="v1",
            decision="approved",
            reason="Publication review",
            expires_at=2000,
        ),
    )
    registry.command(
        ADMIN,
        command(
            "MoveChannel", channel="stable", version="v1", expected_revision=0, reason="Release"
        ),
    )
    assert registry.resolve(MEMBER, REF).version.manifest.spec_digest == SPEC
    registry.inventory.command(
        ADMIN, cmd("SuspendAsset", asset.revision, reason="Stop all controlled entry points")
    )
    with pytest.raises(RegistryError, match="NOT_ADMITTED"):
        registry.resolve(MEMBER, REF)
    with pytest.raises(RegistryError, match="NOT_ADMITTED"):
        registry.command(
            ADMIN,
            command(
                "MoveChannel",
                channel="canary",
                version="v1",
                expected_revision=0,
                reason="Cannot bypass stop",
            ),
        )
    with pytest.raises(RegistryError, match="NOT_ADMITTED"):
        registry.command(
            ADMIN,
            command(
                "DecideVersion",
                version="v1",
                decision="approved",
                reason="Cannot bypass stop",
                expires_at=2000,
            ),
        )


def test_enforced_zone_refuses_unregistered_principal_and_core_L1(registry):
    registry.inventory = AgentInventory(
        registry, zone_domains={"zone-a": "core"}, attestors={EVALUATOR.subject}
    )
    register(registry)
    with pytest.raises(RegistryError, match="REGISTRATION_REQUIRED"):
        registry.command(ADMIN, publish())
    asset = save(registry)
    assert asset.recommended_level == "L2"
    asset = registry.inventory.command(
        ADMIN, cmd("LinkAsset", asset.revision, principal=REF.model_dump())
    )
    accept(registry, asset)
    with pytest.raises(RegistryError, match="CORE_REQUIRES_L2"):
        registry.command(ADMIN, publish())


def test_L2_requires_internal_runtime_permissions_and_same_dataset_scores(registry):
    asset = bind(
        registry, save(registry, domain="core", hosting="internal_hosted", requested_level="L2")
    )
    good = {
        **report(asset),
        "level": "L2",
        "execution_revision": SPEC,
        "checks": ["entry", "identity", "router", "revoke", "isolation", "evaluation", "trace"],
        "evaluation_id": "evaluation-1",
        "dataset_digest": SPEC,
        "rubric_digest": SPEC,
        "before_score": 1.0,
        "after_score": 1.0,
        "allowed_drop": 0.0,
    }
    registry.inventory.attestors.add(EVALUATOR.subject)
    for patch, code in [
        ({"execution_revision": None}, "HOSTED_EVIDENCE"),
        ({"checks": ["entry", "identity", "router", "revoke"]}, "MISMATCH"),
        ({"after_score": 0.5}, "EVALUATION_FAILED"),
        ({"expires_at": 1000}, "EXPIRED"),
    ]:
        with pytest.raises(RegistryError, match=code):
            registry.inventory.command(
                EVALUATOR, cmd("RecordAcceptance", asset.revision, report={**good, **patch})
            )
    result = registry.inventory.command(
        EVALUATOR, cmd("RecordAcceptance", asset.revision, report=good)
    )
    registry.inventory.command(
        ADMIN,
        cmd(
            "ReviewAsset",
            result.revision,
            decision="approve",
            reason="Same dataset and permissions checked",
        ),
    )
    registry.command(ADMIN, publish())


def test_expired_evidence_and_new_source_version_fail_closed(registry):
    accept(registry, bind(registry, save(registry)))
    with pytest.raises(RegistryError, match="VERSION_MISMATCH"):
        registry.inventory.admission(MEMBER, REF, "sha256:" + "b" * 64)
    registry.clock = lambda: 2000
    with pytest.raises(RegistryError, match="EXPIRED"):
        registry.inventory.admission(MEMBER, REF, SPEC)


def test_new_acceptance_preserves_other_reviewed_version(registry):
    asset = accept(registry, bind(registry, save(registry)))
    new_spec = "sha256:" + "b" * 64
    accept(registry, asset, spec=new_spec)
    assert registry.inventory.admission(MEMBER, REF, SPEC)
    assert registry.inventory.admission(MEMBER, REF, new_spec)


def test_owner_filter_and_pagination(registry):
    save(registry)
    assert len(registry.inventory.list(ADMIN, owner="alice").items) == 1
    assert registry.inventory.list(ADMIN, owner="bob").items == []
    assert registry.inventory.list(ADMIN, after="asset-a").items == []
    assert registry.inventory.history(ADMIN, "asset-a", after=1).events == []


def test_new_core_zone_policy_applies_to_existing_assets_without_rewrite(registry):
    asset = accept(registry, bind(registry, save(registry)))
    registry.inventory = AgentInventory(
        registry, zone_domains={"zone-a": "core"}, attestors={EVALUATOR.subject}
    )
    assert registry.inventory.get(ADMIN, asset.id).recommended_level == "L2"
    assert registry.inventory.get(ADMIN, asset.id).effective_domain == "core"
    with pytest.raises(RegistryError, match="CORE_REQUIRES_L2"):
        registry.inventory.admission(MEMBER, REF, SPEC)


def test_core_asset_cannot_hide_risk_by_editing_or_merging(registry):
    asset = save(registry, domain="core")
    with pytest.raises(RegistryError, match="RISK_REDUCTION"):
        registry.inventory.command(ADMIN, cmd("SaveAsset", asset.revision, metadata=values()))
    target = registry.inventory.command(
        ADMIN,
        cmd(
            "SaveAsset",
            asset_id="office-asset",
            metadata={**values(), "source_agent_id": "office-agent"},
        ),
    )
    with pytest.raises(RegistryError, match="RISK_REDUCTION"):
        registry.inventory.command(
            ADMIN,
            cmd(
                "MergeAsset",
                asset.revision,
                target_asset_id=target.id,
                target_revision=target.revision,
                reason="Cannot bury core data",
            ),
        )


def test_expiry_and_removed_issuer_are_visible_without_mutating_history(registry):
    asset = accept(registry, save(registry))
    assert asset.evidence_status == "current"
    registry.clock = lambda: 2000
    expired = registry.inventory.get(ADMIN, asset.id)
    assert expired.actual_level == "L0" and expired.evidence_status == "expired"
    assert expired.revision == asset.revision
    registry.inventory.attestors.clear()
    assert registry.inventory.list(ADMIN).items[0].evidence_status == "untrusted"
    assert len(registry.inventory.history(ADMIN, asset.id).events) == asset.revision


def test_external_observation_blocks_previously_accepted_asset(registry):
    asset = accept(registry, bind(registry, save(registry)))
    obs = {
        "source": "platform",
        "source_id": "source-1",
        "record_id": "record-1",
        "snapshot_digest": SPEC,
        "observed_at": 1000,
        "external_domain": True,
    }
    observed = registry.inventory.command(
        ADMIN, cmd("ObserveAsset", asset.revision, metadata=values(), observation=obs)
    )
    assert observed.actual_level == "L0" and observed.evidence_status == "blocked"
    with pytest.raises(RegistryError, match="MISMATCH"):
        registry.inventory.admission(MEMBER, REF, SPEC)


def test_discovery_claim_is_atomic_owned_audited_and_does_not_publish(registry):
    inventory = registry.inventory
    discovered = inventory.command(
        ADMIN,
        cmd(
            "ObserveAsset",
            metadata={**values(), "owner_user_id": None, "department_id": None},
            observation={
                "source": "platform",
                "source_id": "dify-console",
                "record_id": "app-1",
                "snapshot_digest": SPEC,
                "observed_at": 1000,
            },
        ),
    )
    assert discovered.status == "discovered"
    assert discovered.metadata.owner_user_id is None
    assert inventory.list(ADMIN, owner="").items[0].id == discovered.id
    with pytest.raises(RegistryError, match="AGENT_ASSET_OWNER_REQUIRED"):
        inventory.command(ADMIN, cmd("SubmitAsset", discovered.revision))
    with pytest.raises(ValueError, match="AGENT_ASSET_OWNER_REQUIRED"):
        cmd("SaveAsset", discovered.revision, metadata=discovered.metadata.model_dump())
    claim = cmd("ClaimAsset", discovered.revision, owner_user_id="alice", department_id="support")
    claimed = inventory.command(ADMIN, claim)
    assert claimed.status == "draft"
    assert claimed.actual_level == "L0"
    assert claimed.metadata.owner_user_id == "alice"
    assert inventory.command(ADMIN, claim).revision == claimed.revision
    assert inventory.list(ADMIN, owner="").items == []
    assert inventory.list(ADMIN, owner="alice").items[0].id == claimed.id
    with pytest.raises(RegistryError, match="AGENT_ASSET_REVISION_CONFLICT"):
        inventory.command(ADMIN, cmd("ClaimAsset", discovered.revision, owner_user_id="bob"))
    with pytest.raises(RegistryError, match="AGENT_ASSET_ALREADY_CLAIMED"):
        inventory.command(ADMIN, cmd("ClaimAsset", claimed.revision, owner_user_id="bob"))
    assert inventory.history(ADMIN, claimed.id).events[-1].operation == "ClaimAsset"


def test_discovery_refresh_replaces_only_same_source_and_retains_old_request_evidence(registry):
    created = save(registry)
    metadata = values()
    for i in range(70):
        created = registry.inventory.command(
            ADMIN,
            cmd(
                "ObserveAsset",
                created.revision,
                metadata=metadata,
                observation={
                    "source": "platform",
                    "source_id": "dify-console",
                    "record_id": "app-1",
                    "snapshot_digest": "sha256:" + format(i, "064x"),
                    "observed_at": 1000,
                },
            ),
        )
    assert len(created.observations) == 1
    assert created.observations[0].snapshot_digest.endswith("45")
    assert created.metadata.owner_user_id == "alice"
    assert created.status == "draft"
    assert len(registry.inventory.history(ADMIN, created.id).events) == 71
