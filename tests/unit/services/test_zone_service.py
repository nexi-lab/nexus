"""2C service-layer tests: saga semantics, idempotency, the §11.2 truth
table, epoch fail-closed and worker fencing — on SQLite with fake runtimes."""

from __future__ import annotations

import pytest
import sqlalchemy as sa

from nexus.contracts.zone_v1 import ZoneCreateRequest, ZoneGrantCreateRequest
from nexus.remote.zone_runtime_client import RuntimeReceipt, ZoneRuntimeUnavailable
from nexus.services.zones.authz import AuthorizationService, Principal
from nexus.services.zones.service import ServiceError, ZoneApplicationService
from nexus.services.zones.worker import ZoneOperationWorker

PRINCIPAL = {"subject_type": "user", "subject_id": "usr-admin"}
GRANTEE = {"subject_type": "organization", "subject_id": "org-1"}


class FakeRuntime:
    def __init__(self, *, ok: bool = True) -> None:
        self.ok = ok
        self.calls: list[str] = []

    def create_zone(self, *, zone_id: str, ctx) -> RuntimeReceipt:
        self.calls.append("create")
        if self.ok:
            return RuntimeReceipt(
                ok=True,
                physical_identity=f"phys-{zone_id}",
                membership="member",
                runtime_revision="r1",
                raw={
                    "ok": True,
                    "physical_identity": f"phys-{zone_id}",
                    "membership": "member",
                    "runtime_revision": "r1",
                },
            )
        return RuntimeReceipt(ok=False, error="refused")

    def zone_status(self, *, zone_id: str, ctx) -> RuntimeReceipt:
        self.calls.append("status")
        if self.ok:
            return RuntimeReceipt(
                ok=True,
                physical_identity=f"phys-{zone_id}",
                membership="member",
                runtime_revision="r1",
                raw={"zone_id": zone_id, "presence": "RESIDENT"},
            )
        return RuntimeReceipt(ok=False, error="not resident")

    def join_zone(self, *, zone_id: str, peers: list[str], ctx) -> RuntimeReceipt:
        self.calls.append("join")
        return RuntimeReceipt(
            ok=self.ok,
            physical_identity=f"phys-{zone_id}" if self.ok else None,
            runtime_revision="r2" if self.ok else None,
            raw={"zone_id": zone_id, "outcome": "JOINED"} if self.ok else {},
        )

    def mount(self, *, parent_zone_id: str, target_zone_id: str, path: str, ctx) -> RuntimeReceipt:
        self.calls.append("mount")
        return RuntimeReceipt(ok=self.ok, runtime_revision="r3", raw={"outcome": "MOUNTED"})

    def unmount(self, *, mount_ref: str, ctx) -> RuntimeReceipt:
        self.calls.append("unmount")
        return RuntimeReceipt(ok=self.ok, runtime_revision="r4", raw={"outcome": "UNMOUNTED"})

    def remove_replica(self, *, zone_id: str, force: bool, ctx) -> RuntimeReceipt:
        self.calls.append("remove_replica")
        return RuntimeReceipt(ok=self.ok, raw={"outcome": "REPLICA_REMOVED"})

    def deprovision(self, *, zone_id: str, deletion_epoch: int, ctx) -> RuntimeReceipt:
        self.calls.append("deprovision")
        return RuntimeReceipt(ok=True, raw={"zone_id": zone_id, "outcome": "DEPROVISIONED"})

    def probe_capabilities(self, *, ctx) -> tuple[str, ...]:
        return ("zone-runtime",)


class UnavailableRuntime(FakeRuntime):
    def create_zone(self, *, zone_id: str, ctx) -> RuntimeReceipt:
        raise ZoneRuntimeUnavailable("timed out")


class JournalRecoveredRuntime(UnavailableRuntime):
    def get_operation(self, *, operation_id: str, ctx) -> RuntimeReceipt:
        self.calls.append("get_operation")
        return RuntimeReceipt(
            ok=True,
            physical_identity="phys-team-test-zone",
            membership="member",
            runtime_revision="r1",
            raw={"operation_id": operation_id, "outcome": "CREATED"},
        )


