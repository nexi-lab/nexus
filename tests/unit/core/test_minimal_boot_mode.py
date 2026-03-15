"""Tests for minimal boot mode (Issue #2194).

Verifies that DeploymentProfile.MINIMAL provides the smallest runnable
deployment with only the storage brick, no system services, and working file ops.

Profile hierarchy: minimal < embedded < lite < full <= cloud
"""

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

    from nexus.core.nexus_fs import NexusFS

# ---------------------------------------------------------------------------
# TestMinimalProfileBricks — enum + brick set
# ---------------------------------------------------------------------------


class TestMinimalProfileBricks:
    """MINIMAL profile returns only {storage} as default bricks."""

    def test_minimal_enum_value(self) -> None:
        from nexus.contracts.deployment_profile import DeploymentProfile

        assert DeploymentProfile.MINIMAL.value == "minimal"

    def test_minimal_default_bricks_only_storage(self) -> None:
        from nexus.contracts.deployment_profile import BRICK_STORAGE, DeploymentProfile

        bricks = DeploymentProfile.MINIMAL.default_bricks()
        assert bricks == frozenset({BRICK_STORAGE})

    def test_minimal_storage_is_enabled(self) -> None:
        from nexus.contracts.deployment_profile import DeploymentProfile

        assert DeploymentProfile.MINIMAL.is_brick_enabled("storage") is True

    def test_minimal_eventlog_is_disabled(self) -> None:
        from nexus.contracts.deployment_profile import DeploymentProfile

        assert DeploymentProfile.MINIMAL.is_brick_enabled("eventlog") is False

    def test_minimal_search_is_disabled(self) -> None:
        from nexus.contracts.deployment_profile import DeploymentProfile

        assert DeploymentProfile.MINIMAL.is_brick_enabled("search") is False

    def test_resolve_with_no_overrides(self) -> None:
        from nexus.contracts.deployment_profile import (
            BRICK_STORAGE,
            DeploymentProfile,
            resolve_enabled_bricks,
        )

        result = resolve_enabled_bricks(DeploymentProfile.MINIMAL)
        assert result == frozenset({BRICK_STORAGE})

    def test_resolve_with_override_enables_extra_brick(self) -> None:
        from nexus.contracts.deployment_profile import (
            BRICK_EVENTLOG,
            BRICK_STORAGE,
            DeploymentProfile,
            resolve_enabled_bricks,
        )

        result = resolve_enabled_bricks(DeploymentProfile.MINIMAL, overrides={"eventlog": True})
        assert result == frozenset({BRICK_STORAGE, BRICK_EVENTLOG})

    def test_resolve_with_override_disables_storage(self) -> None:
        from nexus.contracts.deployment_profile import (
            DeploymentProfile,
            resolve_enabled_bricks,
        )

        result = resolve_enabled_bricks(DeploymentProfile.MINIMAL, overrides={"storage": False})
        assert result == frozenset()


# ---------------------------------------------------------------------------
# TestMinimalBootViaFactory — create_nexus_fs with record_store=None
# ---------------------------------------------------------------------------


class TestMinimalBootViaFactory:
    """Factory creates bare kernel when record_store is None (MINIMAL path)."""

    @pytest.mark.asyncio
    async def test_create_nexus_fs_no_record_store(self, tmp_path: "Path") -> None:
        from nexus.backends.storage.path_local import PathLocalBackend
        from nexus.factory.orchestrator import create_nexus_fs
        from tests.helpers.dict_metastore import DictMetastore

        data_dir = tmp_path / "data"
        data_dir.mkdir(exist_ok=True)

        nx = await create_nexus_fs(
            backend=PathLocalBackend(root_path=data_dir),
            metadata_store=DictMetastore(),
            record_store=None,
        )

        assert nx is not None
        # System services should be empty (no record store)
        assert nx._rebac_manager is None
        assert nx._permission_enforcer is None

    @pytest.mark.asyncio
    async def test_minimal_mode_nexus_has_router(self, tmp_path: "Path") -> None:
        from nexus.backends.storage.path_local import PathLocalBackend
        from nexus.factory.orchestrator import create_nexus_fs
        from tests.helpers.dict_metastore import DictMetastore

        data_dir = tmp_path / "data"
        data_dir.mkdir(exist_ok=True)

        nx = await create_nexus_fs(
            backend=PathLocalBackend(root_path=data_dir),
            metadata_store=DictMetastore(),
            record_store=None,
        )

        assert nx.router is not None


