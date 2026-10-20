"""Strict contract tests for the canonical /v2 Zone API surface."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
import sqlalchemy as sa
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from nexus.remote.zone_runtime_client import RuntimeReceipt
from nexus.server.api.v2.routers.sessions import router as sessions_router
from nexus.server.api.v2.routers.zone_grants import router as grants_router
from nexus.server.api.v2.routers.zone_runtime import router as runtime_router
from nexus.server.api.v2.routers.zones import router as zones_router
from nexus.server.auth.zone_routes import router as legacy_zone_router
from nexus.server.dependencies import require_auth
from nexus.server.lifespan import zone_control as zone_control_module
from nexus.server.lifespan.zone_control import (
    ZoneControlNotArmed,
    arm_zone_services,
    startup_zone_control,
)


class Runtime:
    def create_zone(self, *, zone_id, ctx):
        return RuntimeReceipt(ok=True, physical_identity=zone_id, raw={"outcome": "CREATED"})

    def zone_status(self, *, zone_id, ctx):
        return RuntimeReceipt(
            ok=True,
            physical_identity=zone_id,
            membership="RESIDENT",
            runtime_revision="1:1:1",
            raw={"presence": "RESIDENT"},
        )

    def join_zone(self, *, zone_id, peers, ctx):
        return RuntimeReceipt(ok=True, physical_identity=zone_id, raw={"outcome": "JOINED"})

    def mount(self, *, parent_zone_id, target_zone_id, path, ctx):
        return RuntimeReceipt(ok=True, runtime_revision="1:2:2", raw={"outcome": "MOUNTED"})

    def unmount(self, *, ctx):
        return RuntimeReceipt(ok=True, runtime_revision="1:3:3", raw={"outcome": "UNMOUNTED"})

    def remove_replica(self, *, zone_id, force, ctx):
        return RuntimeReceipt(ok=True, raw={"outcome": "REPLICA_REMOVED"})

    def deprovision(self, *, zone_id, ctx):
        return RuntimeReceipt(ok=True, raw={"outcome": "DEPROVISIONED"})

    def probe_capabilities(self, *, ctx):
        return (
            "zone-runtime:create",
            "zone-runtime:join",
            "zone-runtime:status",
            "zone-runtime:mount",
            "zone-runtime:unmount",
            "zone-runtime:deprovision",
            "zone-runtime:operation-journal",
        )


def _app(with_sessions: bool = False) -> FastAPI:
    from nexus.storage.models import auth as auth_models
    from nexus.storage.models import session_v1 as session_models
    from nexus.storage.models import task_v1 as task_models
    from nexus.storage.models import zone_v1 as zone_models

    engine = sa.create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=sa.pool.StaticPool
    )
    auth_models.ZoneModel.__table__.create(engine)
    session_models.SessionModel.__table__.create(engine)
    if with_sessions:
        task_models.TaskSpecModel.__table__.create(engine)
        task_models.TaskResolutionModel.__table__.create(engine)
        task_models.TaskAttemptModel.__table__.create(engine)
        session_models.SessionZoneDependencyModel.__table__.create(engine)
        session_models.SessionDataRecordModel.__table__.create(engine)
    else:
        with engine.begin() as connection:
            connection.exec_driver_sql("CREATE TABLE task_attempts (attempt_id TEXT PRIMARY KEY)")
    session_models.SessionRuntimeRunModel.__table__.create(engine)
    for model in (
        zone_models.ZoneGrantModel,
        zone_models.ZoneOperationModel,
        zone_models.ZoneMountModel,
        zone_models.ZoneRuntimeOutboxModel,
        zone_models.ZoneGrantProjectionOutboxModel,
        zone_models.ZoneAuthorizationEpochModel,
        zone_models.ZoneDelegationModel,
        zone_models.RebacRelationSourceModel,
    ):
        model.__table__.create(engine)
    factory = sessionmaker(bind=engine, future=True, expire_on_commit=False)
    app = FastAPI()
    app.state.session_factory = factory
    app.include_router(zones_router)
    app.include_router(grants_router)
    app.include_router(runtime_router)
    app.include_router(legacy_zone_router)
    if with_sessions:
        app.include_router(sessions_router)
    arm_zone_services(
        app,
        session_factory=factory,
        runtime=Runtime(),
        rebac_check=lambda session, subject, permission, path, zone_id: True,
        projection_write=lambda zone_id, principal, relation, path: None,
        projection_delete=lambda zone_id, principal, relation, path: None,
        membership_check=lambda user_id, org_id, version: True,
        trusted_issuers=frozenset({"moss-provisioner"}),
    )
    app.dependency_overrides[require_auth] = lambda: {
        "authenticated": True,
        "is_admin": True,
        "subject_type": "service",
        "subject_id": "moss-provisioner",
    }
    return app


def test_openapi_fixture_matches_registered_zone_surface() -> None:
    app = _app()
    actual_schema = app.openapi()
    actual = {
        (method, path)
        for path, operations in actual_schema["paths"].items()
        if path.startswith("/v2/")
        for method in operations
        if method not in {"parameters"}
    }
    fixture = json.loads(
        (
            Path(__file__).resolve().parents[3] / "contracts/fixtures/openapi/zone-v2.openapi.json"
        ).read_text(encoding="utf-8")
    )
    expected = {
        (method, path) for path, operations in fixture["paths"].items() for method in operations
    }
    assert actual == expected

    # every fixture operation must declare its status codes (incl. the
    # contract error response) — an empty {} locks only the route set
    for fixture_path, operations in fixture["paths"].items():
        for method, op in operations.items():
            if method == "parameters":
                continue
            assert isinstance(op.get("responses"), dict) and op["responses"], (
                fixture_path,
                method,
            )
            assert "default" in op["responses"], (fixture_path, method)


def test_get_transfer_serializes_operation_datetimes_as_iso() -> None:
    """M-2: the service returns raw ORM datetimes; pydantic v2 does not coerce
    datetime→str, so the view must convert — the pre-fix GET
    /zone-transfers/{id} was a guaranteed 500 the moment transfer arming made
    it reachable."""
    from datetime import UTC, datetime

    from nexus.storage.models import ZoneOperationModel

    app = _app()
    factory = app.state.zone_session_factory
    now = datetime.now(UTC)
    with factory() as s:
        s.add(
            ZoneOperationModel(
                operation_id="op_transfer_1",
                action="transfer",
                zone_id=None,
                state="failed",
                step="transfer-policy",
                retryable=False,
                idempotency_scope='["moss-provisioner", "zone.transfer", "team-alpha"]',
                idempotency_key="tk1",
                request_hash="rh",
                created_at=now,
                updated_at=now,
                completed_at=now,
            )
        )
        s.commit()
    with TestClient(app) as client:
        resp = client.get("/v2/zone-transfers/op_transfer_1")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["operation_id"] == "op_transfer_1"
        assert body["action"] == "transfer"
        # ISO-8601 strings, not raw datetime reprs (pydantic v2 does not
        # coerce datetime→str; SQLite datetimes come back tz-naive).
        assert "T" in body["created_at"] and "T" in body["completed_at"]


def test_contract_invalid_payloads_are_422_not_500() -> None:
    """H-2: every contract-shape violation must be a 422 with a machine
    code — the pre-fix handler-internal contract construction turned each of
    these into an unhandled ValidationError (HTTP 500)."""
    from fastapi.exceptions import RequestValidationError

    from nexus.server.fastapi_server import zone_v2_validation_error_handler

    app = _app()
    app.add_exception_handler(RequestValidationError, zone_v2_validation_error_handler)
    headers = {"Idempotency-Key": "h2-case"}

    with TestClient(app) as client:
        # zone_id admission shape (schema: 3-63 lowercase/digits/hyphen, and
        # root/__control__ reserved).
        for bad_id in ("root", "TeamUP", "-abc"):
            resp = client.post(
                "/v2/zones", headers=headers, json={"zone_id": bad_id, "display_name": "X"}
            )
            assert resp.status_code == 422, (bad_id, resp.text)
            assert resp.json()["detail"]["code"] == "INVALID_ZONE_ID", bad_id

        # deployment.location outside the frozen enum.
        resp = client.post(
            "/v2/zones",
            headers=headers,
            json={
                "zone_id": "team-beta",
                "display_name": "B",
                "deployment": {"location": "mars", "trust_domain": "td"},
            },
        )
        assert resp.status_code == 422, resp.text
        assert resp.json()["detail"]["code"] == "INVALID_REQUEST"

        # deployment.location missing — the server no longer defaults it to
        # "cloud"; the frozen schema marks it required.
        resp = client.post(
            "/v2/zones",
            headers=headers,
            json={
                "zone_id": "team-beta",
                "display_name": "B",
                "deployment": {"trust_domain": "td"},
            },
        )
        assert resp.status_code == 422, resp.text

        # grant body: unknown subject_type literal.
        resp = client.post(
            "/v2/zones/team-alpha/grants",
            headers=headers,
            json={
                "grantee": {"subject_type": "team", "subject_id": "t1"},
                "capabilities": ["zone.data.read"],
                "reason": "r",
            },
        )
        assert resp.status_code == 422, resp.text
        assert resp.json()["detail"]["code"] == "INVALID_REQUEST"

        # grant body: path-traversal prefix — the schema layer rejects '..'
        # components, and so must the body layer.
        resp = client.post(
            "/v2/zones/team-alpha/grants",
            headers=headers,
            json={
                "grantee": {"subject_type": "user", "subject_id": "u1"},
                "capabilities": ["zone.data.read"],
                "resource_prefixes": ["../etc"],
                "reason": "r",
            },
        )
        assert resp.status_code == 422, resp.text
        assert resp.json()["detail"]["code"] == "INVALID_REQUEST"

        # capability outside the open registry's pattern.
        resp = client.post(
            "/v2/zones/team-alpha/grants",
            headers=headers,
            json={
                "grantee": {"subject_type": "user", "subject_id": "u1"},
                "capabilities": ["read"],
                "reason": "r",
            },
        )
        assert resp.status_code == 422, resp.text


def test_create_and_mount_are_operation_backed() -> None:
    with TestClient(_app()) as client:
        capabilities = client.get("/v2/zone-capabilities")
        assert capabilities.status_code == 200
        assert capabilities.json()["providers"]["composite_armed"] is True

        created = client.post(
            "/v2/zones",
            headers={"Idempotency-Key": "create-a"},
            json={"zone_id": "team-alpha", "display_name": "Alpha"},
        )
        assert created.status_code == 202
        assert created.headers["location"].startswith("/v2/zone-operations/")
        assert created.json()["state"] == "succeeded"

        second = client.post(
            "/v2/zones",
            headers={"Idempotency-Key": "create-b"},
            json={"zone_id": "team-beta", "display_name": "Beta"},
        )
        assert second.status_code == 202

        mounted = client.post(
            "/v2/zone-mounts",
            headers={"Idempotency-Key": "mount-a"},
            json={
                "parent_zone_id": "team-alpha",
                "target_zone_id": "team-beta",
                "path": "/shared",
            },
        )
        assert mounted.status_code == 202
        assert mounted.json()["state"] == "succeeded"


def test_transfer_refuses_when_policy_is_not_armed() -> None:
    with TestClient(_app()) as client:
        response = client.post(
            "/v2/zone-transfers",
            headers={"Idempotency-Key": "transfer-a"},
            json={
                "source": {
                    "api_version": "common.sudo.dev/v1",
                    "kind": "ResourceRef",
                    "zone_id": "team-alpha",
                    "path": "/a",
                },
                "target": {
                    "api_version": "common.sudo.dev/v1",
                    "kind": "ResourceRef",
                    "zone_id": "team-beta",
                    "path": "/b",
                },
            },
        )
        assert response.status_code == 501
        assert response.json()["detail"]["code"] == "UNSUPPORTED_CAPABILITY"


def test_zone_api_requires_authenticated_context() -> None:
    app = _app()
    app.dependency_overrides.pop(require_auth)
    app.state.api_key = "required"
    app.state.auth_provider = None
    with TestClient(app) as client:
        response = client.get("/v2/zone-capabilities")
        assert response.status_code == 401


def test_legacy_mutations_delegate_and_advertise_sunset() -> None:
    with TestClient(_app()) as client:
        created = client.post("/api/zones", json={"zone_id": "legacy-zone", "name": "Legacy"})
        assert created.status_code == 201
        assert created.json()["phase"] == "Active"
        assert created.headers["deprecation"] == "true"
        assert created.headers["link"] == '</v2/zones>; rel="successor-version"'

        deleted = client.delete(
            "/api/zones/legacy-zone", headers={"X-Nexus-Confirm-Zone": "legacy-zone"}
        )
        assert deleted.status_code == 202
        assert deleted.json()["phase"] == "Terminating"
        assert deleted.headers["location"].startswith("/v2/zone-operations/")


def test_legacy_create_reserved_id_answers_400_not_500() -> None:
    """root hits the vendored not-enum at ContractCreate time; the legacy
    adapter must answer the contract error shape, not an unhandled 500."""
    with TestClient(_app()) as client:
        response = client.post("/api/zones", json={"zone_id": "root", "name": "Root"})
        assert response.status_code == 400
        assert response.json()["detail"]["code"] == "INVALID_ZONE_ID"

        # a plain reserved word that passes the vendored schema is still
        # refused by the service-level RESERVED_ZONE_IDS gate
        reserved = client.post("/api/zones", json={"zone_id": "admin", "name": "Admin"})
        assert reserved.status_code == 400
        assert reserved.json()["detail"]["code"] == "RESERVED_ZONE_ID"


def test_legacy_create_duplicate_id_answers_409_not_500() -> None:
    with TestClient(_app()) as client:
        first = client.post("/api/zones", json={"zone_id": "dupe-zone", "name": "First"})
        assert first.status_code == 201
        # the legacy adapter keys idempotency on the zone id, so a re-POST
        # with a different body is an idempotency conflict — 409 either way,
        # never the unhandled 500 this path used to raise
        second = client.post("/api/zones", json={"zone_id": "dupe-zone", "name": "Second"})
        assert second.status_code == 409
        assert second.json()["detail"]["code"] == "IDEMPOTENCY_CONFLICT"


def test_legacy_create_refuses_dropped_domain_field() -> None:
    with TestClient(_app()) as client:
        response = client.post(
            "/api/zones", json={"zone_id": "dom-zone", "name": "Dom", "domain": "x.com"}
        )
        assert response.status_code == 400
        assert response.json()["detail"]["code"] == "INVALID_REQUEST"


def test_enabled_startup_refuses_missing_mandatory_provider(monkeypatch) -> None:
    app = FastAPI()
    app.state.session_factory = lambda: None
    app.state.rebac_manager = None
    app.state.nexus_fs = None
    monkeypatch.setenv("NEXUS_ZONE_CONTROL_ENABLED", "true")
    with pytest.raises(ZoneControlNotArmed):
        asyncio.run(startup_zone_control(app))


def _startup_ready_app() -> FastAPI:
    app = FastAPI()
    app.state.session_factory = lambda: None
    app.state.rebac_manager = object()
    app.state.nexus_fs = object()
    app.state.api_key = "configured"
    app.state.deployment_profile = "full"
    return app


def _stub_startup_dependencies(monkeypatch, worker) -> None:
    monkeypatch.setattr(
        zone_control_module,
        "arm_zone_services",
        lambda app, **kwargs: {"composite_armed": True, "membership": kwargs["membership_check"]},
    )
    monkeypatch.setattr(zone_control_module, "zone_worker", lambda app: worker)


def test_full_without_delegation_issuers_does_not_require_membership(monkeypatch) -> None:
    class Worker:
        def pump_once(self):
            return 0

        def reconcile_stale_operations(self):
            return 0

    app = _startup_ready_app()
    _stub_startup_dependencies(monkeypatch, Worker())
    monkeypatch.delenv("NEXUS_ZONE_DELEGATION_ISSUERS", raising=False)
    monkeypatch.delenv("NEXUS_ZONE_MEMBERSHIP_URL", raising=False)
    monkeypatch.delenv("NEXUS_ZONE_MEMBERSHIP_TOKEN", raising=False)

    async def run() -> None:
        tasks = await startup_zone_control(app)
        app.state.zone_worker_stop.set()
        await asyncio.gather(*tasks)

    asyncio.run(run())


@pytest.mark.parametrize("missing", ["url", "token"])
def test_delegation_issuer_refuses_partial_membership_config(monkeypatch, missing: str) -> None:
    app = _startup_ready_app()
    monkeypatch.setenv("NEXUS_ZONE_DELEGATION_ISSUERS", "moss-e2e")
    monkeypatch.setenv("NEXUS_ZONE_MEMBERSHIP_URL", "http://moss/membership")
    monkeypatch.setenv("NEXUS_ZONE_MEMBERSHIP_TOKEN", "secret")
    monkeypatch.delenv(
        "NEXUS_ZONE_MEMBERSHIP_URL" if missing == "url" else "NEXUS_ZONE_MEMBERSHIP_TOKEN"
    )
    with pytest.raises(ZoneControlNotArmed, match="requires both"):
        asyncio.run(startup_zone_control(app))


def test_delegation_issuer_with_complete_membership_config_starts(monkeypatch) -> None:
    class Worker:
        def pump_once(self):
            return 0

        def reconcile_stale_operations(self):
            return 0

    app = _startup_ready_app()
    _stub_startup_dependencies(monkeypatch, Worker())
    monkeypatch.setenv("NEXUS_ZONE_DELEGATION_ISSUERS", "moss-e2e")
    monkeypatch.setenv("NEXUS_ZONE_MEMBERSHIP_URL", "http://moss/membership")
    monkeypatch.setenv("NEXUS_ZONE_MEMBERSHIP_TOKEN", "secret")

    async def run() -> None:
        tasks = await startup_zone_control(app)
        assert callable(app.state.moss_membership_verifier)
        app.state.zone_worker_stop.set()
        await asyncio.gather(*tasks)

    asyncio.run(run())


def test_worker_survives_one_iteration_failure(monkeypatch) -> None:
    class Worker:
        calls = 0

        def pump_once(self):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("transient")
            return 0

        def reconcile_stale_operations(self):
            return 0

    worker = Worker()
    app = _startup_ready_app()
    _stub_startup_dependencies(monkeypatch, worker)
    monkeypatch.delenv("NEXUS_ZONE_DELEGATION_ISSUERS", raising=False)
    monkeypatch.delenv("NEXUS_ZONE_MEMBERSHIP_URL", raising=False)
    monkeypatch.delenv("NEXUS_ZONE_MEMBERSHIP_TOKEN", raising=False)

    async def run() -> None:
        tasks = await startup_zone_control(app)
        deadline = asyncio.get_running_loop().time() + 2
        while worker.calls < 2 and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.05)
        app.state.zone_worker_stop.set()
        await asyncio.gather(*tasks)

    asyncio.run(run())
    assert worker.calls >= 2


# ── token zone boundary / narrow-path authorization regressions ─────────────


def _seed_zone(app: FastAPI, zone_id: str) -> None:
    from datetime import UTC, datetime

    from nexus.storage.models.auth import ZoneModel

    factory = app.state.zone_session_factory
    now = datetime.now(UTC)
    with factory() as s:
        s.add(
            ZoneModel(
                zone_id=zone_id,
                name=zone_id,
                phase="Active",
                canonical_status="active",
                created_by={"subject_type": "user", "subject_id": "usr-admin"},
                created_at=now,
                updated_at=now,
            )
        )
        s.commit()


def test_zone_binding_boundary_requires_a_verified_delegation() -> None:
    """A non-empty X-Nexus-Zone-Delegation used to skip ZONE_OUT_OF_TOKEN_SCOPE
    without ever being validated: any fabricated header crossed the token
    boundary whenever a direct grant held for the target zone."""
    from datetime import UTC, datetime, timedelta

    from nexus.storage.models import ZoneDelegationModel, ZoneGrantModel
    from nexus.storage.models.zone_v1 import ZoneAuthorizationEpochModel

    app = _app()
    _seed_zone(app, "zone-a")
    _seed_zone(app, "zone-b")
    factory = app.state.zone_session_factory
    now = datetime.now(UTC)
    with factory() as s:
        # a DIRECT grant for the user on zone-b (the pre-fix bypass surface)
        s.add(
            ZoneGrantModel(
                grant_id="grant-direct",
                zone_id="zone-b",
                grantee={"subject_type": "user", "subject_id": "usr-1"},
                capabilities=["zone.grants.manage"],
                resource_prefixes=None,
                source_type="manual",
                source_id="seed-direct",
                issued_by={"subject_type": "user", "subject_id": "usr-admin"},
                reason="seed",
                policy_version="p1",
                revision="r1",
                status="active",
                created_at=now,
            )
        )
        s.add(ZoneAuthorizationEpochModel(zone_id="zone-b", epoch=1, advanced_at=now))
        # a VALID delegation on zone-b for the same user
        s.add(
            ZoneDelegationModel(
                delegation_id="dlg-ok",
                user_id="usr-1",
                org_id="org-1",
                membership_version="mv1",
                zone_id="zone-b",
                grant_id="grant-direct",
                grant_revision="r1",
                epoch=1,
                audience="nexus-api",
                status="active",
                purpose="data-access",
                scope_rules=[{"capability": "zone.grants.manage", "resource_prefixes": ["/"]}],
                issued_at=now,
                expires_at=now + timedelta(hours=1),
            )
        )
        s.commit()
    app.dependency_overrides[require_auth] = lambda: {
        "authenticated": True,
        "is_admin": False,
        "subject_type": "user",
        "subject_id": "usr-1",
        "zone_id": "zone-a",  # the token is bound to zone-a only
    }
    with TestClient(app) as client:
        no_header = client.get("/v2/zones/zone-b/grants")
        assert no_header.status_code == 403
        assert no_header.json()["detail"]["code"] == "ZONE_OUT_OF_TOKEN_SCOPE"

        fabricated = client.get(
            "/v2/zones/zone-b/grants", headers={"X-Nexus-Zone-Delegation": "bogus"}
        )
        assert fabricated.status_code == 403
        assert fabricated.json()["detail"]["code"] == "ZONE_OUT_OF_TOKEN_SCOPE"

        verified = client.get(
            "/v2/zones/zone-b/grants", headers={"X-Nexus-Zone-Delegation": "dlg-ok"}
        )
        assert verified.status_code == 200, verified.text


def test_start_run_authorizes_on_the_session_path() -> None:
    """zone_active_check used to authorize on "/" — a grant scoped to
    /sessions never covered it, so start_run 403'd (RESOURCE_RELATION_DENIED)
    despite a valid delegation on the exact session path."""
    from datetime import UTC, datetime, timedelta

    from nexus.storage.models import ZoneDelegationModel, ZoneGrantModel
    from nexus.storage.models.session_v1 import SessionModel
    from nexus.storage.models.zone_v1 import ZoneAuthorizationEpochModel

    app = _app(with_sessions=True)
    # the unit fixture arms no kernel fs; the implicit-task ledger only
    # needs the writer callable to be present (path, bytes, zone_id -> int)
    from nexus.services.zones.session_tasks import SessionTaskService

    app.state.session_task_service = SessionTaskService(
        app.state.zone_session_factory, fs_writer=lambda path, data, zone_id: len(data)
    )
    _seed_zone(app, "zone-run")
    factory = app.state.zone_session_factory
    now = datetime.now(UTC)
    owner = {"subject_type": "user", "subject_id": "usr-1"}
    with factory() as s:
        s.add(
            SessionModel(
                session_id="sess-1",
                home_zone_id="zone-run",
                owner=owner,
                state="active",
                created_by=owner,
                created_at=now,
                updated_at=now,
            )
        )
        s.add(
            ZoneGrantModel(
                grant_id="grant-run",
                zone_id="zone-run",
                grantee={"subject_type": "organization", "subject_id": "org-1"},
                capabilities=["zone.data.write", "zone.runtime.execute"],
                resource_prefixes=["/sessions"],  # narrow: does NOT cover "/"
                source_type="manual",
                source_id="seed-run",
                issued_by={"subject_type": "user", "subject_id": "usr-admin"},
                reason="seed",
                policy_version="p1",
                revision="r1",
                status="active",
                created_at=now,
            )
        )
        s.add(ZoneAuthorizationEpochModel(zone_id="zone-run", epoch=1, advanced_at=now))
        s.add(
            ZoneDelegationModel(
                delegation_id="dlg-run",
                user_id="usr-1",
                org_id="org-1",
                membership_version="mv1",
                zone_id="zone-run",
                grant_id="grant-run",
                grant_revision="r1",
                epoch=1,
                audience="nexus-api",
                status="active",
                purpose="data-access",
                scope_rules=[
                    {
                        "capability": "zone.runtime.execute",
                        "resource_prefixes": ["/sessions/sess-1"],
                    }
                ],
                issued_at=now,
                expires_at=now + timedelta(hours=1),
            )
        )
        s.commit()
    app.dependency_overrides[require_auth] = lambda: {
        "authenticated": True,
        "is_admin": False,
        "subject_type": "user",
        "subject_id": "usr-1",
    }
    with TestClient(app) as client:
        resp = client.post(
            "/v2/runtime/start",
            json={"pid": "run-1", "session_id": "sess-1", "delegation_ref": "dlg-run"},
        )
        assert resp.status_code == 201, resp.text


def test_grant_prefix_column_budget_is_enforced_at_validation() -> None:
    """Grant prefixes feed a 255-char projection column as '{prefix}/*', so
    anything beyond 253 chars must die as a 422 at the validation layer —
    not as a projection failure after the grant row exists."""
    app = _app()
    _seed_zone(app, "zone-pfx")
    with TestClient(app) as client:
        headers = {"Idempotency-Key": "pfx-budget"}
        body = {
            "grantee": {"subject_type": "user", "subject_id": "usr-1"},
            "capabilities": ["zone.data.read"],
            "reason": "prefix budget probe",
        }
        at_budget = dict(body, resource_prefixes=["/" + "a" * 252])  # 253 chars
        ok = client.post("/v2/zones/zone-pfx/grants", headers=headers, json=at_budget)
        assert ok.status_code == 202, ok.text

        over_budget = dict(body, resource_prefixes=["/" + "a" * 253])  # 254 chars
        rejected = client.post(
            "/v2/zones/zone-pfx/grants",
            headers={**headers, "Idempotency-Key": "pfx-budget-2"},
            json=over_budget,
        )
        assert rejected.status_code == 422, rejected.text
