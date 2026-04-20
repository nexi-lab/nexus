from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from nexus.bricks.auth.daemon.config import (
    DaemonConfig,
    DaemonConfigError,
    daemons_root,
    default_profile_for,
    list_profiles,
    profile_dir,
)


def _sample(tmp_path: Path, profile: str = "test.nexus") -> DaemonConfig:
    return DaemonConfig(
        profile=profile,
        server_url="https://test.nexus",
        tenant_id=uuid.uuid4(),
        principal_id=uuid.uuid4(),
        machine_id=uuid.uuid4(),
        key_path=tmp_path / "machine.key",
        jwt_cache_path=tmp_path / "jwt.cache",
        server_pubkey_path=tmp_path / "server.pub.pem",
    )


def test_round_trip(tmp_path: Path) -> None:
    cfg = _sample(tmp_path)
    path = tmp_path / "daemon.toml"
    cfg.save(path)
    loaded = DaemonConfig.load(path)
    assert loaded == cfg


def test_missing_file(tmp_path: Path) -> None:
    with pytest.raises(DaemonConfigError, match="not found"):
        DaemonConfig.load(tmp_path / "no-such.toml")


def test_corrupt_file(tmp_path: Path) -> None:
    p = tmp_path / "daemon.toml"
    p.write_text("this is not valid toml = == =")
    with pytest.raises(DaemonConfigError, match="parse"):
        DaemonConfig.load(p)


def test_missing_key(tmp_path: Path) -> None:
    p = tmp_path / "daemon.toml"
    p.write_text('server_url = "https://x"\n')
    with pytest.raises(DaemonConfigError, match="missing"):
        DaemonConfig.load(p)


def test_default_profile_for() -> None:
    assert default_profile_for("http://localhost:2026") == "localhost-2026"
    assert default_profile_for("https://api.nexus.ai") == "api.nexus.ai"
    assert default_profile_for("https://api.nexus.ai:8443") == "api.nexus.ai-8443"
    assert default_profile_for("https://a.b.c.d") == "a.b.c.d"
    # Garbage in → "default" fallback, never an empty string
    assert default_profile_for("garbage") == "default"


def test_profile_dir_layout(tmp_path: Path) -> None:
    assert daemons_root(tmp_path) == tmp_path / "daemons"
    assert profile_dir(tmp_path, "work") == tmp_path / "daemons" / "work"


def test_list_profiles_enumerates_enrolled(tmp_path: Path) -> None:
    """Only directories that contain daemon.toml count as profiles."""
    assert list_profiles(tmp_path) == []
    root = daemons_root(tmp_path)
    (root / "work").mkdir(parents=True)
    (root / "work" / "daemon.toml").write_text("x")
    (root / "personal").mkdir()
    (root / "personal" / "daemon.toml").write_text("x")
    (root / "ghost").mkdir()  # no daemon.toml → skipped
    assert list_profiles(tmp_path) == ["personal", "work"]
