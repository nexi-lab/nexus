"""Tests for slim boot mode (Issue #2194).

Verifies that DeploymentProfile.SLIM provides the smallest runnable
deployment with only the storage brick, no system services, and working file ops.

Profile hierarchy: slim < embedded < lite < full <= cloud
"""

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

    from nexus.core.nexus_fs import NexusFS

# ---------------------------------------------------------------------------
# TestSlimProfileBricks — enum + brick set
# ---------------------------------------------------------------------------


class TestSlimProfileBricks:
    """SLIM profile returns only {storage} as default bricks."""

    def test_slim_enum_value(self) -> None:
        from nexus.contracts.deployment_profile import DeploymentProfile

        assert DeploymentProfile.SLIM.value == "slim"

    def test_slim_default_bricks_empty(self) -> None:
        from nexus.contracts.deployment_profile import DeploymentProfile

        bricks = DeploymentProfile.SLIM.default_bricks()
        assert bricks == frozenset()

    def test_slim_eventlog_is_disabled(self) -> None:
        from nexus.contracts.deployment_profile import DeploymentProfile

        assert DeploymentProfile.SLIM.is_brick_enabled("eventlog") is False

    def test_slim_search_is_disabled(self) -> None:
        from nexus.contracts.deployment_profile import DeploymentProfile

        assert DeploymentProfile.SLIM.is_brick_enabled("search") is False

    def test_resolve_with_no_overrides(self) -> None:
        from nexus.contracts.deployment_profile import (
            DeploymentProfile,
            resolve_enabled_bricks,
        )

        result = resolve_enabled_bricks(DeploymentProfile.SLIM)
        assert result == frozenset()

    def test_resolve_with_override_enables_extra_brick(self) -> None:
        from nexus.contracts.deployment_profile import (
            BRICK_EVENTLOG,
            DeploymentProfile,
            resolve_enabled_bricks,
        )

        result = resolve_enabled_bricks(DeploymentProfile.SLIM, overrides={"eventlog": True})
        assert result == frozenset({BRICK_EVENTLOG})


# ---------------------------------------------------------------------------
# TestSlimBootViaFactory — create_nexus_fs with record_store=None
# ---------------------------------------------------------------------------


class TestSlimBootViaFactory:
    """Factory creates bare kernel when record_store is None (SLIM path)."""

    @pytest.mark.asyncio
    async def test_create_nexus_fs_no_record_store(self, tmp_path: "Path") -> None:
        from nexus.backends.storage.path_local import PathLocalBackend
        from nexus.factory.orchestrator import create_nexus_fs
        from tests.helpers.dict_metastore import DictMetastore

        data_dir = tmp_path / "data"
        data_dir.mkdir(exist_ok=True)

        nx = create_nexus_fs(
            backend=PathLocalBackend(root_path=data_dir),
            metadata_store=DictMetastore(),
            record_store=None,
        )

        assert nx is not None
        # System services should be empty (no record store)
        # Issue #1801: _system_services deleted — check via ServiceRegistry
        assert nx.service("rebac") is None or nx.service("rebac")._rebac_manager is None
        assert nx.service("permission_enforcer") is None

    @pytest.mark.asyncio
    async def test_minimal_mode_nexus_has_router(self, tmp_path: "Path") -> None:
        from nexus.backends.storage.path_local import PathLocalBackend
        from nexus.factory.orchestrator import create_nexus_fs
        from tests.helpers.dict_metastore import DictMetastore

        data_dir = tmp_path / "data"
        data_dir.mkdir(exist_ok=True)

        nx = create_nexus_fs(
            backend=PathLocalBackend(root_path=data_dir),
            metadata_store=DictMetastore(),
            record_store=None,
        )

        assert nx.router is not None


# ---------------------------------------------------------------------------
# TestSlimFileOperations — write/read/exists/delete/list in slim mode
# ---------------------------------------------------------------------------


