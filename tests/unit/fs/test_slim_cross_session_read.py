"""Regression test for Issue #3821.

Writes through one SlimNexusFS instance, then reads from a fresh one built
against the same on-disk SQLite metastore + CAS backend.  Previously the
read raised ``NexusFileNotFoundError`` because the Rust kernel's dcache was
empty and its metastore hook expects a redb path — the SQLite metastore was
invisible to it.  The facade's Python fallback keeps slim-package reads
working across process boundaries.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nexus.backends.storage.cas_local import CASLocalBackend
from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.contracts.exceptions import NexusFileNotFoundError
from nexus.contracts.types import OperationContext
from nexus.core.config import PermissionConfig
from nexus.core.mount_table import MountTable
from nexus.core.nexus_fs import NexusFS
from nexus.core.router import PathRouter
from nexus.fs import _make_mount_entry
from nexus.fs._facade import SlimNexusFS
from nexus.fs._sqlite_meta import SQLiteMetastore


def _build_slim(db_path: Path, data_dir: Path) -> SlimNexusFS:
    metastore = SQLiteMetastore(str(db_path))
    backend = CASLocalBackend(root_path=data_dir)
    mount_table = MountTable(metastore)
    router = PathRouter(mount_table)
    kernel = NexusFS(
        metadata_store=metastore,
        permissions=PermissionConfig(enforce=False),
        router=router,
    )
    kernel._init_cred = OperationContext(
        user_id="slim-xsession", groups=[], zone_id=ROOT_ZONE_ID, is_admin=True
    )
    kernel._driver_coordinator.mount("/files", backend)
    metastore.put(_make_mount_entry("/files", backend.name))
    return SlimNexusFS(kernel)


def test_slim_read_survives_fresh_process(tmp_path: Path) -> None:
    db_path = tmp_path / "meta.db"
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    writer = _build_slim(db_path, data_dir)
    writer.write("/files/hello.txt", b"hi")
    assert writer.read("/files/hello.txt") == b"hi"  # same-session baseline
    writer.close()  # close SQLite + kernel, like a process exit

    reader = _build_slim(db_path, data_dir)
    try:
        # Previously raised NexusFileNotFoundError — the Rust kernel has
        # cold dcache and no wired metastore in slim mode, so the Python
        # fallback is what makes this succeed.
        assert reader.read("/files/hello.txt") == b"hi"
    finally:
        reader.close()


def test_slim_read_missing_path_still_raises(tmp_path: Path) -> None:
    db_path = tmp_path / "meta.db"
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    slim = _build_slim(db_path, data_dir)
    try:
        with pytest.raises(NexusFileNotFoundError):
            slim.read("/files/does-not-exist.txt")
    finally:
        slim.close()
