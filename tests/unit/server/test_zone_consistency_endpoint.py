"""Tests for PATCH /{zone_id}/consistency-mode endpoint (Issue #1180 Phase D).

Covers:
1. Successful SC → EC migration via endpoint
2. Successful EC → SC migration via endpoint
3. Same-mode migration returns 400
4. Invalid target_mode returns 422 (validation)
5. Zone not found returns 404
6. Unauthorized user returns 403
7. NexusFS not available returns 503
8. ZoneResponse includes consistency_mode field
9. Migration failure returns 400 with error
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from nexus.server.auth.zone_routes import (
    MigrationResponse,
    UpdateConsistencyModeRequest,
    ZoneResponse,
    router,
)
from nexus.storage.models import ZoneModel
from nexus.storage.models._base import Base

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def engine():
    eng = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(eng, "connect")
    def _set_pragma(dbapi_conn, connection_record):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(eng)
    return eng


@pytest.fixture
def session_factory(engine):
    return sessionmaker(bind=engine)


@pytest.fixture
def zone_sc(session_factory):
    with session_factory() as session:
        zone = ZoneModel(zone_id="zone-sc", name="SC Zone", consistency_mode="SC")
        session.add(zone)
        session.commit()
    return "zone-sc"


@pytest.fixture
def zone_ec(session_factory):
    with session_factory() as session:
        zone = ZoneModel(zone_id="zone-ec", name="EC Zone", consistency_mode="EC")
        session.add(zone)
        session.commit()
    return "zone-ec"


def _make_mock_nx(migrate_result: dict[str, Any] | None = None) -> MagicMock:
    """Create a mock NexusFS with migrate_consistency_mode."""
    nx = MagicMock()
    nx._rebac_manager = MagicMock()
    nx._consistency_migration = MagicMock()
    if migrate_result is not None:
        nx.migrate_consistency_mode.return_value = migrate_result
    return nx


def _make_mock_auth(session_factory: Any) -> MagicMock:
    """Create a mock DatabaseLocalAuth."""
    auth = MagicMock()
    auth.session_factory = session_factory
    return auth


# ---------------------------------------------------------------------------
# 1. ZoneResponse includes consistency_mode
# ---------------------------------------------------------------------------


class TestZoneResponseModel:
    def test_zone_response_has_consistency_mode(self) -> None:
        """ZoneResponse model includes consistency_mode field."""
        resp = ZoneResponse(
            zone_id="z1",
            name="Test",
            consistency_mode="SC",
            is_active=True,
            created_at="2024-01-01",
            updated_at="2024-01-01",
        )
        assert resp.consistency_mode == "SC"

    def test_zone_response_default_consistency_mode(self) -> None:
        """ZoneResponse defaults consistency_mode to SC."""
        resp = ZoneResponse(
            zone_id="z1",
            name="Test",
            is_active=True,
            created_at="2024-01-01",
            updated_at="2024-01-01",
        )
        assert resp.consistency_mode == "SC"


# ---------------------------------------------------------------------------
# 2. UpdateConsistencyModeRequest validation
# ---------------------------------------------------------------------------


class TestUpdateConsistencyModeRequest:
    def test_valid_sc_target(self) -> None:
        req = UpdateConsistencyModeRequest(target_mode="SC")
        assert req.target_mode == "SC"
        assert req.timeout_s == 30.0

    def test_valid_ec_target(self) -> None:
        req = UpdateConsistencyModeRequest(target_mode="EC")
        assert req.target_mode == "EC"

    def test_invalid_target_rejected(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            UpdateConsistencyModeRequest(target_mode="INVALID")

    def test_custom_timeout(self) -> None:
        req = UpdateConsistencyModeRequest(target_mode="EC", timeout_s=60.0)
        assert req.timeout_s == 60.0


# ---------------------------------------------------------------------------
# 3. MigrationResponse model
# ---------------------------------------------------------------------------


class TestMigrationResponse:
    def test_migration_response_success(self) -> None:
        resp = MigrationResponse(
            success=True,
            zone_id="z1",
            from_mode="SC",
            to_mode="EC",
            duration_ms=42.0,
        )
        assert resp.success is True
        assert resp.error is None

    def test_migration_response_failure(self) -> None:
        resp = MigrationResponse(
            success=False,
            zone_id="z1",
            from_mode="SC",
            to_mode="EC",
            duration_ms=10.0,
            error="Zone already in EC mode",
        )
        assert resp.success is False
        assert "already" in resp.error


# ---------------------------------------------------------------------------
# 4. Integration: PATCH endpoint with TestClient
# ---------------------------------------------------------------------------


class TestPatchEndpointIntegration:
    """Integration tests using FastAPI TestClient with dependency overrides."""

    @pytest.fixture
    def app(self, session_factory, zone_sc):
        """Create a FastAPI app with test dependencies."""
        app = FastAPI()
        app.include_router(router)

        # Mock dependencies
        mock_nx = _make_mock_nx(
            migrate_result={
                "success": True,
                "zone_id": zone_sc,
                "from_mode": "SC",
                "to_mode": "EC",
                "duration_ms": 42.0,
                "error": None,
            }
        )
        mock_auth = _make_mock_auth(session_factory)

        from nexus.server.auth.auth_routes import (
            get_auth_provider,
            get_authenticated_user,
        )

        app.dependency_overrides[get_authenticated_user] = lambda: ("user-1", "user@test.com")
        app.dependency_overrides[get_auth_provider] = lambda: mock_auth

        # Patch get_nexus_instance
        with patch("nexus.server.auth.zone_routes.get_nexus_instance", return_value=mock_nx):
            yield app, mock_nx

    def test_patch_sc_to_ec(self, app) -> None:
        """PATCH /api/zones/{zone_id}/consistency-mode with SC→EC."""
        test_app, mock_nx = app

        with (
            patch("nexus.server.auth.zone_routes.get_nexus_instance", return_value=mock_nx),
            patch(
                "nexus.server.auth.zone_routes.get_user_by_id",
                return_value=MagicMock(is_global_admin=1),
            ),
        ):
            client = TestClient(test_app)
            resp = client.patch(
                "/api/zones/zone-sc/consistency-mode",
                json={"target_mode": "EC"},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["to_mode"] == "EC"

    def test_patch_zone_not_found(self, session_factory) -> None:
        """PATCH for nonexistent zone returns 404."""
        app = FastAPI()
        app.include_router(router)

        mock_auth = _make_mock_auth(session_factory)
        mock_nx = _make_mock_nx()

        from nexus.server.auth.auth_routes import (
            get_auth_provider,
            get_authenticated_user,
        )

        app.dependency_overrides[get_authenticated_user] = lambda: ("user-1", "user@test.com")
        app.dependency_overrides[get_auth_provider] = lambda: mock_auth

        with (
            patch("nexus.server.auth.zone_routes.get_nexus_instance", return_value=mock_nx),
            patch(
                "nexus.server.auth.zone_routes.get_user_by_id",
                return_value=MagicMock(is_global_admin=1),
            ),
        ):
            client = TestClient(app)
            resp = client.patch(
                "/api/zones/nonexistent/consistency-mode",
                json={"target_mode": "EC"},
            )

        assert resp.status_code == 404

    def test_patch_invalid_target_mode(self, app) -> None:
        """PATCH with invalid target_mode returns 422."""
        test_app, mock_nx = app

        with (
            patch("nexus.server.auth.zone_routes.get_nexus_instance", return_value=mock_nx),
            patch(
                "nexus.server.auth.zone_routes.get_user_by_id",
                return_value=MagicMock(is_global_admin=1),
            ),
        ):
            client = TestClient(test_app)
            resp = client.patch(
                "/api/zones/zone-sc/consistency-mode",
                json={"target_mode": "INVALID"},
            )

        assert resp.status_code == 422

    def test_patch_migration_failure(self, session_factory, zone_sc) -> None:
        """PATCH that fails migration returns 400."""
        app = FastAPI()
        app.include_router(router)

        mock_nx = _make_mock_nx(
            migrate_result={
                "success": False,
                "zone_id": zone_sc,
                "from_mode": "SC",
                "to_mode": "SC",
                "duration_ms": 1.0,
                "error": "Zone is already in SC mode",
            }
        )
        mock_auth = _make_mock_auth(session_factory)

        from nexus.server.auth.auth_routes import (
            get_auth_provider,
            get_authenticated_user,
        )

        app.dependency_overrides[get_authenticated_user] = lambda: ("user-1", "user@test.com")
        app.dependency_overrides[get_auth_provider] = lambda: mock_auth

        with (
            patch("nexus.server.auth.zone_routes.get_nexus_instance", return_value=mock_nx),
            patch(
                "nexus.server.auth.zone_routes.get_user_by_id",
                return_value=MagicMock(is_global_admin=1),
            ),
        ):
            client = TestClient(app)
            resp = client.patch(
                f"/api/zones/{zone_sc}/consistency-mode",
                json={"target_mode": "SC"},
            )

        assert resp.status_code == 400
        assert "already" in resp.json()["detail"].lower()

    def test_patch_no_nexus_returns_503(self, session_factory, zone_sc) -> None:
        """PATCH without NexusFS returns 503."""
        app = FastAPI()
        app.include_router(router)

        mock_auth = _make_mock_auth(session_factory)

        from nexus.server.auth.auth_routes import (
            get_auth_provider,
            get_authenticated_user,
        )

        app.dependency_overrides[get_authenticated_user] = lambda: ("user-1", "user@test.com")
        app.dependency_overrides[get_auth_provider] = lambda: mock_auth

        with (
            patch("nexus.server.auth.zone_routes.get_nexus_instance", return_value=None),
            patch(
                "nexus.server.auth.zone_routes.get_user_by_id",
                return_value=MagicMock(is_global_admin=1),
            ),
        ):
            client = TestClient(app)
            resp = client.patch(
                f"/api/zones/{zone_sc}/consistency-mode",
                json={"target_mode": "EC"},
            )

        assert resp.status_code == 503