class TestSlimFileOperations:
    """File operations work in kernel-only mode (no system services)."""

    @pytest.fixture()
    async def minimal_nx(self, tmp_path: "Path") -> "NexusFS":
        from tests.conftest import make_test_nexus

        return await make_test_nexus(tmp_path)

    @pytest.mark.asyncio
    async def test_write_and_read(self, minimal_nx: "NexusFS") -> None:
        minimal_nx.write("/test.txt", b"hello kernel")
        data = minimal_nx.sys_read("/test.txt")
        assert data == b"hello kernel"

    @pytest.mark.asyncio
    async def test_exists_true(self, minimal_nx: "NexusFS") -> None:
        minimal_nx.write("/exists_check.txt", b"data")
        assert minimal_nx.access("/exists_check.txt") is True

    @pytest.mark.asyncio
    async def test_exists_false(self, minimal_nx: "NexusFS") -> None:
        assert minimal_nx.access("/nonexistent.txt") is False

    @pytest.mark.asyncio
    async def test_delete(self, minimal_nx: "NexusFS") -> None:
        minimal_nx.write("/to_delete.txt", b"bye")
        minimal_nx.sys_unlink("/to_delete.txt")
        assert minimal_nx.access("/to_delete.txt") is False

    @pytest.mark.asyncio
    async def test_list_directory(self, minimal_nx: "NexusFS") -> None:
        minimal_nx.write("/dir/a.txt", b"a")
        minimal_nx.write("/dir/b.txt", b"b")
        listing = minimal_nx.sys_readdir("/dir")
        paths = [item["path"] if isinstance(item, dict) else item for item in listing]
        assert "/dir/a.txt" in paths
        assert "/dir/b.txt" in paths


# ---------------------------------------------------------------------------
# TestSlimConfigValidation — NexusConfig accepts "slim"
# ---------------------------------------------------------------------------


class TestSlimConfigValidation:
    """NexusConfig validates 'slim' as a valid profile value."""

    def test_slim_profile_accepted(self) -> None:
        from nexus.config import NexusConfig

        cfg = NexusConfig(profile="slim")
        assert cfg.profile == "slim"

    def test_invalid_profile_rejected(self) -> None:
        from nexus.config import NexusConfig

        with pytest.raises(ValueError, match="profile must be one of"):
            NexusConfig(profile="nonexistent")


# ---------------------------------------------------------------------------
# TestSlimTuning — performance tuning exists and values <= EMBEDDED
# ---------------------------------------------------------------------------


class TestSlimTuning:
    """SLIM profile has tuning values that are <= EMBEDDED for resources."""

    def test_slim_tuning_exists(self) -> None:
        from nexus.contracts.deployment_profile import DeploymentProfile

        tuning = DeploymentProfile.SLIM.tuning()
        assert tuning is not None

    def test_thread_pool_lte_embedded(self) -> None:
        from nexus.contracts.deployment_profile import DeploymentProfile

        slim = DeploymentProfile.SLIM.tuning()
        embedded = DeploymentProfile.EMBEDDED.tuning()
        assert slim.concurrency.thread_pool_size <= embedded.concurrency.thread_pool_size

    def test_db_pool_lte_embedded(self) -> None:
        from nexus.contracts.deployment_profile import DeploymentProfile

        slim = DeploymentProfile.SLIM.tuning()
        embedded = DeploymentProfile.EMBEDDED.tuning()
        assert slim.storage.db_pool_size <= embedded.storage.db_pool_size

    def test_asyncpg_max_lte_embedded(self) -> None:
        from nexus.contracts.deployment_profile import DeploymentProfile

        slim = DeploymentProfile.SLIM.tuning()
        embedded = DeploymentProfile.EMBEDDED.tuning()
        assert slim.pool.asyncpg_max_size <= embedded.pool.asyncpg_max_size

    def test_default_workers_lte_embedded(self) -> None:
        from nexus.contracts.deployment_profile import DeploymentProfile

        slim = DeploymentProfile.SLIM.tuning()
        embedded = DeploymentProfile.EMBEDDED.tuning()
        assert slim.concurrency.default_workers <= embedded.concurrency.default_workers

    def test_intervals_gte_embedded(self) -> None:
        """Intervals (cleanup, heartbeat) should be >= EMBEDDED (less frequent)."""
        from nexus.contracts.deployment_profile import DeploymentProfile

        slim = DeploymentProfile.SLIM.tuning()
        embedded = DeploymentProfile.EMBEDDED.tuning()
        assert (
            slim.background_task.heartbeat_flush_interval
            >= embedded.background_task.heartbeat_flush_interval
        )
        assert (
            slim.background_task.sandbox_cleanup_interval
            >= embedded.background_task.sandbox_cleanup_interval
        )


# ---------------------------------------------------------------------------
# TestSlimHierarchy — slim < embedded < lite < full <= cloud
# ---------------------------------------------------------------------------


