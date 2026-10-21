"""Real Registry + IAM + ReBAC + database auth; failures injected only at fault boundaries."""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy import select, update

from nexus.bricks.delegation.service import DelegationService
from nexus.bricks.rebac.entity_registry import EntityRegistry
from nexus.bricks.rebac.manager import ReBACManager
from nexus.contracts.agent_execution import (
    CheckExecutionGrant,
    ExecutionPermission,
    ExecutionZonePolicy,
    IssueExecutionGrant,
)
from nexus.services.agents.agent_registration import AgentRegistrationService
from nexus.services.product_agent_registry.execution import AgentExecution, grants
from nexus.services.product_agent_registry.store import (
    ProductAgentRegistry,
    RegistryActor,
    RegistryError,
    initialize_schema,
)
from nexus.storage.models import APIKeyModel, ZoneModel
from nexus.storage.record_store import SQLAlchemyRecordStore
from tests.unit.services.test_agent_inventory import SPEC, cmd, values
from tests.unit.services.test_product_agent_registry import ADMIN, command, publish, register

MODEL = ExecutionPermission(resource="model:lab", operation="model.invoke")
TOOL = ExecutionPermission(resource="tool:lab", operation="tool.invoke")


@pytest.fixture
def execution(tmp_path):
    store = SQLAlchemyRecordStore(db_path=tmp_path / "iam.db")
    with store.session_factory() as session:
        session.add_all(
            [ZoneModel(zone_id="zone-a", name="Lab A"), ZoneModel(zone_id="zone-b", name="Lab B")]
        )
        session.commit()
    entities = EntityRegistry(store)
    rebac = ReBACManager(store.engine, enable_tiger_cache=False, enable_leopard=False)
    registration = AgentRegistrationService(store, entities, rebac_manager=rebac)
    asyncio.run(registration.register("coordinator", "Coordinator", "it", "zone-a", ipc=False))
    delegation = DelegationService(store, rebac, entities)
    initialize_schema(store.engine)
    registry = ProductAgentRegistry(
        store.engine,
        Ed25519PrivateKey.generate(),
        {"agents.example": "zone-a", "other.example": "zone-b"},
    )
    policy = ExecutionZonePolicy(
        coordinator_id="coordinator",
        coordinator_owner_id="it",
        issuers=[ADMIN.subject],
        evaluators=[ADMIN.subject],
        gateways=[ADMIN.subject],
        permissions=[MODEL, TOOL],
        evaluation_permissions=[MODEL, TOOL],
    )
    service = AgentExecution(registry, store, delegation, entities, {"zone-a": policy})
    asset = registry.inventory.command(
        ADMIN,
        cmd(
            "SaveAsset",
            metadata={**values(), "model_paths": [MODEL.resource], "tool_refs": [TOOL.resource]},
        ),
    )
    asset = registry.inventory.command(ADMIN, cmd("SubmitAsset", asset.revision))
    registry.inventory.command(
        ADMIN,
        cmd("ReviewAsset", asset.revision, decision="approve", reason="Synthetic evaluation only"),
    )
    yield service
    store.engine.dispose()


def request(execution, **patch):
    asset = execution.registry.inventory.get(ADMIN, "asset-a")
    return IssueExecutionGrant(
        **{
            "request_id": uuid4().hex,
            "asset_id": asset.id,
            "metadata_digest": asset.metadata_digest,
            "spec_digest": SPEC,
            "task_id": "task-a",
            "initiated_by": "alice",
            "mode": "evaluation",
            "dataset_digest": SPEC,
            "permissions": [MODEL, TOOL],
            "ttl_seconds": 120,
            **patch,
        }
    )


def check(result, **patch):
    return CheckExecutionGrant(
        **{
            "grant_id": result.grant.id,
            "task_id": result.grant.request.task_id,
            "spec_digest": SPEC,
            "permission": MODEL,
            "token": result.api_key,
            **patch,
        }
    )


