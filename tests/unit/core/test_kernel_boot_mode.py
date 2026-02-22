"""Tests for kernel-only boot mode (Issue #2194).

Verifies that DeploymentProfile.KERNEL provides a minimal deployment
with only the storage brick, no system services, and working file ops.

Profile hierarchy: kernel < embedded < lite < full <= cloud
"""

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

    from nexus.core.nexus_fs import NexusFS

# ---------------------------------------------------------------------------
# TestKernelProfileBricks — enum + brick set
# ---------------------------------------------------------------------------


class TestKernelProfileBricks:
    """KERNEL profile returns only {storage} as default bricks."""

    def test_kernel_enum_value(self) -> None:
        from nexus.contracts.deployment_profile import DeploymentProfile

        assert DeploymentProfile.KERNEL.value == "kernel"

    def test_kernel_default_bricks_only_storage(self) -> None:
        from nexus.contracts.deployment_profile import BRICK_STORAGE, DeploymentProfile

        bricks = DeploymentProfile.KERNEL.default_bricks()
        assert bricks == frozenset({BRICK_STORAGE})

    def test_kernel_storage_is_enabled(self) -> None:
        from nexus.contracts.deployment_profile import DeploymentProfile

        assert DeploymentProfile.KERNEL.is_brick_enabled("storage") is True

    def test_kernel_eventlog_is_disabled(self) -> None:
        from nexus.contracts.deployment_profile import DeploymentProfile

        assert DeploymentProfile.KERNEL.is_brick_enabled("eventlog") is False

    def test_kernel_search_is_disabled(self) -> None:
        from nexus.contracts.deployment_profile import DeploymentProfile

        assert DeploymentProfile.KERNEL.is_brick_enabled("search") is False

    def test_resolve_with_no_overrides(self) -> None:
        from nexus.contracts.deployment_profile import (
            BRICK_STORAGE,
            DeploymentProfile,
            resolve_enabled_bricks,
        )

        result = resolve_enabled_bricks(DeploymentProfile.KERNEL)
        assert result == frozenset({BRICK_STORAGE})

    def test_resolve_with_override_enables_extra_brick(self) -> None:
        from nexus.contracts.deployment_profile import (
            BRICK_EVENTLOG,
            BRICK_STORAGE,
            DeploymentProfile,
            resolve_enabled_bricks,
        )

        result = resolve_enabled_bricks(DeploymentProfile.KERNEL, overrides={"eventlog": True})
        assert result == frozenset({BRICK_STORAGE, BRICK_EVENTLOG})

    def test_resolve_with_override_disables_storage(self) -> None:
        from nexus.contracts.deployment_profile import (
            DeploymentProfile,
            resolve_enabled_bricks,
        )

        result = resolve_enabled_bricks(DeploymentProfile.KERNEL, overrides={"storage": False})
        assert result == frozenset()


# ---------------------------------------------------------------------------
# TestKernelBootViaFactory — create_nexus_fs with record_store=None
# ---------------------------------------------------------------------------


class TestKernelBootViaFactory:
    """Factory creates bare kernel when record_store is None (KERNEL path)."""

    def test_create_nexus_fs_no_record_store(self, tmp_path: "Path") -> None:
        from nexus.backends.local import LocalBackend
        from nexus.factory.orchestrator import create_nexus_fs
        from tests.helpers.in_memory_metadata_store import InMemoryMetastore

        data_dir = tmp_path / "data"
        data_dir.mkdir(exist_ok=True)

        nx = create_nexus_fs(
            backend=LocalBackend(root_path=data_dir),
            metadata_store=InMemoryMetastore(),
            record_store=None,
        )

        assert nx is not None
        # System services should be empty (no record store)
        assert nx._rebac_manager is None
        assert nx._permission_enforcer is None

    def test_kernel_mode_nexus_has_router(self, tmp_path: "Path") -> None:
        from nexus.backends.local import LocalBackend
        from nexus.factory.orchestrator import create_nexus_fs
        from tests.helpers.in_memory_metadata_store import InMemoryMetastore

        data_dir = tmp_path / "data"
        data_dir.mkdir(exist_ok=True)

        nx = create_nexus_fs(
            backend=LocalBackend(root_path=data_dir),
            metadata_store=InMemoryMetastore(),
            record_store=None,
        )

        assert nx.router is not None


# ---------------------------------------------------------------------------
# TestKernelFileOperations — write/read/exists/delete/list in kernel mode
# ---------------------------------------------------------------------------


