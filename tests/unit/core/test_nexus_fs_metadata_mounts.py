from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from nexus.backends.storage.path_local import PathLocalBackend
from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.contracts.metadata import DT_MOUNT
from nexus.core.nexus_fs_metadata import MetadataMixin


class _MountOnlyMetadata(MetadataMixin):
    def __init__(self) -> None:
        self._kernel = MagicMock()
        self._kernel.sys_setattr.return_value = {"path": "/zone/local", "created": True}
        self._zone_id = ROOT_ZONE_ID
        self._hook_specs = {}
        self.metadata = None
        self._driver_coordinator = None

    def _validate_path(self, path: str, *, allow_root: bool = False) -> str:
        return path


def test_path_local_mount_preserves_backend_fsync_setting(tmp_path: Path) -> None:
    fs = _MountOnlyMetadata()
    backend = PathLocalBackend(tmp_path, fsync=False)

    fs.sys_setattr(
        "/zone/local",
        entry_type=DT_MOUNT,
        backend=backend,
        zone_id=ROOT_ZONE_ID,
    )

    fs._kernel.sys_setattr.assert_called_once()
    kwargs = fs._kernel.sys_setattr.call_args.kwargs
    assert kwargs["backend_type"] == "path_local"
    assert kwargs["fsync"] is False