@pytest.mark.asyncio
async def test_real_iam_bound_to_task_one_time_key_and_restart(execution):
    req = request(execution)
    result = await execution.issue(ADMIN, req)
    decision = await execution.authorize(ADMIN, check(result))
    assert decision.grant.status == "active" and decision.cacheable is False
    assert decision.grant.owner_user_id == "alice" and decision.grant.department_id == "support"
    assert result.api_key not in repr(result)
    assert result.api_key not in repr(check(result))
    assert (await execution.issue(ADMIN, req)).api_key is None
    with execution.engine.connect() as con:
        assert result.api_key not in con.execute(select(grants.c.document)).scalar_one()
    restarted = AgentExecution(
        execution.registry,
        execution.auth._record_store,
        execution.delegation,
        execution.entities,
        execution.policies,
    )
    assert (await restarted.authorize(ADMIN, check(result))).grant.id == result.grant.id
    assert execution.delegation._enumerate_parent_grants(result.grant.worker_id, "zone-a") == []
    assert restarted.revoke(ADMIN, result.grant.id).cleanup_pending is False
    assert not (await restarted.auth.authenticate(result.api_key)).authenticated
    with pytest.raises(RegistryError, match="INACTIVE"):
        await restarted.authorize(ADMIN, check(result))
    assert restarted.revoke(ADMIN, result.grant.id).status == "revoked"


@pytest.mark.asyncio
async def test_scope_cross_task_token_and_organization_fail_before_dispatch(execution):
    result = await execution.issue(ADMIN, request(execution))
    other = await execution.issue(ADMIN, request(execution, task_id="task-b"))
    for patch in [
        {"task_id": "task-b"},
        {"spec_digest": "sha256:" + "b" * 64},
        {"permission": ExecutionPermission(resource=MODEL.resource, operation="tool.invoke")},
        {"permission": ExecutionPermission(resource="model:other", operation="model.invoke")},
        {"token": other.api_key},
    ]:
        with pytest.raises(RegistryError):
            await execution.authorize(ADMIN, check(result, **patch))
    for actor in [
        RegistryActor(ADMIN.subject, "zone-b", True),
        RegistryActor(ADMIN.subject, "zone-a", False),
        RegistryActor("user:untrusted", "zone-a", True),
    ]:
        with pytest.raises(RegistryError, match="FORBIDDEN"):
            await execution.authorize(actor, check(result))


@pytest.mark.asyncio
async def test_asset_suspend_policy_removal_and_metadata_change_invalidate_live_key(execution):
    result = await execution.issue(ADMIN, request(execution))
    policy = execution.policies["zone-a"]
    execution.policies["zone-a"] = policy.model_copy(update={"evaluation_permissions": []})
    with pytest.raises(RegistryError, match="SCOPE_DENIED"):
        await execution.authorize(ADMIN, check(result))
    execution.policies["zone-a"] = policy
    asset = execution.registry.inventory.get(ADMIN, "asset-a")
    execution.registry.inventory.command(
        ADMIN, cmd("SuspendAsset", asset.revision, reason="IT stopped this agent")
    )
    assert (await execution.auth.authenticate(result.api_key)).authenticated
    with pytest.raises(RegistryError, match="ASSET_NOT_ADMITTED"):
        await execution.authorize(ADMIN, check(result))