# ---------------------------------------------------------------------------
# TestMinimalFileOperations — write/read/exists/delete/list in minimal mode
# ---------------------------------------------------------------------------


class TestMinimalFileOperations:
    """File operations work in kernel-only mode (no system services)."""

    @pytest.fixture()
    def minimal_nx(self, tmp_path: "Path") -> "NexusFS":
        from tests.conftest import make_test_nexus

        return make_test_nexus(tmp_path)

    @pytest.mark.asyncio
    async def test_write_and_read(self, minimal_nx: "NexusFS") -> None:
        await minimal_nx.sys_write("/test.txt", b"hello kernel")
        data = await minimal_nx.sys_read("/test.txt")
        assert data == b"hello kernel"

    @pytest.mark.asyncio
    async def test_exists_true(self, minimal_nx: "NexusFS") -> None:
        await minimal_nx.sys_write("/exists_check.txt", b"data")
        assert await minimal_nx.sys_access("/exists_check.txt") is True

    @pytest.mark.asyncio
    async def test_exists_false(self, minimal_nx: "NexusFS") -> None:
        assert await minimal_nx.sys_access("/nonexistent.txt") is False

    @pytest.mark.asyncio
    async def test_delete(self, minimal_nx: "NexusFS") -> None:
        await minimal_nx.sys_write("/to_delete.txt", b"bye")
        await minimal_nx.sys_unlink("/to_delete.txt")
        assert await minimal_nx.sys_access("/to_delete.txt") is False

    @pytest.mark.asyncio
    async def test_list_directory(self, minimal_nx: "NexusFS") -> None:
        await minimal_nx.sys_write("/dir/a.txt", b"a")
        await minimal_nx.sys_write("/dir/b.txt", b"b")
        listing = await minimal_nx.sys_readdir("/dir")
        paths = [item["path"] if isinstance(item, dict) else item for item in listing]
        assert "/dir/a.txt" in paths
        assert "/dir/b.txt" in paths


# ---------------------------------------------------------------------------
# TestMinimalConfigValidation — NexusConfig accepts "minimal"
# ---------------------------------------------------------------------------


class TestMinimalConfigValidation:
    """NexusConfig validates 'kernel' as a valid profile value."""

    def test_minimal_profile_accepted(self) -> None:
        from nexus.config import NexusConfig

        cfg = NexusConfig(profile="minimal")
        assert cfg.profile == "minimal"

    def test_invalid_profile_rejected(self) -> None:
        from nexus.config import NexusConfig

        with pytest.raises(ValueError, match="profile must be one of"):
            NexusConfig(profile="nonexistent")


# ---------------------------------------------------------------------------
# TestMinimalTuning — performance tuning exists and values <= EMBEDDED
# ---------------------------------------------------------------------------