class TestSlimHierarchy:
    """Profile hierarchy: each tier's bricks are a superset of the previous."""

    def test_slim_subset_of_embedded(self) -> None:
        from nexus.contracts.deployment_profile import DeploymentProfile

        assert DeploymentProfile.SLIM.default_bricks() < DeploymentProfile.EMBEDDED.default_bricks()

    def test_embedded_subset_of_lite(self) -> None:
        from nexus.contracts.deployment_profile import DeploymentProfile

        assert DeploymentProfile.EMBEDDED.default_bricks() < DeploymentProfile.LITE.default_bricks()

    def test_lite_subset_of_full(self) -> None:
        from nexus.contracts.deployment_profile import DeploymentProfile

        assert DeploymentProfile.LITE.default_bricks() < DeploymentProfile.FULL.default_bricks()

    def test_full_subset_or_equal_cloud(self) -> None:
        from nexus.contracts.deployment_profile import DeploymentProfile

        assert DeploymentProfile.FULL.default_bricks() <= DeploymentProfile.CLOUD.default_bricks()

    def test_slim_is_strict_minimum(self) -> None:
        """SLIM has exactly 0 bricks (kernel only)."""
        from nexus.contracts.deployment_profile import DeploymentProfile

        assert len(DeploymentProfile.SLIM.default_bricks()) == 0


# ---------------------------------------------------------------------------
# TestSlimDeviceCapabilities — profile index
# ---------------------------------------------------------------------------


class TestSlimDeviceCapabilities:
    """Slim appears in device capability profile index."""

    def test_slim_in_profile_index(self) -> None:
        from nexus.lib.device_capabilities import _PROFILE_INDEX

        assert "slim" in _PROFILE_INDEX

    def test_slim_index_below_embedded(self) -> None:
        from nexus.lib.device_capabilities import _PROFILE_INDEX

        assert _PROFILE_INDEX["slim"] < _PROFILE_INDEX["embedded"]

    def test_slim_never_auto_suggested(self) -> None:
        """suggest_profile() never returns SLIM — it must be explicit."""
        from nexus.lib.device_capabilities import DeviceCapabilities, suggest_profile

        # Even with very low memory, suggest_profile returns EMBEDDED, not SLIM
        tiny = DeviceCapabilities(memory_mb=16, cpu_cores=1)
        suggested = suggest_profile(tiny)
        assert suggested.value != "slim"


# ---------------------------------------------------------------------------
# TestSlimIntegrationViaConnect — full connect() path with profile=slim
# ---------------------------------------------------------------------------