@pytest.fixture()
def session_factory():
    engine = sa.create_engine("sqlite://", future=True)

    @sa.event.listens_for(engine, "connect")
    def _fk_on(dbapi_conn, _rec):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    # Only the zone-v1 surface (plus its FK targets) — this suite is unit-level.
    from nexus.storage.models import auth as auth_models
    from nexus.storage.models import zone_v1 as zv1

    auth_models.ZoneModel.__table__.create(engine)
    for t in (
        zv1.ZoneGrantModel,
        zv1.ZoneOperationModel,
        zv1.ZoneMountModel,
        zv1.ZoneRuntimeOutboxModel,
        zv1.ZoneGrantProjectionOutboxModel,
        zv1.ZoneAuthorizationEpochModel,
        zv1.ZoneDelegationModel,
        zv1.RebacRelationSourceModel,
    ):
        t.__table__.create(engine)
    return sessionmaker_for(engine)


def sessionmaker_for(engine):
    from sqlalchemy.orm import sessionmaker

    return sessionmaker(bind=engine, future=True, expire_on_commit=False)


def make_service(
    session_factory, runtime: FakeRuntime, *, worker: bool = True
) -> ZoneApplicationService:
    return ZoneApplicationService(session_factory, runtime, worker_enabled=worker)


def create_request(zone_id="team-test-zone") -> ZoneCreateRequest:
    return ZoneCreateRequest(
        api_version="auth.sudo.dev/v1",
        kind="ZoneCreateRequest",
        zone_id=zone_id,
        display_name="Test Zone",
    )


def grant_request() -> ZoneGrantCreateRequest:
    return ZoneGrantCreateRequest(
        api_version="auth.sudo.dev/v1",
        kind="ZoneGrantCreateRequest",
        grantee=GRANTEE,
        capabilities=["zone.data.read"],
        reason="onboarding default",
    )


# ── create saga ───────────────────────────────────────────────────────────────


def test_create_saga_success_marks_active_only_with_receipt(session_factory):
    runtime = FakeRuntime(ok=True)
    svc = make_service(session_factory, runtime)
    result = svc.create_zone(create_request(), idempotency_key="k1", principal=PRINCIPAL)
    assert result.state == "succeeded" and result.step == "mark-active-with-receipt"
    with session_factory() as s:
        from nexus.storage.models import ZoneGrantModel
        from nexus.storage.models.auth import ZoneModel

        zone = s.get(ZoneModel, "team-test-zone")
        assert zone.canonical_status == "active"
        assert zone.runtime_observed_receipt["physical_identity"] == "phys-team-test-zone"
        grants = s.execute(sa.select(ZoneGrantModel)).scalars().all()
        assert len(grants) == 1 and grants[0].status == "active"


def test_create_runtime_refusal_never_marks_active(session_factory):
    runtime = FakeRuntime(ok=False)
    svc = make_service(session_factory, runtime)
    result = svc.create_zone(create_request(), idempotency_key="k1", principal=PRINCIPAL)
    assert result.state == "failed" and result.retryable is True
    with session_factory() as s:
        from nexus.storage.models.auth import ZoneModel

        zone = s.get(ZoneModel, "team-test-zone")
        assert zone.canonical_status is None


def test_create_timeout_is_unknown_not_failed(session_factory):
    svc = make_service(session_factory, UnavailableRuntime())
    result = svc.create_zone(create_request(), idempotency_key="k1", principal=PRINCIPAL)
    assert result.state == "running" and result.step == "runtime-unknown" and result.retryable


def test_create_recovers_lost_response_from_runtime_journal(session_factory):
    svc = make_service(session_factory, JournalRecoveredRuntime())
    result = svc.create_zone(create_request(), idempotency_key="k1", principal=PRINCIPAL)
    assert result.state == "succeeded"


