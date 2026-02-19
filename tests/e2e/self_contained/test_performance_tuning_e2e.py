"""E2E tests for per-profile performance tuning (Issue #2071).

Tests:
- Profile tuning is set on app.state at startup
- Features endpoint includes performance_tuning summary
- LITE profile produces different tuning than FULL
- Tuning values propagate to features response
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from nexus.core.deployment_profile import DeploymentProfile
from nexus.server.api.core.features import router
from nexus.server.lifespan import _compute_features_info


@pytest.fixture
def app_full_profile() -> FastAPI:
    """FastAPI app with FULL profile tuning on app.state."""
    app = FastAPI()
    profile = DeploymentProfile.FULL
    app.state.deployment_profile = profile.value
    app.state.enabled_bricks = profile.default_bricks()
    app.state.profile_tuning = profile.tuning()
    app.state.deployment_mode = "standalone"

    _compute_features_info(app)
    app.include_router(router)
    return app


@pytest.fixture
def app_lite_profile() -> FastAPI:
    """FastAPI app with LITE profile tuning on app.state."""
    app = FastAPI()
    profile = DeploymentProfile.LITE
    app.state.deployment_profile = profile.value
    app.state.enabled_bricks = profile.default_bricks()
    app.state.profile_tuning = profile.tuning()
    app.state.deployment_mode = "standalone"

    _compute_features_info(app)
    app.include_router(router)
    return app


class TestPerformanceTuningOnAppState:
    """Verify profile_tuning is correctly set on app.state."""

    def test_full_profile_tuning_set(self, app_full_profile: FastAPI) -> None:
        tuning = app_full_profile.state.profile_tuning
        assert tuning.concurrency.default_workers == 4
        assert tuning.concurrency.thread_pool_size == 200
        assert tuning.network.default_http_timeout == 30.0
        assert tuning.storage.db_pool_size == 10

    def test_lite_profile_tuning_set(self, app_lite_profile: FastAPI) -> None:
        tuning = app_lite_profile.state.profile_tuning
        assert tuning.concurrency.default_workers == 2
        assert tuning.concurrency.thread_pool_size == 50
        assert tuning.network.default_http_timeout == 30.0
        assert tuning.storage.db_pool_size == 5


class TestFeaturesEndpointWithTuning:
    """Features endpoint includes performance_tuning in response."""

    def test_full_features_includes_tuning(self, app_full_profile: FastAPI) -> None:
        client = TestClient(app_full_profile)
        resp = client.get("/api/v2/features")
        assert resp.status_code == 200
        data = resp.json()
        assert "performance_tuning" in data
        pt = data["performance_tuning"]
        assert pt is not None
        assert pt["thread_pool_size"] == 200
        assert pt["default_workers"] == 4
        assert pt["task_runner_workers"] == 4
        assert pt["default_http_timeout"] == 30.0
        assert pt["db_pool_size"] == 10
        assert pt["search_max_concurrency"] == 10

    def test_lite_features_different_tuning(self, app_lite_profile: FastAPI) -> None:
        client = TestClient(app_lite_profile)
        resp = client.get("/api/v2/features")
        assert resp.status_code == 200
        data = resp.json()
        pt = data["performance_tuning"]
        assert pt is not None
        assert pt["thread_pool_size"] == 50
        assert pt["default_workers"] == 2
        assert pt["task_runner_workers"] == 2
        assert pt["db_pool_size"] == 5
        assert pt["search_max_concurrency"] == 5

    def test_profile_field_matches(self, app_lite_profile: FastAPI) -> None:
        client = TestClient(app_lite_profile)
        resp = client.get("/api/v2/features")
        data = resp.json()
        assert data["profile"] == "lite"


class TestTuningViaEnvVar:
    """NEXUS_PROFILE env var selects correct tuning."""

    def test_embedded_profile_env(self) -> None:
        """Verify embedded profile produces minimal tuning."""
        profile = DeploymentProfile.EMBEDDED
        tuning = profile.tuning()
        assert tuning.concurrency.default_workers == 1
        assert tuning.concurrency.thread_pool_size == 10
        assert tuning.storage.db_pool_size == 2
        assert tuning.search.grep_parallel_workers == 1

    def test_cloud_profile_env(self) -> None:
        """Verify cloud profile produces aggressive tuning."""
        profile = DeploymentProfile.CLOUD
        tuning = profile.tuning()
        assert tuning.concurrency.default_workers == 8
        assert tuning.concurrency.thread_pool_size == 400
        assert tuning.storage.db_pool_size == 20
        assert tuning.search.search_max_concurrency == 20


class TestBackwardCompatibility:
    """Existing module-level constants still work as fallback defaults."""

    def test_grep_parallel_workers_constant(self) -> None:
        from nexus.search.strategies import GREP_PARALLEL_WORKERS

        assert GREP_PARALLEL_WORKERS == 4  # FULL profile default

    def test_list_parallel_workers_constant(self) -> None:
        from nexus.services.search_service import LIST_PARALLEL_WORKERS

        assert LIST_PARALLEL_WORKERS == 10  # FULL profile default

    def test_webhook_timeout_constant(self) -> None:
        from nexus.server.subscriptions.manager import WEBHOOK_TIMEOUT

        assert WEBHOOK_TIMEOUT == 10.0  # FULL profile default

    def test_default_operation_timeout_constant(self) -> None:
        from nexus.server.batch_executor import DEFAULT_OPERATION_TIMEOUT

        assert DEFAULT_OPERATION_TIMEOUT == 30.0  # FULL profile default
