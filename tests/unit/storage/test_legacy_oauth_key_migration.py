"""Tests for the legacy-redb → SQL OAuth key migration shim.

The migration is a one-shot upgrade path that copies an existing
OAuth encryption key from the pre-R20.18.5 filesystem-metastore
location (``~/.nexus/metastore[.redb]``) into the record_store (SQL)
so the post-R20.18.5 boot path, which reads only from SQL, can find
it. These tests stub out the redb reader so they don't need a real
nexus_kernel; the migration function is a pure orchestration layer
above ``_read_oauth_key_from_redb``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from nexus.contracts.auth_store_types import SystemSettingDTO
from nexus.lib.oauth.crypto import OAUTH_ENCRYPTION_KEY_NAME
from nexus.storage.auth_stores import legacy_oauth_key_migration
from nexus.storage.auth_stores.legacy_oauth_key_migration import migrate_legacy_oauth_key


class _FakeSettingsStore:
    def __init__(self, initial: dict[str, str] | None = None) -> None:
        self._data: dict[str, tuple[str, str | None]] = {
            k: (v, None) for k, v in (initial or {}).items()
        }

    def get_setting(self, key: str) -> SystemSettingDTO | None:
        if key not in self._data:
            return None
        value, description = self._data[key]
        return SystemSettingDTO(key=key, value=value, description=description)

    def set_setting(self, key: str, value: str, *, description: str | None = None) -> None:
        self._data[key] = (value, description)


def _patch_candidates(monkeypatch: pytest.MonkeyPatch, paths: list[Path]) -> None:
    monkeypatch.setattr(legacy_oauth_key_migration, "_legacy_redb_candidates", lambda: paths)


def _patch_reader(monkeypatch: pytest.MonkeyPatch, result_by_path: dict[Path, Any]) -> None:
    def _fake_read(path: Path) -> str | None:
        return result_by_path.get(path)

    monkeypatch.setattr(legacy_oauth_key_migration, "_read_oauth_key_from_redb", _fake_read)


class TestIdempotency:
    def test_skips_when_sql_already_has_key(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        store = _FakeSettingsStore(initial={OAUTH_ENCRYPTION_KEY_NAME: "existing-key"})

        # Rig the reader to return a different key — we must NOT see it.
        candidate = tmp_path / "metastore.redb"
        candidate.touch()
        _patch_candidates(monkeypatch, [candidate])
        _patch_reader(monkeypatch, {candidate: "legacy-key-would-overwrite"})

        migrated = migrate_legacy_oauth_key(store)

        assert migrated is False
        dto = store.get_setting(OAUTH_ENCRYPTION_KEY_NAME)
        assert dto is not None and dto.value == "existing-key"

    def test_running_twice_is_noop(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        store = _FakeSettingsStore()
        candidate = tmp_path / "metastore.redb"
        candidate.touch()
        _patch_candidates(monkeypatch, [candidate])
        _patch_reader(monkeypatch, {candidate: "legacy-key"})

        assert migrate_legacy_oauth_key(store) is True
        # Second call sees the key already in SQL → skips.
        assert migrate_legacy_oauth_key(store) is False


class TestMigrationHappyPath:
    def test_migrates_redb_candidate(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        store = _FakeSettingsStore()
        candidate = tmp_path / "metastore.redb"
        candidate.touch()
        _patch_candidates(monkeypatch, [candidate])
        _patch_reader(monkeypatch, {candidate: "secret-legacy-key"})

        migrated = migrate_legacy_oauth_key(store)

        assert migrated is True
        dto = store.get_setting(OAUTH_ENCRYPTION_KEY_NAME)
        assert dto is not None
        assert dto.value == "secret-legacy-key"
        assert dto.description is not None and "Migrated" in dto.description

    def test_prefers_redb_over_noext_when_both_present(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        store = _FakeSettingsStore()
        redb = tmp_path / "metastore.redb"
        noext = tmp_path / "metastore"
        redb.touch()
        noext.touch()
        _patch_candidates(monkeypatch, [redb, noext])
        _patch_reader(
            monkeypatch,
            {redb: "redb-era-key", noext: "pre-redb-era-key"},
        )

        migrate_legacy_oauth_key(store)

        dto = store.get_setting(OAUTH_ENCRYPTION_KEY_NAME)
        assert dto is not None and dto.value == "redb-era-key"

    def test_falls_through_to_noext_when_redb_missing(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        store = _FakeSettingsStore()
        redb = tmp_path / "metastore.redb"  # does NOT exist
        noext = tmp_path / "metastore"
        noext.touch()
        _patch_candidates(monkeypatch, [redb, noext])
        _patch_reader(monkeypatch, {noext: "pre-redb-era-key"})

        migrate_legacy_oauth_key(store)

        dto = store.get_setting(OAUTH_ENCRYPTION_KEY_NAME)
        assert dto is not None and dto.value == "pre-redb-era-key"

    def test_falls_through_to_noext_when_redb_has_no_key(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        store = _FakeSettingsStore()
        redb = tmp_path / "metastore.redb"
        noext = tmp_path / "metastore"
        redb.touch()
        noext.touch()
        _patch_candidates(monkeypatch, [redb, noext])
        _patch_reader(monkeypatch, {redb: None, noext: "legacy-key"})

        migrate_legacy_oauth_key(store)

        dto = store.get_setting(OAUTH_ENCRYPTION_KEY_NAME)
        assert dto is not None and dto.value == "legacy-key"


class TestFreshInstall:
    def test_no_legacy_files_is_noop(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        store = _FakeSettingsStore()
        _patch_candidates(monkeypatch, [tmp_path / "metastore.redb", tmp_path / "metastore"])
        _patch_reader(monkeypatch, {})  # reader never called (file doesn't exist)

        migrated = migrate_legacy_oauth_key(store)

        assert migrated is False
        assert store.get_setting(OAUTH_ENCRYPTION_KEY_NAME) is None

    def test_legacy_files_with_no_key_is_noop(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        store = _FakeSettingsStore()
        redb = tmp_path / "metastore.redb"
        redb.touch()
        _patch_candidates(monkeypatch, [redb])
        _patch_reader(monkeypatch, {redb: None})  # file exists but no key inside

        migrated = migrate_legacy_oauth_key(store)

        assert migrated is False
        assert store.get_setting(OAUTH_ENCRYPTION_KEY_NAME) is None