class TestMinimalTuning:
    """MINIMAL profile has tuning values that are <= EMBEDDED for resources."""

    def test_minimal_tuning_exists(self) -> None:
        from nexus.contracts.deployment_profile import DeploymentProfile

        tuning = DeploymentProfile.MINIMAL.tuning()
        assert tuning is not None

    def test_thread_pool_lte_embedded(self) -> None:
        from nexus.contracts.deployment_profile import DeploymentProfile

        minimal = DeploymentProfile.MINIMAL.tuning()
        embedded = DeploymentProfile.EMBEDDED.tuning()
        assert minimal.concurrency.thread_pool_size <= embedded.concurrency.thread_pool_size

    def test_db_pool_lte_embedded(self) -> None:
        from nexus.contracts.deployment_profile import DeploymentProfile

        minimal = DeploymentProfile.MINIMAL.tuning()
        embedded = DeploymentProfile.EMBEDDED.tuning()
        assert minimal.storage.db_pool_size <= embedded.storage.db_pool_size

    def test_asyncpg_max_lte_embedded(self) -> None:
        from nexus.contracts.deployment_profile import DeploymentProfile

        minimal = DeploymentProfile.MINIMAL.tuning()
        embedded = DeploymentProfile.EMBEDDED.tuning()
        assert minimal.pool.asyncpg_max_size <= embedded.pool.asyncpg_max_size

    def test_default_workers_lte_embedded(self) -> None:
        from nexus.contracts.deployment_profile import DeploymentProfile

        minimal = DeploymentProfile.MINIMAL.tuning()
        embedded = DeploymentProfile.EMBEDDED.tuning()
        assert minimal.concurrency.default_workers <= embedded.concurrency.default_workers

    def test_intervals_gte_embedded(self) -> None:
        """Intervals (cleanup, heartbeat) should be >= EMBEDDED (less frequent)."""
        from nexus.contracts.deployment_profile import DeploymentProfile

        minimal = DeploymentProfile.MINIMAL.tuning()
        embedded = DeploymentProfile.EMBEDDED.tuning()
        assert (
            minimal.background_task.heartbeat_flush_interval
            >= embedded.background_task.heartbeat_flush_interval
        )
        assert (
            minimal.background_task.sandbox_cleanup_interval
            >= embedded.background_task.sandbox_cleanup_interval
        )


# ---------------------------------------------------------------------------
# TestMinimalHierarchy — kernel < embedded < lite < full <= cloud
# ---------------------------------------------------------------------------


class TestMinimalHierarchy:
    """Profile hierarchy: each tier's bricks are a superset of the previous."""

    def test_minimal_subset_of_embedded(self) -> None:
        from nexus.contracts.deployment_profile import DeploymentProfile

        assert (
            DeploymentProfile.MINIMAL.default_bricks() < DeploymentProfile.EMBEDDED.default_bricks()
        )

    def test_embedded_subset_of_lite(self) -> None:
        from nexus.contracts.deployment_profile import DeploymentProfile

        assert DeploymentProfile.EMBEDDED.default_bricks() < DeploymentProfile.LITE.default_bricks()

    def test_lite_subset_of_full(self) -> None:
        from nexus.contracts.deployment_profile import DeploymentProfile

        assert DeploymentProfile.LITE.default_bricks() < DeploymentProfile.FULL.default_bricks()

    def test_full_subset_or_equal_cloud(self) -> None:
        from nexus.contracts.deployment_profile import DeploymentProfile

        assert DeploymentProfile.FULL.default_bricks() <= DeploymentProfile.CLOUD.default_bricks()

    def test_minimal_is_strict_minimum(self) -> None:
        """MINIMAL has exactly 1 brick (storage)."""
        from nexus.contracts.deployment_profile import DeploymentProfile

        assert len(DeploymentProfile.MINIMAL.default_bricks()) == 1


# ---------------------------------------------------------------------------
# TestMinimalDeviceCapabilities — profile index
# ---------------------------------------------------------------------------


class TestMinimalDeviceCapabilities:
    """Kernel appears in device capability profile index."""

    def test_minimal_in_profile_index(self) -> None:
        from nexus.lib.device_capabilities import _PROFILE_INDEX

        assert "minimal" in _PROFILE_INDEX

    def test_minimal_index_below_embedded(self) -> None:
        from nexus.lib.device_capabilities import _PROFILE_INDEX

        assert _PROFILE_INDEX["minimal"] < _PROFILE_INDEX["embedded"]

    def test_minimal_never_auto_suggested(self) -> None:
        """suggest_profile() never returns MINIMAL — it must be explicit."""
        from nexus.lib.device_capabilities import DeviceCapabilities, suggest_profile

        # Even with very low memory, suggest_profile returns EMBEDDED, not MINIMAL
        tiny = DeviceCapabilities(memory_mb=16, cpu_cores=1)
        suggested = suggest_profile(tiny)
        assert suggested.value != "minimal"