def test_idempotency_replays_same_request_and_conflicts_on_different(session_factory):
    svc = make_service(session_factory, FakeRuntime())
    first = svc.create_zone(create_request(), idempotency_key="k1", principal=PRINCIPAL)
    replay = svc.create_zone(create_request(), idempotency_key="k1", principal=PRINCIPAL)
    assert replay.operation_id == first.operation_id  # same operation, no rebuild
    # §5.7: the key is scoped to principal+action+target — a different target
    # is a different scope, not a conflict. Same scope + different body IS.
    changed = create_request().model_copy(update={"display_name": "Different Name"})
    with pytest.raises(ServiceError) as exc:
        svc.create_zone(changed, idempotency_key="k1", principal=PRINCIPAL)
    assert exc.value.code == "IDEMPOTENCY_CONFLICT"


# ── grants ────────────────────────────────────────────────────────────────────


def _active_zone(svc, session_factory, zone_id="team-test-zone"):
    svc.create_zone(create_request(zone_id), idempotency_key="k1", principal=PRINCIPAL)


def test_grant_stays_pending_until_projection_completes(session_factory):
    svc = make_service(session_factory, FakeRuntime())
    _active_zone(svc, session_factory)
    result = svc.issue_grant(
        "team-test-zone", grant_request(), idempotency_key="gk1", principal=PRINCIPAL
    )
    assert result.state == "queued"
    with session_factory() as s:
        from nexus.storage.models import (
            RebacRelationSourceModel,
            ZoneAuthorizationEpochModel,
            ZoneGrantModel,
        )

        grant = s.execute(
            sa.select(ZoneGrantModel).where(ZoneGrantModel.status == "pending")
        ).scalar_one()
        assert grant.status == "pending"  # §5.3: pending grants no access
        # Genesis projection advances the first epoch during create.
        assert s.get(ZoneAuthorizationEpochModel, "team-test-zone").epoch == 1
        assert svc.complete_grant_projection(grant_id=grant.grant_id)
        s.expire_all()
        grant2 = s.get(ZoneGrantModel, grant.grant_id)
        assert grant2.status == "active"
        assert s.get(ZoneAuthorizationEpochModel, "team-test-zone").epoch == 2
        edges = (
            s.execute(
                sa.select(RebacRelationSourceModel).where(
                    RebacRelationSourceModel.source_grant_id == grant.grant_id
                )
            )
            .scalars()
            .all()
        )
        assert {(edge.relation, edge.object) for edge in edges} == {("direct_viewer", "/")}


def test_failed_mandatory_projection_never_activates_grant(session_factory):
    def fail_projection(zone_id, principal, relation, path):
        raise RuntimeError("rebac unavailable")

    svc = ZoneApplicationService(
        session_factory,
        FakeRuntime(),
        worker_enabled=True,
        projection_write=fail_projection,
    )
    _active_zone(svc, session_factory)
    # Creation itself remains non-active when its mandatory owner projection fails.
    with session_factory() as session:
        from nexus.storage.models import ZoneGrantModel
        from nexus.storage.models.auth import ZoneModel

        zone = session.get(ZoneModel, "team-test-zone")
        grant = session.execute(sa.select(ZoneGrantModel)).scalar_one()
        assert zone.canonical_status is None
        assert grant.status == "pending"


def test_revoke_commits_fact_epoch_and_invalidation_together(session_factory):
    svc = make_service(session_factory, FakeRuntime())
    _active_zone(svc, session_factory)
    svc.issue_grant("team-test-zone", grant_request(), idempotency_key="gk1", principal=PRINCIPAL)
    with session_factory() as s:
        from nexus.storage.models import ZoneGrantModel

        grant = s.execute(
            sa.select(ZoneGrantModel).where(ZoneGrantModel.source_id == "gk1")
        ).scalar_one()
        svc.complete_grant_projection(grant_id=grant.grant_id)
        gid = grant.grant_id
    result = svc.revoke_grant("team-test-zone", gid, principal=PRINCIPAL, reason="done")
    assert result.state == "succeeded" and result.step == "revoked-epoch-committed"
    with session_factory() as s:
        from nexus.storage.models import (
            ZoneAuthorizationEpochModel,
            ZoneGrantModel,
            ZoneGrantProjectionOutboxModel,
        )

        assert s.get(ZoneGrantModel, gid).status == "revoked"
        assert s.get(ZoneAuthorizationEpochModel, "team-test-zone").epoch == 3
        events = s.execute(sa.select(ZoneGrantProjectionOutboxModel)).scalars().all()
        assert any(e.event_type == "grant.cleanup_projections" for e in events)


