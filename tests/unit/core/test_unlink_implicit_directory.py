"""Deleting an implicit (backend-only) directory must not answer 404.

On a path_local backend a write creates its parent directories physically
without metastore rows.  exists/glob then report the directory, but the
kernel's unlink found no row and missed, so ``DELETE /files/delete`` on the
(empty) directory answered 404 and nothing could remove it.  sys_unlink now
gives such a directory an explicit DT_DIR row and deletes it through the
kernel's rmdir path.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.contracts.exceptions import BackendError, NexusFileNotFoundError
from nexus.contracts.metadata import DT_DIR, DT_MOUNT
from nexus.core.nexus_fs_metadata import MetadataMixin


class _Kernel:
    def __init__(self, stat: dict[str, Any] | None) -> None:
        self.stat = stat
        self.rows: dict[str, int] = {}
        self.unlinks: list[str] = []
        self.post_hooks: list[tuple[str, Any]] = []
        self.setattr_calls: list[tuple[str, dict[str, Any]]] = []

    def sys_stat(self, path: str, zone_id: str = ROOT_ZONE_ID) -> dict[str, Any] | None:
        return self.stat

    def sys_unlink(self, path: str, ctx: Any = None, recursive: bool = False) -> Any:
        self.unlinks.append(path)
        entry_type = self.rows.pop(path, None)
        return SimpleNamespace(hit=entry_type is not None, entry_type=entry_type or 0)

    def sys_setattr(self, path: str, **kwargs: Any) -> Any:
        self.setattr_calls.append((path, kwargs))
        self.rows[path] = kwargs["entry_type"]
        return SimpleNamespace(created=True)

    def dispatch_post_hooks(self, op: str, ctx: Any) -> None:
        self.post_hooks.append((op, ctx))


class _FS(MetadataMixin):
    def __init__(self, kernel: _Kernel) -> None:
        self._kernel = kernel
        self._zone_id = ROOT_ZONE_ID
        self._hook_specs: dict[str, Any] = {}
        self._driver_coordinator = SimpleNamespace(unmount=lambda *a, **k: None)

    def resolve_delete(self, path: str, context: Any = None) -> tuple[bool, Any]:
        return False, None

    def _prepare_rust_ctx(self, context: Any) -> tuple[str, str | None, bool, Any]:
        return ROOT_ZONE_ID, None, True, None

    def _resolve_cred(self, context: Any) -> Any:
        return context


def test_empty_implicit_directory_is_removed_via_rmdir() -> None:
    kernel = _Kernel({"path": "/ws/notes", "is_directory": True, "entry_type": 0})
    result = _FS(kernel).sys_unlink("/ws/notes", context=None)

    assert isinstance(result, dict)
    assert kernel.unlinks == ["/ws/notes", "/ws/notes"], "miss, materialise, retry"
    assert kernel.setattr_calls == [("/ws/notes", {"entry_type": DT_DIR, "zone_id": ROOT_ZONE_ID})]
    # rmdir post-hooks run (search subtree eviction relies on them).
    assert [op for op, _ in kernel.post_hooks] == ["rmdir"]


def test_missing_file_still_404s() -> None:
    kernel = _Kernel(None)
    with pytest.raises(NexusFileNotFoundError):
        _FS(kernel).sys_unlink("/ws/gone.txt", context=None)
    assert kernel.setattr_calls == []
    assert kernel.unlinks == ["/ws/gone.txt"]


def test_mount_miss_is_not_materialised_as_a_directory() -> None:
    # A mount miss keeps its stranded-route handling (here: the fake route
    # never goes away → BackendError); it must not get a DT_DIR row.
    kernel = _Kernel({"path": "/mnt/x", "is_directory": True, "entry_type": DT_MOUNT})
    with pytest.raises(BackendError):
        _FS(kernel).sys_unlink("/mnt/x", context=None)
    assert kernel.setattr_calls == []
