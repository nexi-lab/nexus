"""E2E tests for GET /api/v2/features endpoint.

Issue #1389: Feature flags for deployment modes.

Tests:
- Features endpoint returns correct profile and bricks
- Default profile is 'full'
- Endpoint is accessible without auth
- Response includes version info
"""

from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from nexus.contracts.deployment_profile import (
    ALL_BRICK_NAMES,
    DeploymentProfile,
)
from nexus.server.api.core.features import FeaturesResponse, router


@pytest.fixture
def app_with_features() -> FastAPI:
    """Create a FastAPI app with features endpoint and pre-computed features_info."""
    app = FastAPI()

    # Mock minimal app.state (features endpoint reads from app.state.features_info)
    full_bricks = DeploymentProfile.FULL.default_bricks()
    disabled = sorted(ALL_BRICK_NAMES - full_bricks)

    app.state.features_info = FeaturesResponse(
        profile="full",
        mode="standalone",
        enabled_bricks=sorted(full_bricks),
        disabled_bricks=disabled,
        version="0.test.0",
    )
    app.state.limiter = MagicMock()

    app.include_router(router)
    return app


@pytest.fixture
def client(app_with_features: FastAPI) -> TestClient:
    return TestClient(app_with_features)


class TestFeaturesEndpoint:
    """Tests for GET /api/v2/features."""

    def test_returns_200(self, client: TestClient) -> None:
        resp = client.get("/api/v2/features")
        assert resp.status_code == 200

    def test_returns_profile(self, client: TestClient) -> None:
        data = client.get("/api/v2/features").json()
        assert data["profile"] == "full"

    def test_returns_mode(self, client: TestClient) -> None:
        data = client.get("/api/v2/features").json()
        assert data["mode"] == "standalone"

    def test_returns_enabled_bricks(self, client: TestClient) -> None:
        data = client.get("/api/v2/features").json()
        assert isinstance(data["enabled_bricks"], list)
        assert len(data["enabled_bricks"]) > 0
        # Full profile should include search and pay
        assert "search" in data["enabled_bricks"]
        assert "pay" in data["enabled_bricks"]

    def test_returns_disabled_bricks(self, client: TestClient) -> None:
        data = client.get("/api/v2/features").json()
        assert isinstance(data["disabled_bricks"], list)
        # Federation is a system service (not a brick) — won't appear in brick lists

    def test_returns_version(self, client: TestClient) -> None:
        data = client.get("/api/v2/features").json()
        assert data["version"] == "0.test.0"

    def test_bricks_are_sorted(self, client: TestClient) -> None:
        data = client.get("/api/v2/features").json()
        assert data["enabled_bricks"] == sorted(data["enabled_bricks"])
        assert data["disabled_bricks"] == sorted(data["disabled_bricks"])


class TestFeaturesEndpointLiteProfile:
    """Tests for features endpoint with lite profile."""

    @pytest.fixture
    def lite_app(self) -> FastAPI:
        app = FastAPI()
        lite_bricks = DeploymentProfile.LITE.default_bricks()
        disabled = sorted(ALL_BRICK_NAMES - lite_bricks)

        app.state.features_info = FeaturesResponse(
            profile="lite",
            mode="standalone",
            enabled_bricks=sorted(lite_bricks),
            disabled_bricks=disabled,
            version="0.test.0",
        )
        app.state.limiter = MagicMock()
        app.include_router(router)
        return app

    @pytest.fixture
    def lite_client(self, lite_app: FastAPI) -> TestClient:
        return TestClient(lite_app)

    def test_lite_profile(self, lite_client: TestClient) -> None:
        data = lite_client.get("/api/v2/features").json()
        assert data["profile"] == "lite"

    def test_lite_disables_search(self, lite_client: TestClient) -> None:
        data = lite_client.get("/api/v2/features").json()
        assert "search" not in data["enabled_bricks"]
        assert "search" in data["disabled_bricks"]

    def test_lite_disables_pay(self, lite_client: TestClient) -> None:
        data = lite_client.get("/api/v2/features").json()
        assert "pay" not in data["enabled_bricks"]
        assert "pay" in data["disabled_bricks"]

    def test_lite_enables_core(self, lite_client: TestClient) -> None:
        data = lite_client.get("/api/v2/features").json()
        assert "permissions" in data["enabled_bricks"]
        assert "cache" in data["enabled_bricks"]


class TestFeaturesEndpointFallback:
    """Tests for features endpoint when features_info is not pre-computed."""

    @pytest.fixture
    def fallback_app(self) -> FastAPI:
        app = FastAPI()
        # Do NOT set app.state.features_info — test fallback
        app.state.limiter = MagicMock()
        app.include_router(router)
        return app

    @pytest.fixture
    def fallback_client(self, fallback_app: FastAPI) -> TestClient:
        return TestClient(fallback_app)

    def test_fallback_returns_200(self, fallback_client: TestClient) -> None:
        resp = fallback_client.get("/api/v2/features")
        assert resp.status_code == 200

    def test_fallback_defaults_to_full(self, fallback_client: TestClient) -> None:
        data = fallback_client.get("/api/v2/features").json()
        assert data["profile"] == "full"


def _svc_from_app(app: FastAPI) -> Any:
    """Build a minimal LifespanServices from app.state for testing."""
    from nexus.server.lifespan.services_container import LifespanServices

    return LifespanServices(
        deployment_profile=getattr(app.state, "deployment_profile", "full"),
        deployment_mode=getattr(app.state, "deployment_mode", "standalone"),
        enabled_bricks=getattr(app.state, "enabled_bricks", frozenset()),
        profile_tuning=getattr(app.state, "profile_tuning", None),
    )


class TestComputeFeaturesInfo:
    """Tests for _compute_features_info lifespan function."""

    def test_compute_sets_app_state(self) -> None:
        from nexus.server.lifespan import _compute_features_info

        app = FastAPI()
        app.state.deployment_profile = "lite"
        app.state.deployment_mode = "standalone"

        _compute_features_info(app, _svc_from_app(app))

        info: Any = app.state.features_info
        assert info.profile == "lite"
        assert info.mode == "standalone"
        assert "search" not in info.enabled_bricks

    def test_compute_with_enabled_bricks_override(self) -> None:
        from nexus.server.lifespan import _compute_features_info

        app = FastAPI()
        app.state.deployment_profile = "lite"
        app.state.deployment_mode = "standalone"
        # Explicitly override enabled_bricks with search added
        from nexus.contracts.deployment_profile import BRICK_SEARCH, resolve_enabled_bricks

        custom_bricks = resolve_enabled_bricks(
            DeploymentProfile.LITE, overrides={BRICK_SEARCH: True}
        )
        app.state.enabled_bricks = custom_bricks

        _compute_features_info(app, _svc_from_app(app))

        info: Any = app.state.features_info
        assert "search" in info.enabled_bricks

    def test_compute_defaults_to_full(self) -> None:
        from nexus.server.lifespan import _compute_features_info

        app = FastAPI()
        # Don't set any state — should default to full
        _compute_features_info(app, _svc_from_app(app))

        info: Any = app.state.features_info
        assert info.profile == "full"

    def test_compute_unknown_profile_falls_back(self) -> None:
        from nexus.server.lifespan import _compute_features_info

        app = FastAPI()
        app.state.deployment_profile = "unknown_profile"
        _compute_features_info(app, _svc_from_app(app))

        info: Any = app.state.features_info
        assert info.profile == "full"
