"""Tests for nexus.bricks.auth.daemon.jwt_cache."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from nexus.bricks.auth.daemon.jwt_cache import (
    FileJwtCache,
    KeyringJwtCache,
    make_jwt_cache,
)


def test_file_cache_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "jwt.cache"
    cache = FileJwtCache(path)
    assert cache.load() is None
    cache.store("eyJabc.def.ghi")
    assert cache.load() == "eyJabc.def.ghi"


def test_file_cache_writes_0600(tmp_path: Path) -> None:
    path = tmp_path / "jwt.cache"
    cache = FileJwtCache(path)
    cache.store("secret-token")
    mode = stat.S_IMODE(os.stat(path).st_mode)
    assert mode == 0o600, f"expected 0600, got {oct(mode)}"


def test_keyring_cache_round_trip() -> None:
    fake = MagicMock()
    fake.get_password.return_value = "stored-jwt"
    with patch.dict("sys.modules", {"keyring": fake}):
        cache = KeyringJwtCache()
        assert cache.load() == "stored-jwt"
        cache.store("new-jwt")
    fake.get_password.assert_called_once_with("com.nexus.daemon", "jwt")
    fake.set_password.assert_called_once_with("com.nexus.daemon", "jwt", "new-jwt")


def test_make_cache_respects_file_env_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NEXUS_DAEMON_JWT_CACHE_BACKEND", "file")
    cache = make_jwt_cache(tmp_path / "jwt.cache")
    assert isinstance(cache, FileJwtCache)


def test_make_cache_falls_back_to_file_when_keyring_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If keyring's probe roundtrip raises, we must degrade silently to file."""
    monkeypatch.delenv("NEXUS_DAEMON_JWT_CACHE_BACKEND", raising=False)
    fake = MagicMock()
    fake.set_password.side_effect = RuntimeError("no secret service running")
    with patch.dict("sys.modules", {"keyring": fake}):
        cache = make_jwt_cache(tmp_path / "jwt.cache")
    assert isinstance(cache, FileJwtCache)


def test_make_cache_picks_keyring_when_probe_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("NEXUS_DAEMON_JWT_CACHE_BACKEND", raising=False)
    fake = MagicMock()
    fake.get_password.return_value = "ok"
    with patch.dict("sys.modules", {"keyring": fake}):
        cache = make_jwt_cache(tmp_path / "jwt.cache")
    assert isinstance(cache, KeyringJwtCache)
