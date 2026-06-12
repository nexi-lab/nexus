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

import pytest

from nexus.remote.kernel_client import _apply_storage_env, _resolve_kernel_spawn_paths


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
    legacy.write_bytes(b"SQLite format 3\x00")
    data_dir, metastore = _resolve_kernel_spawn_paths(str(legacy))
    assert metastore is None
    assert data_dir == str(legacy) + ".kernel"


def test_suffixless_redb_content_is_forwarded_as_metastore(tmp_path: Path) -> None:
    # A configured metastore path need not end in .redb — an existing
    # file with the redb magic header is the namespace itself and must
    # be reopened, not demoted to a data directory.
    store = tmp_path / "metastore"
    store.write_bytes(b"redb\x1a\x0a\xa9\x0d\x0a" + b"\x00" * 16)
    data_dir, metastore = _resolve_kernel_spawn_paths(str(store))
    assert metastore == str(store)
    assert data_dir == str(store) + ".kernel"


def test_suffixless_non_redb_file_keeps_sidecar_fallback(tmp_path: Path) -> None:
    store = tmp_path / "metastore"
    store.write_bytes(b"not a database")
    data_dir, metastore = _resolve_kernel_spawn_paths(str(store))
    assert metastore is None
    assert data_dir == str(store) + ".kernel"


def test_ambient_metastore_env_dropped_when_client_manages_storage(tmp_path: Path) -> None:
    # Two clients with distinct metadata_paths must not be silently
    # collapsed onto one shared namespace file by ambient env.
    env = {"NEXUS_METASTORE_PATH": "/somewhere/shared.redb"}
    _apply_storage_env(env, str(tmp_path))
    assert env["NEXUS_DATA_DIR"] == str(tmp_path)
    assert "NEXUS_METASTORE_PATH" not in env


def test_explicit_redb_metadata_path_wins_over_ambient(tmp_path: Path) -> None:
    redb = tmp_path / "namespace.redb"
    env = {"NEXUS_METASTORE_PATH": "/somewhere/shared.redb"}
    _apply_storage_env(env, str(redb))
    assert env["NEXUS_METASTORE_PATH"] == str(redb)
    assert env["NEXUS_DATA_DIR"] == str(redb) + ".kernel"


def test_ambient_env_passes_through_without_metadata_path() -> None:
    # Operator-managed spawn: no metadata_path means the ambient env is
    # the operator's contract — leave it alone.
    env = {"NEXUS_METASTORE_PATH": "/operator/choice.redb"}
    _apply_storage_env(env, None)
    assert env == {"NEXUS_METASTORE_PATH": "/operator/choice.redb"}


def test_existing_directory_named_like_redb_is_data_dir(tmp_path: Path) -> None:
    # A directory literally named *.redb must not be forwarded as a
    # metastore file — the kernel would fail opening a dir as redb.
    trap = tmp_path / "store.redb"
    trap.mkdir()
    data_dir, metastore = _resolve_kernel_spawn_paths(str(trap))
    assert data_dir == str(trap)
    assert metastore is None


def test_explicit_metastore_file_forwarded_verbatim_suffixless(tmp_path: Path) -> None:
    # The explicit channel carries intent: fresh suffixless paths are
    # NOT demoted to data dirs (operator-requested namespace file).
    target = tmp_path / "metastore"
    env: dict[str, str] = {}
    _apply_storage_env(env, None, str(target))
    assert env["NEXUS_METASTORE_PATH"] == str(target)
    assert env["NEXUS_DATA_DIR"] == str(target) + ".kernel"


def test_explicit_metastore_file_with_separate_data_dir(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    ns = tmp_path / "namespace"
    env: dict[str, str] = {}
    _apply_storage_env(env, str(data), str(ns))
    assert env["NEXUS_METASTORE_PATH"] == str(ns)
    assert env["NEXUS_DATA_DIR"] == str(data)


def test_inherited_env_naming_same_path_is_explicit_intent(tmp_path: Path) -> None:
    # `NEXUS_METASTORE_PATH=/x nexus ...` with metadata_path=/x: the
    # operator named that exact file — forward it, don't demote the
    # fresh suffixless path to a data dir.
    target = str(tmp_path / "ns")
    env = {"NEXUS_METASTORE_PATH": target}
    _apply_storage_env(env, target)
    assert env["NEXUS_METASTORE_PATH"] == target
    assert env["NEXUS_DATA_DIR"] == target + ".kernel"


def test_inherited_env_same_path_existing_dir_stays_data_dir(tmp_path: Path) -> None:
    # Deployed volumes have a directory at the configured path; a
    # redundant env naming it must not flip it to a (failing) file open.
    env = {"NEXUS_METASTORE_PATH": str(tmp_path)}
    _apply_storage_env(env, str(tmp_path))
    assert env["NEXUS_DATA_DIR"] == str(tmp_path)
    assert "NEXUS_METASTORE_PATH" not in env


def test_ephemeral_strips_ambient_durable_env(tmp_path: Path) -> None:
    # ":memory:" kernels must never reopen an ambient shared namespace
    # nor default to a durable cwd-relative data dir.
    env = {"NEXUS_METASTORE_PATH": "/shared/namespace.redb"}
    _apply_storage_env(env, None, None, ephemeral_dir=str(tmp_path))
    assert env["NEXUS_DATA_DIR"] == str(tmp_path)
    assert "NEXUS_METASTORE_PATH" not in env


def test_memory_kernel_client_gets_ephemeral_tempdir(monkeypatch: pytest.MonkeyPatch) -> None:
    # The ":memory:" mapping must reach KernelClient as ephemeral=True.
    from nexus.remote.kernel_client import KernelClient

    client = KernelClient(ephemeral=True)
    assert client._ephemeral is True
    assert client._metadata_path is None


def test_unreadable_existing_file_fails_closed(tmp_path: Path) -> None:
    # A permission-broken existing file may BE the namespace: booting a
    # fresh sidecar instead would silently lose it (#4343 symptom).
    import os as _os

    store = tmp_path / "metastore"
    store.write_bytes(b"redb\x1a\x0a\xa9\x0d\x0a")
    store.chmod(0)
    if _os.access(store, _os.R_OK):  # running as root — cannot test
        pytest.skip("permissions not enforced for this user")
    try:
        with pytest.raises(OSError):
            _resolve_kernel_spawn_paths(str(store))
    finally:
        store.chmod(0o600)
