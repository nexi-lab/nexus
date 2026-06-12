"""Spawn-path resolution contract for KernelClient (#4343).

``metadata_path`` historically meant "metastore database file" in
Python-only runs and "kernel data directory" post-KernelClient. The
resolver must keep both callers working: directories pass through as
``NEXUS_DATA_DIR`` (the kernel derives ``<data_dir>/metastore.redb``);
explicit ``.redb`` files are forwarded verbatim as
``NEXUS_METASTORE_PATH`` so a preexisting namespace file is reopened,
not silently abandoned to a sidecar directory.
"""

from __future__ import annotations

from pathlib import Path

from nexus.remote.kernel_client import _resolve_kernel_spawn_paths


def test_none_passes_through() -> None:
    assert _resolve_kernel_spawn_paths(None) == (None, None)


def test_directory_is_data_dir_only(tmp_path: Path) -> None:
    data_dir, metastore = _resolve_kernel_spawn_paths(str(tmp_path))
    assert data_dir == str(tmp_path)
    assert metastore is None


def test_nonexistent_dirlike_path_is_data_dir(tmp_path: Path) -> None:
    target = tmp_path / "not-yet-created"
    data_dir, metastore = _resolve_kernel_spawn_paths(str(target))
    assert data_dir == str(target)
    assert metastore is None


def test_redb_file_is_forwarded_as_metastore(tmp_path: Path) -> None:
    redb = tmp_path / "namespace.redb"
    data_dir, metastore = _resolve_kernel_spawn_paths(str(redb))
    assert metastore == str(redb)
    # Payloads/TLS land in the deterministic sidecar, not on the file.
    assert data_dir == str(redb) + ".kernel"


def test_existing_redb_file_is_forwarded_as_metastore(tmp_path: Path) -> None:
    redb = tmp_path / "namespace.redb"
    redb.write_bytes(b"existing")
    data_dir, metastore = _resolve_kernel_spawn_paths(str(redb))
    assert metastore == str(redb)
    assert data_dir == str(redb) + ".kernel"


def test_legacy_existing_db_file_keeps_sidecar_fallback(tmp_path: Path) -> None:
    # SQLite-era file the kernel cannot reopen as redb: keep the
    # pre-#4343 behavior (fresh namespace in a sidecar dir) instead of
    # failing the boot on it.
    legacy = tmp_path / "metadata.db"
    legacy.write_bytes(b"sqlite")
    data_dir, metastore = _resolve_kernel_spawn_paths(str(legacy))
    assert metastore is None
    assert data_dir == str(legacy) + ".kernel"
