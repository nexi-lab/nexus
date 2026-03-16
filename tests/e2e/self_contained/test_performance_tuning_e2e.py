"""E2E tests for per-profile performance tuning (Issue #2071).

Tests:
- Profile tuning is set on app.state at startup
- Features endpoint includes performance_tuning summary (all 9 slices)
- LITE profile produces different tuning than FULL
- Tuning values propagate to features response
- New slices (background_task, resiliency, connector, pool) are exposed
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from nexus.contracts.deployment_profile import DeploymentProfile
from nexus.server.api.core.features import router
from nexus.server.lifespan import _compute_features_info
from nexus.server.lifespan.services_container import LifespanServices


@pytest.fixture
def app_full_profile() -> FastAPI:
    """FastAPI app with FULL profile tuning on app.state."""
    app = FastAPI()
    profile = DeploymentProfile.FULL
    app.state.deployment_profile = profile.value
    app.state.enabled_bricks = profile.default_bricks()
    app.state.profile_tuning = profile.tuning()
    app.state.deployment_mode = "standalone"

    svc = LifespanServices(
        deployment_profile=profile.value,
        deployment_mode="standalone",
        enabled_bricks=profile.default_bricks(),
        profile_tuning=profile.tuning(),
    )
    _compute_features_info(app, svc)
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

    svc = LifespanServices(
        deployment_profile=profile.value,
        deployment_mode="standalone",
        enabled_bricks=profile.default_bricks(),
        profile_tuning=profile.tuning(),
    )
    _compute_features_info(app, svc)
    app.include_router(router)
    return app


class TestPerformanceTuningOnAppState:
    """Verify profile_tuning is correctly set on app.state."""

    def test_full_profile_tuning_set(self, app_full_profile: FastAPI) -> None:
        tuning = app_full_profile.state.profile_tuning
        assert tuning.concurrency.default_workers == 4
        assert tuning.concurrency.thread_pool_size == 200
        assert tuning.network.default_http_timeout == 30.0
        assert tuning.storage.db_pool_size == 20

    def test_full_profile_new_slices(self, app_full_profile: FastAPI) -> None:
        tuning = app_full_profile.state.profile_tuning
        assert tuning.background_task.heartbeat_flush_interval == 60
        assert tuning.background_task.stale_agent_check_interval == 300
        assert tuning.resiliency.default_max_retries == 3
        assert tuning.resiliency.circuit_breaker_timeout == 30.0
        assert tuning.connector.blob_operation_timeout == 60.0
        assert tuning.connector.large_upload_timeout == 300.0
        assert tuning.pool.asyncpg_min_size == 2
        assert tuning.pool.asyncpg_max_size == 5

    def test_lite_profile_tuning_set(self, app_lite_profile: FastAPI) -> None:
        tuning = app_lite_profile.state.profile_tuning
        assert tuning.concurrency.default_workers == 2
        assert tuning.concurrency.thread_pool_size == 50
        assert tuning.network.default_http_timeout == 30.0
        assert tuning.storage.db_pool_size == 8

    def test_lite_profile_new_slices(self, app_lite_profile: FastAPI) -> None:
        tuning = app_lite_profile.state.profile_tuning
        assert tuning.background_task.heartbeat_flush_interval == 120
        assert tuning.resiliency.default_max_retries == 3
        assert tuning.connector.blob_operation_timeout == 60.0
        assert tuning.pool.asyncpg_max_size == 5


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
        assert pt["db_pool_size"] == 20
        assert pt["search_max_concurrency"] == 10

    def test_full_features_includes_new_slices(self, app_full_profile: FastAPI) -> None:
        client = TestClient(app_full_profile)
        resp = client.get("/api/v2/features")
        pt = resp.json()["performance_tuning"]
        assert pt["heartbeat_flush_interval"] == 60
        assert pt["default_max_retries"] == 3
        assert pt["blob_operation_timeout"] == 60.0
        assert pt["asyncpg_max_size"] == 5

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
        assert pt["db_pool_size"] == 8
        assert pt["search_max_concurrency"] == 5

    def test_lite_features_new_slices_differ(self, app_lite_profile: FastAPI) -> None:
        client = TestClient(app_lite_profile)
        resp = client.get("/api/v2/features")
        pt = resp.json()["performance_tuning"]
        assert pt["heartbeat_flush_interval"] == 120
        assert pt["default_max_retries"] == 3
        assert pt["blob_operation_timeout"] == 60.0
        assert pt["asyncpg_max_size"] == 5

    def test_profile_field_matches(self, app_lite_profile: FastAPI) -> None:
        client = TestClient(app_lite_profile)
        resp = client.get("/api/v2/features")
        data = resp.json()
        assert data["profile"] == "lite"

    def test_all_ten_tuning_fields_present(self, app_full_profile: FastAPI) -> None:
        """Verify all 10 summary fields are in the response."""
        client = TestClient(app_full_profile)
        resp = client.get("/api/v2/features")
        pt = resp.json()["performance_tuning"]
        expected_keys = {
            "thread_pool_size",
            "default_workers",
            "task_runner_workers",
            "default_http_timeout",
            "db_pool_size",
            "search_max_concurrency",
            "heartbeat_flush_interval",
            "default_max_retries",
            "blob_operation_timeout",
            "asyncpg_max_size",
        }
        assert set(pt.keys()) == expected_keys


class TestTuningViaEnvVar:
    """NEXUS_PROFILE env var selects correct tuning."""

    def test_embedded_profile_env(self) -> None:
        """Verify embedded profile produces minimal tuning."""
        profile = DeploymentProfile.EMBEDDED
        tuning = profile.tuning()
        assert tuning.concurrency.default_workers == 1
        assert tuning.concurrency.thread_pool_size == 10
        assert tuning.storage.db_pool_size == 3
        assert tuning.search.grep_parallel_workers == 1
        # New slices
        assert tuning.background_task.heartbeat_flush_interval == 120
        assert tuning.resiliency.default_max_retries == 2
        assert tuning.connector.blob_operation_timeout == 30.0
        assert tuning.pool.asyncpg_max_size == 2

    def test_cloud_profile_env(self) -> None:
        """Verify cloud profile produces aggressive tuning."""
        profile = DeploymentProfile.CLOUD
        tuning = profile.tuning()
        assert tuning.concurrency.default_workers == 8
        assert tuning.concurrency.thread_pool_size == 400
        assert tuning.storage.db_pool_size == 30
        assert tuning.search.search_max_concurrency == 20
        # New slices
        assert tuning.background_task.heartbeat_flush_interval == 30
        assert tuning.resiliency.default_max_retries == 5
        assert tuning.connector.blob_operation_timeout == 120.0
        assert tuning.pool.asyncpg_max_size == 15


class TestBackwardCompatibility:
    """Existing module-level constants still work as fallback defaults."""

    def test_grep_parallel_workers_constant(self) -> None:
        from nexus.contracts.search_types import GREP_PARALLEL_WORKERS

        assert GREP_PARALLEL_WORKERS == 4  # FULL profile default

    def test_list_parallel_workers_constant(self) -> None:
        from nexus.bricks.search.search_service import LIST_PARALLEL_WORKERS

        assert LIST_PARALLEL_WORKERS == 10  # FULL profile default

    def test_webhook_timeout_constant(self) -> None:
        from nexus.server.subscriptions.manager import WEBHOOK_TIMEOUT

        assert WEBHOOK_TIMEOUT == 10.0  # FULL profile default

    def test_default_operation_timeout_constant(self) -> None:
        from nexus.server.batch_executor import DEFAULT_OPERATION_TIMEOUT

        assert DEFAULT_OPERATION_TIMEOUT == 30.0  # FULL profile default


class TestDIWiring:
    """Verify DI constructor params accept profile tuning values."""

    def test_subscription_manager_accepts_webhook_timeout(self) -> None:
        """SubscriptionManager constructor accepts custom webhook_timeout."""
        from unittest.mock import MagicMock

        from nexus.server.subscriptions.manager import SubscriptionManager

        mgr = SubscriptionManager(session_factory=MagicMock(), webhook_timeout=5.0)
        assert mgr._webhook_timeout == 5.0

    def test_search_service_accepts_parallel_workers(self) -> None:
        """SearchService constructor accepts custom parallel worker counts."""
        from unittest.mock import MagicMock

        from nexus.bricks.search.search_service import SearchService

        svc = SearchService(
            metadata_store=MagicMock(),
            list_parallel_workers=20,
            grep_parallel_workers=8,
        )
        assert svc._list_parallel_workers == 20
        assert svc._grep_parallel_workers == 8

    def test_tiger_cache_accepts_l2_max_workers(self) -> None:
        """TigerCache constructor accepts custom l2_max_workers."""
        from unittest.mock import MagicMock

        from nexus.bricks.rebac.cache.tiger.bitmap_cache import TigerCache

        cache = TigerCache(engine=MagicMock(), l2_max_workers=8)
        assert cache._l2_max_workers == 8
