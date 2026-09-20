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
from nexus.server.api.v2.routers.zone_grants import router as grants_router
from nexus.server.api.v2.routers.zone_runtime import router as runtime_router
from nexus.server.api.v2.routers.zones import router as zones_router
from nexus.server.auth.zone_routes import router as legacy_zone_router
from nexus.server.dependencies import require_auth
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

    def unmount(self, *, mount_ref, ctx):
        return RuntimeReceipt(ok=True, runtime_revision="1:3:3", raw={"outcome": "UNMOUNTED"})

    def remove_replica(self, *, zone_id, force, ctx):
        return RuntimeReceipt(ok=True, raw={"outcome": "REPLICA_REMOVED"})

    def deprovision(self, *, zone_id, deletion_epoch, ctx):
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


def _app() -> FastAPI:
    from nexus.storage.models import auth as auth_models
    from nexus.storage.models import zone_v1 as zone_models

    engine = sa.create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=sa.pool.StaticPool
    )
    auth_models.ZoneModel.__table__.create(engine)
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

        deleted = client.delete("/api/zones/legacy-zone")
        assert deleted.status_code == 202
        assert deleted.json()["phase"] == "Terminating"
        assert deleted.headers["location"].startswith("/v2/zone-operations/")


def test_enabled_startup_refuses_missing_mandatory_provider(monkeypatch) -> None:
    app = FastAPI()
    app.state.session_factory = lambda: None
    app.state.rebac_manager = None
    app.state.nexus_fs = None
    monkeypatch.setenv("NEXUS_ZONE_CONTROL_ENABLED", "true")
    with pytest.raises(ZoneControlNotArmed):
        asyncio.run(startup_zone_control(app))