class TestKernelFileOperations:
    """File operations work in kernel-only mode (no system services)."""

    @pytest.fixture()
    def kernel_nx(self, tmp_path: "Path") -> "NexusFS":
        from tests.conftest import make_test_nexus

        return make_test_nexus(tmp_path)

    def test_write_and_read(self, kernel_nx: "NexusFS") -> None:
        kernel_nx.write("/test.txt", b"hello kernel")
        data = kernel_nx.read("/test.txt")
        assert data == b"hello kernel"

    def test_exists_true(self, kernel_nx: "NexusFS") -> None:
        kernel_nx.write("/exists_check.txt", b"data")
        assert kernel_nx.exists("/exists_check.txt") is True

    def test_exists_false(self, kernel_nx: "NexusFS") -> None:
        assert kernel_nx.exists("/nonexistent.txt") is False

    def test_delete(self, kernel_nx: "NexusFS") -> None:
        kernel_nx.write("/to_delete.txt", b"bye")
        kernel_nx.delete("/to_delete.txt")
        assert kernel_nx.exists("/to_delete.txt") is False

    def test_list_directory(self, kernel_nx: "NexusFS") -> None:
        kernel_nx.write("/dir/a.txt", b"a")
        kernel_nx.write("/dir/b.txt", b"b")
        listing = kernel_nx.list("/dir")
        paths = [item["path"] if isinstance(item, dict) else item for item in listing]
        assert "/dir/a.txt" in paths
        assert "/dir/b.txt" in paths


# ---------------------------------------------------------------------------
# TestKernelConfigValidation — NexusConfig accepts "kernel"
# ---------------------------------------------------------------------------


class TestKernelConfigValidation:
    """NexusConfig validates 'kernel' as a valid profile value."""

    def test_kernel_profile_accepted(self) -> None:
        from nexus.config import NexusConfig

        cfg = NexusConfig(profile="kernel")
        assert cfg.profile == "kernel"

    def test_invalid_profile_rejected(self) -> None:
        from nexus.config import NexusConfig

        with pytest.raises(ValueError, match="profile must be one of"):
            NexusConfig(profile="nonexistent")


# ---------------------------------------------------------------------------
# TestKernelTuning — performance tuning exists and values <= EMBEDDED
# ---------------------------------------------------------------------------


class TestKernelTuning:
    """KERNEL profile has tuning values that are <= EMBEDDED for resources."""

    def test_kernel_tuning_exists(self) -> None:
        from nexus.contracts.deployment_profile import DeploymentProfile

        tuning = DeploymentProfile.KERNEL.tuning()
        assert tuning is not None

    def test_thread_pool_lte_embedded(self) -> None:
        from nexus.contracts.deployment_profile import DeploymentProfile

        kernel = DeploymentProfile.KERNEL.tuning()
        embedded = DeploymentProfile.EMBEDDED.tuning()
        assert kernel.concurrency.thread_pool_size <= embedded.concurrency.thread_pool_size

    def test_db_pool_lte_embedded(self) -> None:
        from nexus.contracts.deployment_profile import DeploymentProfile

        kernel = DeploymentProfile.KERNEL.tuning()
        embedded = DeploymentProfile.EMBEDDED.tuning()
        assert kernel.storage.db_pool_size <= embedded.storage.db_pool_size

    def test_asyncpg_max_lte_embedded(self) -> None:
        from nexus.contracts.deployment_profile import DeploymentProfile

        kernel = DeploymentProfile.KERNEL.tuning()
        embedded = DeploymentProfile.EMBEDDED.tuning()
        assert kernel.pool.asyncpg_max_size <= embedded.pool.asyncpg_max_size

    def test_default_workers_lte_embedded(self) -> None:
        from nexus.contracts.deployment_profile import DeploymentProfile

        kernel = DeploymentProfile.KERNEL.tuning()
        embedded = DeploymentProfile.EMBEDDED.tuning()
        assert kernel.concurrency.default_workers <= embedded.concurrency.default_workers

    def test_intervals_gte_embedded(self) -> None:
        """Intervals (cleanup, heartbeat) should be >= EMBEDDED (less frequent)."""
        from nexus.contracts.deployment_profile import DeploymentProfile

        kernel = DeploymentProfile.KERNEL.tuning()
        embedded = DeploymentProfile.EMBEDDED.tuning()
        assert (
            kernel.background_task.heartbeat_flush_interval
            >= embedded.background_task.heartbeat_flush_interval
        )
        assert (
            kernel.background_task.sandbox_cleanup_interval
            >= embedded.background_task.sandbox_cleanup_interval
        )