def test_grant_requires_active_zone(session_factory):
    svc = make_service(session_factory, FakeRuntime(ok=False))
    svc.create_zone(create_request(), idempotency_key="k1", principal=PRINCIPAL)
    with pytest.raises(ServiceError) as exc:
        svc.issue_grant(
            "team-test-zone", grant_request(), idempotency_key="gk1", principal=PRINCIPAL
        )
    assert exc.value.code == "ZONE_NOT_ACTIVE"


# ── lifecycle / deprovision / patch ──────────────────────────────────────────


def test_deprovision_blocked_by_active_grants(session_factory):
    svc = make_service(session_factory, FakeRuntime())
    _active_zone(svc, session_factory)
    svc.issue_grant("team-test-zone", grant_request(), idempotency_key="gk1", principal=PRINCIPAL)
    with session_factory() as s:
        from nexus.storage.models import ZoneGrantModel

        g = s.execute(
            sa.select(ZoneGrantModel).where(ZoneGrantModel.source_id == "gk1")
        ).scalar_one()
        svc.complete_grant_projection(grant_id=g.grant_id)  # blocker needs an ACTIVE external grant
    with pytest.raises(ServiceError) as exc:
        svc.request_deprovision("team-test-zone", principal=PRINCIPAL)
    assert exc.value.code == "ZONE_DELETE_BLOCKED"


def test_deprovision_moves_to_deleting_without_grants(session_factory):
    runtime = FakeRuntime()
    svc = make_service(session_factory, runtime)
    _active_zone(svc, session_factory)
    result = svc.request_deprovision("team-test-zone", principal=PRINCIPAL)
    assert result.state == "queued"
    with session_factory() as s:
        from nexus.storage.models.auth import ZoneModel

        assert s.get(ZoneModel, "team-test-zone").canonical_status == "deleting"
    worker = ZoneOperationWorker(session_factory, runtime, svc)
    assert worker.pump_once() >= 1
    with session_factory() as s:
        from nexus.storage.models.auth import ZoneModel

        zone = s.get(ZoneModel, "team-test-zone")
        assert zone.canonical_status == "deleted"
        assert zone.phase == "Terminated"


def test_patch_if_match_conflict(session_factory):
    svc = make_service(session_factory, FakeRuntime())
    _active_zone(svc, session_factory)
    from nexus.contracts.zone_v1 import ZonePatchRequest

    patch = ZonePatchRequest(
        api_version="auth.sudo.dev/v1", kind="ZonePatchRequest", display_name="New"
    )
    with pytest.raises(ServiceError) as exc:
        svc.patch_zone("team-test-zone", patch, revision_if_match="stale-rev")
    assert exc.value.code == "ZONE_REVISION_CONFLICT"
    new_rev = svc.patch_zone("team-test-zone", patch, revision_if_match=None)
    assert new_rev


# ── authorization truth table (§11.2) ─────────────────────────────────────────


def _authz_env(session_factory, *, grant: bool, rebac: bool, expired: bool = False):
    svc = make_service(session_factory, FakeRuntime())
    _active_zone(svc, session_factory)
    if grant:
        req = grant_request()
        if expired:
            req = req.model_copy(update={"expires_at": "2020-01-01T00:00:00Z"})
        svc.issue_grant("team-test-zone", req, idempotency_key="gk1", principal=PRINCIPAL)
        with session_factory() as s:
            from nexus.storage.models import ZoneGrantModel

            g = s.execute(
                sa.select(ZoneGrantModel).where(ZoneGrantModel.source_id == "gk1")
            ).scalar_one()
            svc.complete_grant_projection(grant_id=g.grant_id)
            s.expire_all()
            g2 = s.get(ZoneGrantModel, g.grant_id)
            g2.grantee = GRANTEE  # cover the org principal below
            s.commit()
    checker = (
        (lambda sess, sub, rel, obj, zone_id: rebac)
        if rebac
        else (lambda sess, sub, rel, obj, zone_id: False)
    )
    return AuthorizationService(
        session_factory,
        checker,
        membership_check=lambda user_id, org_id, version: True,
        trusted_issuers=frozenset({"moss-provisioner"}),
    )