class TestSlimIntegrationViaConnect:
    """Integration: nexus.connect() with profile=slim boots bare kernel."""

    @pytest.mark.asyncio
    async def test_connect_slim_profile_creates_nexusfs(
        self, tmp_path: "Path", monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """connect() with profile=slim gives a functional NexusFS."""
        from nexus.backends.storage.path_local import PathLocalBackend
        from nexus.contracts.deployment_profile import DeploymentProfile, resolve_enabled_bricks
        from nexus.core.config import PermissionConfig
        from nexus.factory.orchestrator import create_nexus_fs
        from tests.helpers.dict_metastore import DictMetastore

        data_dir = tmp_path / "data"
        data_dir.mkdir(exist_ok=True)

        monkeypatch.setenv("NEXUS_PROFILE", "slim")

        profile = DeploymentProfile.SLIM
        enabled_bricks = resolve_enabled_bricks(profile)
        assert enabled_bricks == frozenset()

        nx = create_nexus_fs(
            backend=PathLocalBackend(root_path=data_dir),
            metadata_store=DictMetastore(),
            record_store=None,
            enabled_bricks=enabled_bricks,
            permissions=PermissionConfig(enforce=False),
        )

        # Services should be empty
        # Issue #1801: _system_services deleted — check via ServiceRegistry
        assert nx.service("rebac") is None or nx.service("rebac")._rebac_manager is None
        assert nx.service("permission_enforcer") is None
        # Issue #1570: audit_store accessed from container, not flat attr
        # Issue #1801: check via ServiceRegistry instead of _system_services
        assert nx.service("audit") is None

        # File operations should work
        nx.write("/hello.txt", b"slim mode")
        assert nx.sys_read("/hello.txt") == b"slim mode"
        assert nx.access("/hello.txt") is True
        nx.sys_unlink("/hello.txt")
        assert nx.access("/hello.txt") is False

    @pytest.mark.asyncio
    async def test_slim_factory_enabled_bricks_logged(
        self, tmp_path: "Path", monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Factory logs exactly 1 enabled brick for SLIM profile."""
        import logging

        data_dir = tmp_path / "data"
        data_dir.mkdir(exist_ok=True)

        monkeypatch.setenv("NEXUS_PROFILE", "slim")

        from nexus.backends.storage.path_local import PathLocalBackend
        from nexus.contracts.deployment_profile import DeploymentProfile, resolve_enabled_bricks
        from nexus.factory.orchestrator import create_nexus_fs
        from tests.helpers.dict_metastore import DictMetastore

        enabled_bricks = resolve_enabled_bricks(DeploymentProfile.SLIM)

        with caplog.at_level(logging.INFO, logger="nexus.factory.orchestrator"):
            # Using record_store triggers create_nexus_services which logs bricks
            # With record_store=None, factory path skips services entirely
            nx = create_nexus_fs(
                backend=PathLocalBackend(root_path=data_dir),
                metadata_store=DictMetastore(),
                record_store=None,
                enabled_bricks=enabled_bricks,
            )

        assert nx is not None

    @pytest.mark.asyncio
    async def test_slim_profile_dispatch_has_no_observers(self, tmp_path: "Path") -> None:
        """SLIM mode has only the late-binding EventBusObserver (no record store to sync)."""
        from nexus.backends.storage.path_local import PathLocalBackend
        from nexus.contracts.deployment_profile import DeploymentProfile, resolve_enabled_bricks
        from nexus.factory.orchestrator import create_nexus_fs
        from tests.helpers.dict_metastore import DictMetastore

        data_dir = tmp_path / "data"
        data_dir.mkdir(exist_ok=True)

        nx = create_nexus_fs(
            backend=PathLocalBackend(root_path=data_dir),
            metadata_store=DictMetastore(),
            record_store=None,
            enabled_bricks=resolve_enabled_bricks(DeploymentProfile.SLIM),
        )

        # register_observe is now a no-op (Python observers deleted).
        # All observers (FileWatcher, StreamEventObservers) are Rust kernel-internal.
        # Service-registered observer count is always 0.
        assert nx.observer_count == 0

    @pytest.mark.asyncio
    async def test_slim_profile_no_workflow_engine(self, tmp_path: "Path") -> None:
        """SLIM mode has no workflow engine."""
        from nexus.backends.storage.path_local import PathLocalBackend
        from nexus.contracts.deployment_profile import DeploymentProfile, resolve_enabled_bricks
        from nexus.factory.orchestrator import create_nexus_fs
        from tests.helpers.dict_metastore import DictMetastore

        data_dir = tmp_path / "data"
        data_dir.mkdir(exist_ok=True)

        nx = create_nexus_fs(
            backend=PathLocalBackend(root_path=data_dir),
            metadata_store=DictMetastore(),
            record_store=None,
            enabled_bricks=resolve_enabled_bricks(DeploymentProfile.SLIM),
        )

        # workflow_engine is no longer a NexusFS attribute; it lives in
        # BrickDict / server state. getattr mirrors the CLI access pattern.
        assert getattr(nx, "workflow_engine", None) is None


# ---------------------------------------------------------------------------
# TestSlimPerformanceCharacteristics — no perf regressions
# ---------------------------------------------------------------------------


class TestSlimPerformanceCharacteristics:
    """Verify slim tuning values are the most conservative across all profiles."""

    def test_slim_has_smallest_thread_pool(self) -> None:
        from nexus.contracts.deployment_profile import DeploymentProfile

        slim_tp = DeploymentProfile.SLIM.tuning().concurrency.thread_pool_size
        for profile in DeploymentProfile:
            other_tp = profile.tuning().concurrency.thread_pool_size
            assert slim_tp <= other_tp, f"SLIM thread_pool ({slim_tp}) > {profile} ({other_tp})"

    def test_slim_has_smallest_db_pool(self) -> None:
        from nexus.contracts.deployment_profile import DeploymentProfile

        slim_dp = DeploymentProfile.SLIM.tuning().storage.db_pool_size
        for profile in DeploymentProfile:
            other_dp = profile.tuning().storage.db_pool_size
            assert slim_dp <= other_dp, f"SLIM db_pool ({slim_dp}) > {profile} ({other_dp})"

    def test_slim_has_fewest_workers(self) -> None:
        from nexus.contracts.deployment_profile import DeploymentProfile

        slim_w = DeploymentProfile.SLIM.tuning().concurrency.default_workers
        for profile in DeploymentProfile:
            other_w = profile.tuning().concurrency.default_workers
            assert slim_w <= other_w, f"SLIM workers ({slim_w}) > {profile} ({other_w})"

    def test_slim_has_longest_cleanup_intervals(self) -> None:
        """SLIM should have the longest (least frequent) cleanup intervals."""
        from nexus.contracts.deployment_profile import DeploymentProfile

        slim_hb = DeploymentProfile.SLIM.tuning().background_task.heartbeat_flush_interval
        for profile in DeploymentProfile:
            other_hb = profile.tuning().background_task.heartbeat_flush_interval
            assert slim_hb >= other_hb, (
                f"SLIM heartbeat interval ({slim_hb}) < {profile} ({other_hb})"
            )