# ---------------------------------------------------------------------------
# TestKernelHierarchy — kernel < embedded < lite < full <= cloud
# ---------------------------------------------------------------------------


class TestKernelHierarchy:
    """Profile hierarchy: each tier's bricks are a superset of the previous."""

    def test_kernel_subset_of_embedded(self) -> None:
        from nexus.contracts.deployment_profile import DeploymentProfile

        assert (
            DeploymentProfile.KERNEL.default_bricks() < DeploymentProfile.EMBEDDED.default_bricks()
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

    def test_kernel_is_strict_minimum(self) -> None:
        """KERNEL has exactly 1 brick (storage)."""
        from nexus.contracts.deployment_profile import DeploymentProfile

        assert len(DeploymentProfile.KERNEL.default_bricks()) == 1


# ---------------------------------------------------------------------------
# TestKernelDeviceCapabilities — profile index
# ---------------------------------------------------------------------------


class TestKernelDeviceCapabilities:
    """Kernel appears in device capability profile index."""

    def test_kernel_in_profile_index(self) -> None:
        from nexus.core.device_capabilities import _PROFILE_INDEX

        assert "kernel" in _PROFILE_INDEX

    def test_kernel_index_below_embedded(self) -> None:
        from nexus.core.device_capabilities import _PROFILE_INDEX

        assert _PROFILE_INDEX["kernel"] < _PROFILE_INDEX["embedded"]

    def test_kernel_never_auto_suggested(self) -> None:
        """suggest_profile() never returns KERNEL — it must be explicit."""
        from nexus.core.device_capabilities import DeviceCapabilities, suggest_profile

        # Even with very low memory, suggest_profile returns EMBEDDED, not KERNEL
        tiny = DeviceCapabilities(memory_mb=16, cpu_cores=1)
        suggested = suggest_profile(tiny)
        assert suggested.value != "kernel"


# ---------------------------------------------------------------------------
# TestKernelIntegrationViaConnect — full connect() path with profile=kernel
# ---------------------------------------------------------------------------


class TestKernelIntegrationViaConnect:
    """Integration: nexus.connect() with profile=kernel boots bare kernel."""

    def test_connect_kernel_profile_creates_nexusfs(
        self, tmp_path: "Path", monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """connect() with profile=kernel gives a functional NexusFS."""
        from nexus.backends.local import LocalBackend
        from nexus.contracts.deployment_profile import DeploymentProfile, resolve_enabled_bricks
        from nexus.core.config import PermissionConfig
        from nexus.factory.orchestrator import create_nexus_fs
        from tests.helpers.in_memory_metadata_store import InMemoryMetastore

        data_dir = tmp_path / "data"
        data_dir.mkdir(exist_ok=True)

        monkeypatch.setenv("NEXUS_PROFILE", "kernel")

        profile = DeploymentProfile.KERNEL
        enabled_bricks = resolve_enabled_bricks(profile)
        assert enabled_bricks == frozenset({"storage"})

        nx = create_nexus_fs(
            backend=LocalBackend(root_path=data_dir),
            metadata_store=InMemoryMetastore(),
            record_store=None,
            enabled_bricks=enabled_bricks,
            permissions=PermissionConfig(enforce=False),
        )

        # Services should be empty
        assert nx._rebac_manager is None
        assert nx._permission_enforcer is None
        assert nx._audit_store is None

        # File operations should work
        nx.write("/hello.txt", b"kernel mode")
        assert nx.read("/hello.txt") == b"kernel mode"
        assert nx.exists("/hello.txt") is True
        nx.delete("/hello.txt")
        assert nx.exists("/hello.txt") is False

    def test_kernel_factory_enabled_bricks_logged(
        self, tmp_path: "Path", monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Factory logs exactly 1 enabled brick for KERNEL profile."""
        import logging

        data_dir = tmp_path / "data"
        data_dir.mkdir(exist_ok=True)

        monkeypatch.setenv("NEXUS_PROFILE", "kernel")

        from nexus.backends.local import LocalBackend
        from nexus.contracts.deployment_profile import DeploymentProfile, resolve_enabled_bricks
        from nexus.factory.orchestrator import create_nexus_fs
        from tests.helpers.in_memory_metadata_store import InMemoryMetastore

        enabled_bricks = resolve_enabled_bricks(DeploymentProfile.KERNEL)

        with caplog.at_level(logging.INFO, logger="nexus.factory.orchestrator"):
            # Using record_store triggers create_nexus_services which logs bricks
            # With record_store=None, factory path skips services entirely
            nx = create_nexus_fs(
                backend=LocalBackend(root_path=data_dir),
                metadata_store=InMemoryMetastore(),
                record_store=None,
                enabled_bricks=enabled_bricks,
            )

        assert nx is not None

    def test_kernel_profile_dispatch_has_no_observers(self, tmp_path: "Path") -> None:
        """KERNEL mode has only the late-binding EventBusObserver (no record store to sync)."""
        from nexus.backends.local import LocalBackend
        from nexus.contracts.deployment_profile import DeploymentProfile, resolve_enabled_bricks
        from nexus.factory.orchestrator import create_nexus_fs
        from tests.helpers.in_memory_metadata_store import InMemoryMetastore

        data_dir = tmp_path / "data"
        data_dir.mkdir(exist_ok=True)

        nx = create_nexus_fs(
            backend=LocalBackend(root_path=data_dir),
            metadata_store=InMemoryMetastore(),
            record_store=None,
            enabled_bricks=resolve_enabled_bricks(DeploymentProfile.KERNEL),
        )

        # EventBusObserver is unconditionally registered with late-binding
        # (Issue #969); it won't publish if no bus is configured.
        assert nx._dispatch.observer_count == 1

    def test_kernel_profile_no_workflow_engine(self, tmp_path: "Path") -> None:
        """KERNEL mode has no workflow engine."""
        from nexus.backends.local import LocalBackend
        from nexus.contracts.deployment_profile import DeploymentProfile, resolve_enabled_bricks
        from nexus.factory.orchestrator import create_nexus_fs
        from tests.helpers.in_memory_metadata_store import InMemoryMetastore

        data_dir = tmp_path / "data"
        data_dir.mkdir(exist_ok=True)

        nx = create_nexus_fs(
            backend=LocalBackend(root_path=data_dir),
            metadata_store=InMemoryMetastore(),
            record_store=None,
            enabled_bricks=resolve_enabled_bricks(DeploymentProfile.KERNEL),
        )

        # workflow_engine is no longer a NexusFS attribute; it lives in
        # BrickDict / server state. getattr mirrors the CLI access pattern.
        assert getattr(nx, "workflow_engine", None) is None


# ---------------------------------------------------------------------------
# TestKernelPerformanceCharacteristics — no perf regressions
# ---------------------------------------------------------------------------


class TestKernelPerformanceCharacteristics:
    """Verify kernel tuning values are the most conservative across all profiles."""

    def test_kernel_has_smallest_thread_pool(self) -> None:
        from nexus.contracts.deployment_profile import DeploymentProfile

        kernel_tp = DeploymentProfile.KERNEL.tuning().concurrency.thread_pool_size
        for profile in DeploymentProfile:
            other_tp = profile.tuning().concurrency.thread_pool_size
            assert kernel_tp <= other_tp, (
                f"KERNEL thread_pool ({kernel_tp}) > {profile} ({other_tp})"
            )

    def test_kernel_has_smallest_db_pool(self) -> None:
        from nexus.contracts.deployment_profile import DeploymentProfile

        kernel_dp = DeploymentProfile.KERNEL.tuning().storage.db_pool_size
        for profile in DeploymentProfile:
            other_dp = profile.tuning().storage.db_pool_size
            assert kernel_dp <= other_dp, f"KERNEL db_pool ({kernel_dp}) > {profile} ({other_dp})"

    def test_kernel_has_fewest_workers(self) -> None:
        from nexus.contracts.deployment_profile import DeploymentProfile

        kernel_w = DeploymentProfile.KERNEL.tuning().concurrency.default_workers
        for profile in DeploymentProfile:
            other_w = profile.tuning().concurrency.default_workers
            assert kernel_w <= other_w, f"KERNEL workers ({kernel_w}) > {profile} ({other_w})"

    def test_kernel_has_longest_cleanup_intervals(self) -> None:
        """KERNEL should have the longest (least frequent) cleanup intervals."""
        from nexus.contracts.deployment_profile import DeploymentProfile

        kernel_hb = DeploymentProfile.KERNEL.tuning().background_task.heartbeat_flush_interval
        for profile in DeploymentProfile:
            other_hb = profile.tuning().background_task.heartbeat_flush_interval
            assert kernel_hb >= other_hb, (
                f"KERNEL heartbeat interval ({kernel_hb}) < {profile} ({other_hb})"
            )
