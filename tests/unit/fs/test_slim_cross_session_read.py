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
from nexus.contracts.metadata import DT_MOUNT
from nexus.contracts.types import OperationContext
from nexus.core.config import PermissionConfig
from nexus.core.nexus_fs import NexusFS
from nexus.fs import _make_mount_entry
from nexus.fs._facade import SlimNexusFS
from nexus.fs._sqlite_meta import SQLiteMetastore


def _build_slim(db_path: Path, data_dir: Path) -> SlimNexusFS:
    metastore = SQLiteMetastore(str(db_path))
    backend = CASLocalBackend(root_path=data_dir)
    kernel = NexusFS(
        metadata_store=metastore,
        permissions=PermissionConfig(enforce=False),
        init_cred=OperationContext(
            user_id="slim-xsession", groups=[], zone_id=ROOT_ZONE_ID, is_admin=True
        ),
    )
    # Mount through the kernel so the Rust router learns about it; the
    # metastore entry keeps the route visible to ``SlimNexusFS`` after
    # the kernel shuts down (cross-session read path for #3821).
    kernel.sys_setattr("/files", entry_type=DT_MOUNT, backend=backend)
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


def test_slim_read_propagates_non_notfound_backend_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Corruption/permission/IO failures must NOT be collapsed into FileNotFound.

    Masking real backend errors as missing-file would let operators silently
    recreate over corrupted blobs or retry against a permission-denied disk.
    The slim fallback only swallows genuine ``NexusFileNotFoundError`` from
    the backend; every other exception propagates unchanged.
    """
    from nexus.contracts.exceptions import BackendError

    db_path = tmp_path / "meta.db"
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    writer = _build_slim(db_path, data_dir)
    writer.write("/files/corrupt.txt", b"payload")
    writer.close()

    reader = _build_slim(db_path, data_dir)
    # Force the backend's ``read_content`` to raise a non-not-found error
    # — this mirrors a corrupt blob or unreadable data file on disk.
    mount_entry = next(iter(reader._kernel.router._mount_table._entries.values()))
    backend = mount_entry.backend

    def _raise_backend_error(*_a: object, **_kw: object) -> bytes:
        raise BackendError("simulated disk corruption", backend="local")

    monkeypatch.setattr(backend, "read_content", _raise_backend_error)

    try:
        with pytest.raises(BackendError, match="simulated disk corruption"):
            reader.read("/files/corrupt.txt")
    finally:
        reader.close()