def test_truth_table_allow_when_both_layers_hold(session_factory):
    principal = Principal(subject_type="organization", subject_id="org-1")
    authz = _authz_env(session_factory, grant=True, rebac=True)
    with session_factory() as s:
        assert authz.allow(
            s,
            principal=principal,
            zone_id="team-test-zone",
            capability="zone.data.read",
            resource_path="/sessions/x",
        )


def test_truth_table_denies_when_relation_missing(session_factory):
    principal = Principal(subject_type="organization", subject_id="org-1")
    authz = _authz_env(session_factory, grant=True, rebac=False)
    with session_factory() as s:
        decision = authz.allow(
            s,
            principal=principal,
            zone_id="team-test-zone",
            capability="zone.data.read",
            resource_path="/sessions/x",
        )
        assert not decision and decision.code == "RESOURCE_RELATION_DENIED"


def test_truth_table_denies_when_grant_missing(session_factory):
    principal = Principal(subject_type="organization", subject_id="org-1")
    authz = _authz_env(session_factory, grant=False, rebac=True)
    with session_factory() as s:
        decision = authz.allow(
            s,
            principal=principal,
            zone_id="team-test-zone",
            capability="zone.data.read",
            resource_path="/sessions/x",
        )
        assert not decision and decision.code == "GRANT_NOT_ACTIVE"


def test_expired_grant_denies_at_access_time(session_factory):
    principal = Principal(subject_type="organization", subject_id="org-1")
    authz = _authz_env(session_factory, grant=True, rebac=True, expired=True)
    with session_factory() as s:
        decision = authz.allow(
            s,
            principal=principal,
            zone_id="team-test-zone",
            capability="zone.data.read",
            resource_path="/x",
        )
        assert not decision


def test_delegation_denies_when_epoch_moves(session_factory):
    authz = _authz_env(session_factory, grant=True, rebac=True)
    with session_factory() as s, s.begin():
        d = authz.issue_delegation(
            s,
            principal=Principal(subject_type="user", subject_id="u1"),
            issuer=Principal(subject_type="service", subject_id="moss-provisioner"),
            org_id="org-1",
            membership_version="mv-1",
            zone_id="team-test-zone",
            audience="runtime",
        )
        dlg_id = d.delegation_id
    with session_factory() as s, s.begin():
        assert authz.verify_delegation(s, delegation_id=dlg_id, audience="runtime")
    # epoch advances (a revoke) → old delegation denied
    with session_factory() as s, s.begin():
        from nexus.storage.models import ZoneAuthorizationEpochModel

        epoch = s.get(ZoneAuthorizationEpochModel, "team-test-zone")
        epoch.epoch += 1
    with session_factory() as s:
        decision = authz.verify_delegation(s, delegation_id=dlg_id, audience="runtime")
        assert not decision and decision.code == "GRANT_REVOKED"


def test_delegation_denies_when_membership_is_removed(session_factory):
    active = True
    base = _authz_env(session_factory, grant=True, rebac=True)
    authz = AuthorizationService(
        session_factory,
        lambda session, subject, permission, path, zone_id: True,
        membership_check=lambda user_id, org_id, version: active,
        trusted_issuers=frozenset({"moss-provisioner"}),
    )
    del base
    with session_factory() as s, s.begin():
        d = authz.issue_delegation(
            s,
            principal=Principal(subject_type="user", subject_id="u1"),
            issuer=Principal(subject_type="service", subject_id="moss-provisioner"),
            org_id="org-1",
            membership_version="mv-1",
            zone_id="team-test-zone",
            audience="runtime",
        )
        delegation_id = d.delegation_id
    active = False
    with session_factory() as s:
        decision = authz.verify_delegation(s, delegation_id=delegation_id, audience="runtime")
        assert not decision and decision.code == "GRANT_REVOKED"


