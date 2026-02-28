"""Integration tests for ProfileTuning DI wiring (Issue #2071).

Verifies that different deployment profiles produce different service
configurations when wired through the DI system. These tests validate
that profile_tuning values flow correctly from ProfileTuning slices
through to service constructors.
"""

from unittest.mock import MagicMock

import pytest

from nexus.contracts.deployment_profile import DeploymentProfile
from nexus.lib.performance_tuning import ProfileTuning


class TestProfilesProduceDifferentTuning:
    """Verify that LITE and FULL profiles produce measurably different tuning."""

    @pytest.fixture
    def full_tuning(self) -> ProfileTuning:
        return DeploymentProfile.FULL.tuning()

    @pytest.fixture
    def lite_tuning(self) -> ProfileTuning:
        return DeploymentProfile.LITE.tuning()

    def test_storage_differs(self, full_tuning: ProfileTuning, lite_tuning: ProfileTuning) -> None:
        assert full_tuning.storage.db_pool_size > lite_tuning.storage.db_pool_size
        assert full_tuning.storage.db_max_overflow > lite_tuning.storage.db_max_overflow

    def test_concurrency_differs(
        self, full_tuning: ProfileTuning, lite_tuning: ProfileTuning
    ) -> None:
        assert full_tuning.concurrency.default_workers > lite_tuning.concurrency.default_workers
        assert full_tuning.concurrency.thread_pool_size > lite_tuning.concurrency.thread_pool_size

    def test_pool_differs(self, full_tuning: ProfileTuning, lite_tuning: ProfileTuning) -> None:
        assert full_tuning.pool.httpx_max_connections > lite_tuning.pool.httpx_max_connections
        assert full_tuning.pool.remote_pool_maxsize > lite_tuning.pool.remote_pool_maxsize

    def test_connector_differs(
        self, full_tuning: ProfileTuning, lite_tuning: ProfileTuning
    ) -> None:
        assert (
            full_tuning.connector.connector_max_workers
            > lite_tuning.connector.connector_max_workers
        )

    def test_background_task_heartbeat_differs(
        self, full_tuning: ProfileTuning, lite_tuning: ProfileTuning
    ) -> None:
        # FULL has shorter intervals (more aggressive) than LITE
        assert (
            full_tuning.background_task.heartbeat_flush_interval
            < lite_tuning.background_task.heartbeat_flush_interval
        )


class TestDIFlowsToConstructors:
    """Verify profile tuning values propagate through DI to service constructors."""

    def test_record_store_pool_size_from_profile(self) -> None:
        """create_record_store accepts pool_size/max_overflow from profile."""
        import inspect

        from nexus.storage.record_store import SQLAlchemyRecordStore

        sig = inspect.signature(SQLAlchemyRecordStore.__init__)
        assert "pool_size" in sig.parameters
        assert "max_overflow" in sig.parameters
        # Both should have None defaults (backward compat)
        assert sig.parameters["pool_size"].default is None
        assert sig.parameters["max_overflow"].default is None

    def test_gcs_backend_timeout_from_profile(self) -> None:
        """GCSBackend accepts operation_timeout/upload_timeout from profile."""
        import inspect

        from nexus.backends.gcs import GCSBackend

        sig = inspect.signature(GCSBackend.__init__)
        assert "operation_timeout" in sig.parameters
        assert "upload_timeout" in sig.parameters
        # Should have reasonable defaults matching FULL profile
        assert sig.parameters["operation_timeout"].default == 60.0
        assert sig.parameters["upload_timeout"].default == 300.0

    def test_subscription_manager_timeout_from_profile(self) -> None:
        """SubscriptionManager webhook_timeout matches profile."""
        from nexus.server.subscriptions.manager import (
            WEBHOOK_TIMEOUT,
            SubscriptionManager,
        )

        full_tuning = DeploymentProfile.FULL.tuning()
        assert full_tuning.network.webhook_timeout == WEBHOOK_TIMEOUT

        mgr = SubscriptionManager(
            session_factory=MagicMock(),
            webhook_timeout=full_tuning.network.webhook_timeout,
        )
        assert mgr._webhook_timeout == WEBHOOK_TIMEOUT

    def test_search_service_workers_from_profile(self) -> None:
        """SearchService parallel workers match profile tuning."""
        from nexus.services.search.search_service import SearchService

        full_tuning = DeploymentProfile.FULL.tuning()
        svc = SearchService(
            metadata_store=MagicMock(),
            list_parallel_workers=full_tuning.search.list_parallel_workers,
            grep_parallel_workers=full_tuning.search.grep_parallel_workers,
        )
        assert svc._list_parallel_workers == 10
        assert svc._grep_parallel_workers == 4

    def test_tiger_cache_workers_from_profile(self) -> None:
        """TigerCache l2_max_workers matches profile cache tuning."""
        from nexus.bricks.rebac.cache.tiger.bitmap_cache import TigerCache

        full_tuning = DeploymentProfile.FULL.tuning()
        cache = TigerCache(
            engine=MagicMock(),
            l2_max_workers=full_tuning.cache.tiger_max_workers,
        )
        assert cache._l2_max_workers == 4


class TestFeaturesInfoComputationIntegration:
    """Verify _compute_features_info integrates all 9 slices correctly."""

    def test_features_info_includes_all_new_fields(self) -> None:
        """Features info should include fields from all 9 tuning slices."""
        from fastapi import FastAPI

        from nexus.server.lifespan import _compute_features_info

        app = FastAPI()
        profile = DeploymentProfile.FULL
        app.state.deployment_profile = profile.value
        app.state.enabled_bricks = profile.default_bricks()
        app.state.profile_tuning = profile.tuning()
        app.state.deployment_mode = "standalone"

        _compute_features_info(app)

        fi = app.state.features_info
        pt = fi.performance_tuning
        assert pt is not None
        # Original 6 fields
        assert pt.thread_pool_size == 200
        assert pt.db_pool_size == 20
        # New 4 fields from new slices
        assert pt.heartbeat_flush_interval == 60
        assert pt.default_max_retries == 3
        assert pt.blob_operation_timeout == 60.0
        assert pt.asyncpg_max_size == 5

    def test_features_info_differs_by_profile(self) -> None:
        """EMBEDDED and CLOUD profiles produce different features info."""
        from fastapi import FastAPI

        from nexus.server.lifespan import _compute_features_info

        apps = {}
        for p in [DeploymentProfile.EMBEDDED, DeploymentProfile.CLOUD]:
            app = FastAPI()
            app.state.deployment_profile = p.value
            app.state.enabled_bricks = p.default_bricks()
            app.state.profile_tuning = p.tuning()
            app.state.deployment_mode = "standalone"
            _compute_features_info(app)
            apps[p.value] = app.state.features_info.performance_tuning

        emb = apps["embedded"]
        cloud = apps["cloud"]
        assert emb.thread_pool_size < cloud.thread_pool_size
        assert emb.db_pool_size < cloud.db_pool_size
        assert emb.heartbeat_flush_interval > cloud.heartbeat_flush_interval
        assert emb.default_max_retries < cloud.default_max_retries
        assert emb.blob_operation_timeout < cloud.blob_operation_timeout
        assert emb.asyncpg_max_size < cloud.asyncpg_max_size