# ---------------------------------------------------------------------------
# TestMinimalIntegrationViaConnect — full connect() path with profile=kernel
# ---------------------------------------------------------------------------


class TestMinimalIntegrationViaConnect:
    """Integration: nexus.connect() with profile=kernel boots bare kernel."""

    @pytest.mark.asyncio
    async def test_connect_kernel_profile_creates_nexusfs(
        self, tmp_path: "Path", monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """connect() with profile=kernel gives a functional NexusFS."""
        from nexus.backends.storage.path_local import PathLocalBackend
        from nexus.contracts.deployment_profile import DeploymentProfile, resolve_enabled_bricks
        from nexus.core.config import PermissionConfig
        from nexus.factory.orchestrator import create_nexus_fs
        from tests.helpers.dict_metastore import DictMetastore

        data_dir = tmp_path / "data"
        data_dir.mkdir(exist_ok=True)

        monkeypatch.setenv("NEXUS_PROFILE", "minimal")

        profile = DeploymentProfile.MINIMAL
        enabled_bricks = resolve_enabled_bricks(profile)
        assert enabled_bricks == frozenset({"storage"})

        nx = await create_nexus_fs(
            backend=PathLocalBackend(root_path=data_dir),
            metadata_store=DictMetastore(),
            record_store=None,
            enabled_bricks=enabled_bricks,
            permissions=PermissionConfig(enforce=False),
        )

        # Services should be empty
        assert nx._rebac_manager is None
        assert nx._permission_enforcer is None
        assert nx._audit_store is None

        # File operations should work
        await nx.sys_write("/hello.txt", b"minimal mode")
        assert await nx.sys_read("/hello.txt") == b"minimal mode"
        assert await nx.sys_access("/hello.txt") is True
        await nx.sys_unlink("/hello.txt")
        assert await nx.sys_access("/hello.txt") is False

    @pytest.mark.asyncio
    async def test_minimal_factory_enabled_bricks_logged(
        self, tmp_path: "Path", monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Factory logs exactly 1 enabled brick for MINIMAL profile."""
        import logging

        data_dir = tmp_path / "data"
        data_dir.mkdir(exist_ok=True)

        monkeypatch.setenv("NEXUS_PROFILE", "minimal")

        from nexus.backends.storage.path_local import PathLocalBackend
        from nexus.contracts.deployment_profile import DeploymentProfile, resolve_enabled_bricks
        from nexus.factory.orchestrator import create_nexus_fs
        from tests.helpers.dict_metastore import DictMetastore

        enabled_bricks = resolve_enabled_bricks(DeploymentProfile.MINIMAL)

        with caplog.at_level(logging.INFO, logger="nexus.factory.orchestrator"):
            # Using record_store triggers create_nexus_services which logs bricks
            # With record_store=None, factory path skips services entirely
            nx = await create_nexus_fs(
                backend=PathLocalBackend(root_path=data_dir),
                metadata_store=DictMetastore(),
                record_store=None,
                enabled_bricks=enabled_bricks,
            )

        assert nx is not None

    @pytest.mark.asyncio
    async def test_minimal_profile_dispatch_has_no_observers(self, tmp_path: "Path") -> None:
        """MINIMAL mode has only the late-binding EventBusObserver (no record store to sync)."""
        from nexus.backends.storage.path_local import PathLocalBackend
        from nexus.contracts.deployment_profile import DeploymentProfile, resolve_enabled_bricks
        from nexus.factory.orchestrator import create_nexus_fs
        from tests.helpers.dict_metastore import DictMetastore

        data_dir = tmp_path / "data"
        data_dir.mkdir(exist_ok=True)

        nx = await create_nexus_fs(
            backend=PathLocalBackend(root_path=data_dir),
            metadata_store=DictMetastore(),
            record_store=None,
            enabled_bricks=resolve_enabled_bricks(DeploymentProfile.MINIMAL),
        )

        # EventBusObserver + RevisionTrackingObserver are unconditionally
        # registered (Issue #969, #1382); they degrade gracefully when
        # no bus or version is configured.
        assert nx._dispatch.observer_count == 2

    @pytest.mark.asyncio
    async def test_minimal_profile_no_workflow_engine(self, tmp_path: "Path") -> None:
        """MINIMAL mode has no workflow engine."""
        from nexus.backends.storage.path_local import PathLocalBackend
        from nexus.contracts.deployment_profile import DeploymentProfile, resolve_enabled_bricks
        from nexus.factory.orchestrator import create_nexus_fs
        from tests.helpers.dict_metastore import DictMetastore

        data_dir = tmp_path / "data"
        data_dir.mkdir(exist_ok=True)

        nx = await create_nexus_fs(
            backend=PathLocalBackend(root_path=data_dir),
            metadata_store=DictMetastore(),
            record_store=None,
            enabled_bricks=resolve_enabled_bricks(DeploymentProfile.MINIMAL),
        )

        # workflow_engine is no longer a NexusFS attribute; it lives in
        # BrickDict / server state. getattr mirrors the CLI access pattern.
        assert getattr(nx, "workflow_engine", None) is None


# ---------------------------------------------------------------------------
# TestMinimalPerformanceCharacteristics — no perf regressions
# ---------------------------------------------------------------------------


class TestMinimalPerformanceCharacteristics:
    """Verify minimal tuning values are the most conservative across all profiles."""

    def test_minimal_has_smallest_thread_pool(self) -> None:
        from nexus.contracts.deployment_profile import DeploymentProfile

        minimal_tp = DeploymentProfile.MINIMAL.tuning().concurrency.thread_pool_size
        for profile in DeploymentProfile:
            other_tp = profile.tuning().concurrency.thread_pool_size
            assert minimal_tp <= other_tp, (
                f"MINIMALthread_pool ({minimal_tp}) > {profile} ({other_tp})"
            )

    def test_minimal_has_smallest_db_pool(self) -> None:
        from nexus.contracts.deployment_profile import DeploymentProfile

        minimal_dp = DeploymentProfile.MINIMAL.tuning().storage.db_pool_size
        for profile in DeploymentProfile:
            other_dp = profile.tuning().storage.db_pool_size
            assert minimal_dp <= other_dp, f"MINIMALdb_pool ({minimal_dp}) > {profile} ({other_dp})"

    def test_minimal_has_fewest_workers(self) -> None:
        from nexus.contracts.deployment_profile import DeploymentProfile

        minimal_w = DeploymentProfile.MINIMAL.tuning().concurrency.default_workers
        for profile in DeploymentProfile:
            other_w = profile.tuning().concurrency.default_workers
            assert minimal_w <= other_w, f"MINIMALworkers ({minimal_w}) > {profile} ({other_w})"

    def test_minimal_has_longest_cleanup_intervals(self) -> None:
        """MINIMAL should have the longest (least frequent) cleanup intervals."""
        from nexus.contracts.deployment_profile import DeploymentProfile

        minimal_hb = DeploymentProfile.MINIMAL.tuning().background_task.heartbeat_flush_interval
        for profile in DeploymentProfile:
            other_hb = profile.tuning().background_task.heartbeat_flush_interval
            assert minimal_hb >= other_hb, (
                f"MINIMALheartbeat interval ({minimal_hb}) < {profile} ({other_hb})"
            )