# ── worker fencing ────────────────────────────────────────────────────────────


def test_worker_pumps_grant_projection_outbox(session_factory):
    svc = make_service(session_factory, FakeRuntime())
    _active_zone(svc, session_factory)
    svc.issue_grant("team-test-zone", grant_request(), idempotency_key="gk1", principal=PRINCIPAL)
    worker = ZoneOperationWorker(session_factory, FakeRuntime(), svc)
    processed = worker.pump_once()
    assert processed >= 1
    with session_factory() as s:
        from nexus.storage.models import ZoneGrantModel

        grant = s.execute(
            sa.select(ZoneGrantModel).where(ZoneGrantModel.source_id == "gk1")
        ).scalar_one()
        assert grant.status == "active"


def test_worker_resumes_create_and_activates_only_after_projection(session_factory):
    runtime = FakeRuntime()
    svc = make_service(session_factory, runtime, worker=False)
    accepted = svc.create_zone(
        create_request(), idempotency_key="create-async", principal=PRINCIPAL
    )
    assert accepted.state == "queued"
    with session_factory() as session:
        from nexus.storage.models.auth import ZoneModel

        assert session.get(ZoneModel, "team-test-zone").canonical_status is None

    worker = ZoneOperationWorker(session_factory, runtime, svc)
    assert worker.pump_once() >= 2
    with session_factory() as session:
        from nexus.storage.models.auth import ZoneModel

        assert session.get(ZoneModel, "team-test-zone").canonical_status == "active"
    assert runtime.calls == ["create", "status"]


def test_projection_cleanup_preserves_an_overlapping_grant(session_factory):
    deleted: list[tuple[str, str]] = []
    svc = ZoneApplicationService(
        session_factory,
        FakeRuntime(),
        worker_enabled=True,
        projection_delete=lambda zone_id, principal, relation, path: deleted.append(
            (relation, path)
        ),
    )
    _active_zone(svc, session_factory)
    for key in ("gk1", "gk2"):
        svc.issue_grant("team-test-zone", grant_request(), idempotency_key=key, principal=PRINCIPAL)
    with session_factory() as s:
        from nexus.storage.models import ZoneGrantModel

        grants = (
            s.execute(sa.select(ZoneGrantModel).where(ZoneGrantModel.source_type == "manual"))
            .scalars()
            .all()
        )
    for grant in grants:
        assert svc.complete_grant_projection(grant_id=grant.grant_id)
    svc.revoke_grant("team-test-zone", grants[0].grant_id, principal=PRINCIPAL, reason="first")
    svc.cleanup_grant_projection(grant_id=grants[0].grant_id)
    assert deleted == []
    svc.revoke_grant("team-test-zone", grants[1].grant_id, principal=PRINCIPAL, reason="second")
    svc.cleanup_grant_projection(grant_id=grants[1].grant_id)
    assert deleted == [("direct_viewer", "/")]


def test_mount_observed_state_only_follows_runtime_receipt(session_factory):
    runtime = FakeRuntime()
    svc = make_service(session_factory, runtime)
    _active_zone(svc, session_factory, "team-parent")
    svc.create_zone(create_request("team-target"), idempotency_key="k2", principal=PRINCIPAL)
    result = svc.request_mount(
        parent_zone_id="team-parent",
        target_zone_id="team-target",
        path="/shared",
        idempotency_key="mount-1",
        principal=PRINCIPAL,
    )
    assert result.state == "succeeded"
    with session_factory() as s:
        from nexus.storage.models import ZoneMountModel

        mount = s.execute(sa.select(ZoneMountModel)).scalar_one()
        assert mount.desired_state == "mounted"
        assert mount.observed_state == "mounted"
