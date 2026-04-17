"""Integration tests for mount I/O profiles (Issue #1413).

Tests that io_profile propagates through the DriverLifecycleCoordinator ->
PathRouter -> RouteResult pipeline and that MountConfigModel round-trips
io_profile through the database.
"""

from unittest.mock import MagicMock

import pytest

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.contracts.io_profile import IOProfile
from nexus.core.driver_lifecycle_coordinator import DriverLifecycleCoordinator, _PyMountInfo
from nexus.core.path_utils import canonicalize_path
from nexus.core.router import PathRouter, RouteResult
from tests.helpers.dict_metastore import DictMetastore


def _add_mount(
    dlc: DriverLifecycleCoordinator,
    mount_point: str,
    backend,
    *,
    readonly: bool = False,
    admin_only: bool = False,
    io_profile: str = "balanced",
    zone_id: str = ROOT_ZONE_ID,
) -> None:
    """Populate the DLC mount map directly — avoids touching the Rust kernel.

    F2 MountTable migration removed the standalone Python MountTable; these
    tests now exercise the Python fallback in PathRouter by writing
    ``_PyMountInfo`` records directly into the DLC.
    """
    canonical = canonicalize_path(mount_point, zone_id)
    dlc._mounts[canonical] = _PyMountInfo(
        backend=backend,
        readonly=readonly,
        admin_only=admin_only,
        io_profile=io_profile,
        zone_id=zone_id,
    )


class TestPathRouterIOProfile:
    """Test io_profile propagation through PathRouter and RouteResult."""

    def _make_backend(self) -> MagicMock:
        backend = MagicMock()
        backend.name = "mock-backend"
        return backend

    def _make_dlc_and_router(self) -> tuple[DriverLifecycleCoordinator, PathRouter]:
        metastore = DictMetastore()
        # kernel=None → PathRouter uses the Python LPM fallback.
        dlc = DriverLifecycleCoordinator(dispatch=None, kernel=None)
        router = PathRouter(dlc, metastore, None)
        return dlc, router

    def test_add_mount_with_io_profile(self) -> None:
        dlc, router = self._make_dlc_and_router()
        backend = self._make_backend()
        _add_mount(dlc, "/weights", backend, io_profile="fast_read")

        mounts = list(router.list_mounts())
        weight_mount = [m for m in mounts if m.mount_point == "/weights"]
        assert len(weight_mount) == 1

    def test_route_propagates_io_profile(self) -> None:
        dlc, router = self._make_dlc_and_router()
        backend = self._make_backend()
        _add_mount(dlc, "/models", backend, io_profile="fast_read")

        result = router.route("/models/gpt4/weights.bin")
        assert result is not None
        assert isinstance(result, RouteResult)
        assert result.io_profile == "fast_read"

    def test_route_default_io_profile(self) -> None:
        dlc, router = self._make_dlc_and_router()
        backend = self._make_backend()
        _add_mount(dlc, "/data", backend)

        result = router.route("/data/file.txt")
        assert result is not None
        assert result.io_profile == "balanced"

    def test_multiple_mounts_different_profiles(self) -> None:
        dlc, router = self._make_dlc_and_router()
        backend_read = self._make_backend()
        backend_read.name = "read-backend"
        backend_write = self._make_backend()
        backend_write.name = "write-backend"
        backend_edit = self._make_backend()
        backend_edit.name = "edit-backend"

        _add_mount(dlc, "/models", backend_read, io_profile="fast_read")
        _add_mount(dlc, "/logs", backend_write, io_profile="fast_write")
        _add_mount(dlc, "/workspace", backend_edit, io_profile="edit")

        result_models = router.route("/models/weights.bin")
        result_logs = router.route("/logs/app.log")
        result_ws = router.route("/workspace/readme.md")

        assert result_models is not None
        assert result_models.io_profile == "fast_read"
        assert result_logs is not None
        assert result_logs.io_profile == "fast_write"
        assert result_ws is not None
        assert result_ws.io_profile == "edit"

    def test_route_result_io_profile_field(self) -> None:
        backend = self._make_backend()
        rr = RouteResult(
            backend=backend,
            metastore=DictMetastore(),
            backend_path="/file.txt",
            mount_point="/data",
            readonly=False,
            io_profile="edit",
        )
        assert rr.io_profile == "edit"


class TestMountConfigModelIOProfile:
    """Test MountConfigModel database persistence with io_profile."""

    def test_model_explicit_balanced_io_profile(self) -> None:
        from nexus.storage.models.infrastructure import MountConfigModel

        model = MountConfigModel(
            mount_id="test-123",
            mount_point="/data",
            backend_type="cas_local",
            backend_config='{"root": "/tmp"}',
            io_profile="balanced",
        )
        assert model.io_profile == "balanced"

    def test_model_custom_io_profile(self) -> None:
        from nexus.storage.models.infrastructure import MountConfigModel

        model = MountConfigModel(
            mount_id="test-456",
            mount_point="/weights",
            backend_type="cas_gcs",
            backend_config='{"bucket": "ml-weights"}',
            io_profile="fast_read",
        )
        assert model.io_profile == "fast_read"

    @pytest.mark.parametrize("profile", [p.value for p in IOProfile])
    def test_model_all_profiles_accepted(self, profile: str) -> None:
        from nexus.storage.models.infrastructure import MountConfigModel

        model = MountConfigModel(
            mount_id=f"test-{profile}",
            mount_point=f"/{profile}",
            backend_type="cas_local",
            backend_config='{"root": "/tmp"}',
            io_profile=profile,
        )
        assert model.io_profile == profile


class TestIOProfileEndToEnd:
    """Test IOProfile -> config -> ReadaheadConfig -> cache_priority pipeline."""

    def test_fast_read_full_pipeline(self) -> None:
        """FAST_READ: high readahead, high cache priority."""
        profile = IOProfile.FAST_READ
        cfg = profile.config()
        assert cfg.readahead_enabled is True
        assert cfg.readahead_max_window == 64 * 1024 * 1024
        assert cfg.cache_priority == 3

    def test_fast_write_full_pipeline(self) -> None:
        """FAST_WRITE: no readahead, low cache priority."""
        profile = IOProfile.FAST_WRITE
        cfg = profile.config()
        assert cfg.readahead_enabled is False
        assert cfg.cache_priority == 1

    def test_archive_full_pipeline(self) -> None:
        """ARCHIVE: everything disabled/minimal."""
        profile = IOProfile.ARCHIVE
        cfg = profile.config()
        assert cfg.readahead_enabled is False
        assert cfg.cache_priority == 0
        assert cfg.write_buffer_max_size == 0
