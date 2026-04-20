from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from nexus.bricks.auth.daemon.config import DaemonConfig, DaemonConfigError


def _sample(tmp_path: Path) -> DaemonConfig:
    return DaemonConfig(
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
