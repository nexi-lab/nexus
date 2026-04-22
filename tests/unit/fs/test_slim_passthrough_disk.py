"""Regression test for Issue #3831.

Pre-existing files dropped into the root of a ``local://`` (path-addressed,
passthrough) mount must be visible via ``fs.read()`` and ``fs.ls()`` even
though the Rust kernel has no dcache entry and the Python metastore has
no row for them.  The facade falls through to the backend's path-based
``read_content`` / ``list_dir`` for this scheme, because the virtual
path *is* the on-disk path — no hashing, no indirection.

Contrast with ``CASLocalBackend`` (``cas-local://``), where the virtual
path maps to a hash-named blob and path-based fallback is meaningless;
that backend keeps its metastore-keyed fallback (Issue #3821).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nexus.backends.storage.path_local import PathLocalBackend
from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.contracts.exceptions import NexusFileNotFoundError
from nexus.contracts.types import OperationContext
from nexus.core.config import PermissionConfig
from nexus.core.nexus_fs import NexusFS
from nexus.fs import _make_mount_entry
from nexus.fs._facade import SlimNexusFS
from nexus.fs._sqlite_meta import SQLiteMetastore


def _build_slim(db_path: Path, data_dir: Path, mount_point: str = "/files") -> SlimNexusFS:
    metastore = SQLiteMetastore(str(db_path))
    backend = PathLocalBackend(root_path=data_dir)
    kernel = NexusFS(
        metadata_store=metastore,
        permissions=PermissionConfig(enforce=False),
    )
    kernel._init_cred = OperationContext(
        user_id="slim-passthrough", groups=[], zone_id=ROOT_ZONE_ID, is_admin=True
    )
    kernel._driver_coordinator._store_mount_info(mount_point, backend)
    metastore.put(_make_mount_entry(mount_point, backend.name))
    return SlimNexusFS(kernel)


def test_preexisting_ondisk_file_is_readable(tmp_path: Path) -> None:
    """Bug report: files on disk under the mount root are invisible to
    the facade unless it wrote them itself.  Mount a directory with a
    pre-existing file and the facade must serve it."""
    db_path = tmp_path / "meta.db"
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "hello.txt").write_bytes(b"from disk")

    slim = _build_slim(db_path, data_dir)
    try:
        assert slim.read("/files/hello.txt") == b"from disk"
    finally:
        slim.close()


def test_preexisting_ondisk_file_is_listed(tmp_path: Path) -> None:
    """Companion to the read case — ``ls`` must merge in on-disk entries
    the kernel doesn't know about, else users see an empty mount."""
    db_path = tmp_path / "meta.db"
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "a.txt").write_bytes(b"a")
    (data_dir / "b.txt").write_bytes(b"b")
    (data_dir / "sub").mkdir()
    (data_dir / "sub" / "c.txt").write_bytes(b"c")

    slim = _build_slim(db_path, data_dir)
    try:
        shallow = slim.ls("/files")
        names = sorted(p.rstrip("/").rsplit("/", 1)[-1] for p in shallow)
        assert "a.txt" in names
        assert "b.txt" in names
        assert "sub" in names

        recursive = slim.ls("/files", recursive=True)
        rec_names = sorted(p.rstrip("/") for p in recursive)
        assert any(p.endswith("/files/sub/c.txt") for p in rec_names)
    finally:
        slim.close()


def test_missing_path_still_raises(tmp_path: Path) -> None:
    """Passthrough must not swallow legit NOT_FOUND — a file that's
    neither in metastore nor on disk is still an error."""
    db_path = tmp_path / "meta.db"
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    slim = _build_slim(db_path, data_dir)
    try:
        with pytest.raises(NexusFileNotFoundError):
            slim.read("/files/does-not-exist.txt")
    finally:
        slim.close()


def test_passthrough_read_propagates_backend_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Backend-layer failures (permission, I/O, corruption, auth)
    must NOT be silently collapsed into a 404.  Only a real
    NexusFileNotFoundError from the backend should trigger the
    fall-through re-raise of the original not-found."""
    from nexus.contracts.exceptions import BackendError

    db_path = tmp_path / "meta.db"
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "broken.txt").write_bytes(b"data")

    slim = _build_slim(db_path, data_dir)

    def _raise_backend_error(*_a: object, **_kw: object) -> bytes:
        raise BackendError("simulated disk corruption", backend="path_local")

    try:
        mount_entry = next(iter(slim._kernel.router._mount_table._entries.values()))
        backend = mount_entry.backend
        monkeypatch.setattr(backend, "read_content", _raise_backend_error)

        with pytest.raises(BackendError, match="simulated disk corruption"):
            slim.read("/files/broken.txt")
    finally:
        slim.close()


def test_passthrough_ls_propagates_backend_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same contract for ls — a backend list failure must surface as
    the actual error, not an empty list that looks like a clean dir."""
    from nexus.contracts.exceptions import BackendError

    db_path = tmp_path / "meta.db"
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "file.txt").write_bytes(b"x")

    slim = _build_slim(db_path, data_dir)

    def _raise_backend_error(*_a: object, **_kw: object) -> list[str]:
        raise BackendError("simulated listing failure", backend="path_local")

    try:
        mount_entry = next(iter(slim._kernel.router._mount_table._entries.values()))
        backend = mount_entry.backend
        monkeypatch.setattr(backend, "list_dir", _raise_backend_error)

        with pytest.raises(BackendError, match="simulated listing failure"):
            slim.ls("/files")
    finally:
        slim.close()


def test_merge_dedupes_kernel_and_backend_entries(tmp_path: Path) -> None:
    """Files the kernel already knows about (metastore row present) and
    files only on disk must both appear in the listing without
    duplicates — the merge has to dedup against the kernel output."""
    from datetime import UTC, datetime

    from nexus.contracts.metadata import FileMetadata

    db_path = tmp_path / "meta.db"
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    # One file on disk only (no metastore row).
    (data_dir / "on_disk.txt").write_bytes(b"disk")
    # One file on disk *and* with a metastore row (simulating a prior
    # facade write that populated both).
    (data_dir / "known.txt").write_bytes(b"known")

    slim = _build_slim(db_path, data_dir)
    try:
        now = datetime.now(UTC)
        slim._kernel.metadata.put(
            FileMetadata(
                path="/files/known.txt",
                physical_path="known.txt",
                size=len(b"known"),
                etag=None,
                mime_type="text/plain",
                created_at=now,
                modified_at=now,
                version=1,
                zone_id=ROOT_ZONE_ID,
                backend_name="path_local",
            )
        )

        entries = slim.ls("/files")
        names = [p.rstrip("/").rsplit("/", 1)[-1] for p in entries]
        assert sorted(set(names)) == ["known.txt", "on_disk.txt"]
        # Merge must not double-list the metastore entry.
        assert names.count("known.txt") == 1

        assert slim.read("/files/on_disk.txt") == b"disk"
        assert slim.read("/files/known.txt") == b"known"
    finally:
        slim.close()