@pytest.mark.asyncio
async def test_key_and_delegation_expiry_are_independently_checked(execution):
    result = await execution.issue(ADMIN, request(execution))
    with execution.auth.session_factory() as session:
        session.execute(
            update(APIKeyModel)
            .where(APIKeyModel.key_id == result.grant.key_id)
            .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
        session.commit()
    with pytest.raises(RegistryError, match="TOKEN_INVALID"):
        await execution.authorize(ADMIN, check(result))
    result2 = await execution.issue(ADMIN, request(execution))
    execution.registry.clock = lambda: result2.grant.expires_at
    with pytest.raises(RegistryError, match="INACTIVE"):
        await execution.authorize(ADMIN, check(result2))


@pytest.mark.asyncio
async def test_L0_cannot_run_and_unreviewed_cannot_evaluate(execution):
    with pytest.raises(RegistryError, match="PRINCIPAL_REQUIRED"):
        await execution.issue(
            ADMIN, request(execution, mode="run", version="v1", dataset_digest=None)
        )
    register(execution.registry)
    asset = execution.registry.inventory.get(ADMIN, "asset-a")
    from tests.unit.services.test_product_agent_registry import REF

    asset = execution.registry.inventory.command(
        ADMIN, cmd("LinkAsset", asset.revision, principal=REF.model_dump())
    )
    with pytest.raises(RegistryError):
        await execution.issue(
            ADMIN, request(execution, mode="run", version="v1", dataset_digest=None)
        )
    execution.registry.inventory.command(
        ADMIN,
        cmd(
            "SaveAsset",
            asset.revision,
            metadata={**asset.metadata.model_dump(), "purpose": "Changed"},
        ),
    )
    with pytest.raises(RegistryError, match="ASSET_NOT_ADMITTED"):
        await execution.issue(ADMIN, request(execution))


@pytest.mark.asyncio
async def test_production_version_revocation_is_checked(execution):
    from tests.unit.services.test_agent_inventory import bind, report

    registry = execution.registry
    asset = bind(registry, registry.inventory.get(ADMIN, "asset-a"))
    registry.inventory.attestors.add(ADMIN.subject)
    asset = registry.inventory.command(
        ADMIN,
        cmd(
            "RecordAcceptance",
            asset.revision,
            report={
                **report(asset),
                "expires_at": registry.clock() + 60000,
                "checks": ["entry", "identity", "router", "revoke", "tool"],
            },
        ),
    )
    registry.inventory.command(
        ADMIN, cmd("ReviewAsset", asset.revision, decision="approve", reason="Verified lab report")
    )
    registry.command(ADMIN, publish())
    registry.command(
        ADMIN,
        command(
            "DecideVersion",
            version="v1",
            decision="approved",
            reason="Test acceptance",
            expires_at=registry.clock() + 60000,
        ),
    )
    result = await execution.issue(
        ADMIN, request(execution, mode="run", version="v1", dataset_digest=None)
    )
    assert (await execution.authorize(ADMIN, check(result))).grant.request.mode == "run"
    registry.command(
        ADMIN, command("DecideVersion", version="v1", decision="revoked", reason="Withdraw version")
    )
    with pytest.raises(RegistryError, match="VERSION_REVOKED"):
        await execution.authorize(ADMIN, check(result))


@pytest.mark.asyncio
async def test_revoke_cleanup_failure_is_denied_and_retryable(execution, monkeypatch):
    result = await execution.issue(ADMIN, request(execution))
    original = execution.delegation._delete_worker_tuples
    monkeypatch.setattr(
        execution.delegation,
        "_delete_worker_tuples",
        lambda *args: (_ for _ in ()).throw(RuntimeError("injected outage")),
    )
    revoked = execution.revoke(ADMIN, result.grant.id)
    assert revoked.status == "revoked" and revoked.cleanup_pending
    with pytest.raises(RegistryError, match="INACTIVE"):
        await execution.authorize(ADMIN, check(result))
    monkeypatch.setattr(execution.delegation, "_delete_worker_tuples", original)
    assert not execution.revoke(ADMIN, result.grant.id).cleanup_pending
    assert not (await execution.auth.authenticate(result.api_key)).authenticated


@pytest.mark.asyncio
async def test_concurrent_replay_has_single_worker_and_no_recoverable_secret(execution):
    req = request(execution)
    results = await asyncio.gather(*(execution.issue(ADMIN, req) for _ in range(6)))
    assert len({r.grant.id for r in results}) == 1
    assert sum(bool(r.api_key) for r in results) == 1
    assert execution.delegation.list_delegations()[1] == 1
    with pytest.raises(RegistryError, match="REQUEST_CONFLICT"):
        await execution.issue(ADMIN, req.model_copy(update={"task_id": "different"}))


@pytest.mark.asyncio
async def test_crash_before_activation_recovers_and_revokes_iam(execution, monkeypatch):
    monkeypatch.setattr(
        execution, "_activate", lambda *args: (_ for _ in ()).throw(RuntimeError("crash boundary"))
    )
    req = request(execution)
    with pytest.raises(RegistryError, match="ISSUANCE_FAILED"):
        await execution.issue(ADMIN, req)
    result = await execution.issue(ADMIN, req)
    assert result.grant.status == "failed" and not result.grant.cleanup_pending
    assert result.api_key is None
    with execution.auth.session_factory() as session:
        worker_keys = session.scalars(
            select(APIKeyModel).where(APIKeyModel.subject_id == result.grant.worker_id)
        ).all()
        assert worker_keys and all(k.revoked for k in worker_keys)


@pytest.mark.asyncio
async def test_revoke_during_issuance_does_not_reactivate(execution, monkeypatch):
    original = execution._delegate

    def race(actor, grant):
        state = execution.revoke(actor, grant.id)
        assert state.cleanup_pending
        return original(actor, grant)

    monkeypatch.setattr(execution, "_delegate", race)
    req = request(execution)
    with pytest.raises(RegistryError, match="ISSUANCE_FAILED"):
        await execution.issue(ADMIN, req)
    result = await execution.issue(ADMIN, req)
    assert result.grant.status == "revoked" and not result.grant.cleanup_pending


def test_request_contract_rejects_scope_or_selector_ambiguity(execution):
    for patch in [
        {"ttl_seconds": 301},
        {"ttl_seconds": True},
        {"permissions": []},
        {"permissions": [MODEL, MODEL]},
        {"mode": "run"},
        {"version": "v1"},
        {"dataset_digest": None},
    ]:
        with pytest.raises(ValueError):
            request(execution, **patch)
    with pytest.raises(ValueError):
        ExecutionPermission(operation="model.invoke", resource="model:*")


@pytest.mark.asyncio
async def test_revocation_between_iam_record_and_key_creation_cleans_late_key(
    execution, monkeypatch
):
    original = execution.delegation._create_worker_api_key

    def race(**kwargs):
        page = execution.list(ADMIN, "asset-a")
        assert len(page.items) == 1
        state = execution.revoke(ADMIN, page.items[0].id)
        assert not state.cleanup_pending
        return original(**kwargs)

    monkeypatch.setattr(execution.delegation, "_create_worker_api_key", race)
    req = request(execution)
    with pytest.raises(RegistryError, match="ISSUANCE_FAILED"):
        await execution.issue(ADMIN, req)
    result = await execution.issue(ADMIN, req)
    assert result.grant.status == "revoked" and not result.grant.cleanup_pending
    with execution.auth.session_factory() as session:
        keys = session.scalars(
            select(APIKeyModel).where(APIKeyModel.subject_id == result.grant.worker_id)
        ).all()
        assert keys and all(k.revoked for k in keys)


@pytest.mark.asyncio
async def test_lost_response_can_list_and_revoke_pending_after_restart(execution):
    req = request(execution)
    reserved, created = execution._reserve(ADMIN, req)
    assert created
    issued = execution._delegate(ADMIN, reserved)
    restarted = AgentExecution(
        execution.registry,
        execution.auth._record_store,
        execution.delegation,
        execution.entities,
        execution.policies,
    )
    page = restarted.list(ADMIN, "asset-a")
    assert len(page.items) == 1 and page.items[0].status == "issuing"
    assert issued.api_key not in page.model_dump_json()
    assert not restarted.revoke(ADMIN, reserved.id).cleanup_pending
    assert not (await restarted.auth.authenticate(issued.api_key)).authenticated


@pytest.mark.asyncio
async def test_iam_zone_junction_and_delegation_status_are_authoritative(execution):
    from sqlalchemy import delete

    from nexus.bricks.delegation.models import DelegationOutcome
    from nexus.storage.models import APIKeyZoneModel

    first = await execution.issue(ADMIN, request(execution))
    execution.delegation.complete_delegation(first.grant.delegation_id, DelegationOutcome.COMPLETED)
    assert (await execution.auth.authenticate(first.api_key)).authenticated
    with pytest.raises(RegistryError, match="DELEGATION_INACTIVE"):
        await execution.authorize(ADMIN, check(first))
    assert not execution.revoke(ADMIN, first.grant.id).cleanup_pending
    assert not (await execution.auth.authenticate(first.api_key)).authenticated
    second = await execution.issue(ADMIN, request(execution))
    with execution.auth.session_factory() as session:
        session.execute(
            delete(APIKeyZoneModel).where(APIKeyZoneModel.key_id == second.grant.key_id)
        )
        session.commit()
    with pytest.raises(RegistryError, match="TOKEN_INVALID"):
        await execution.authorize(ADMIN, check(second))


@pytest.mark.asyncio
async def test_model_call_limit_cannot_exceed_current_zone_policy(execution):
    with pytest.raises(RegistryError, match="CALL_LIMIT_DENIED"):
        await execution.issue(ADMIN, request(execution, max_model_calls=9))
    result = await execution.issue(ADMIN, request(execution, max_model_calls=2))
    execution.policies["zone-a"] = execution.policies["zone-a"].model_copy(
        update={"max_model_calls": 1}
    )
    with pytest.raises(RegistryError, match="CALL_LIMIT_DENIED"):
        await execution.authorize(ADMIN, check(result))
