"""Regression tests for the documented local quickstart path."""

from __future__ import annotations

from pathlib import Path

import nexus
from nexus.raft import zone_manager


def test_local_connect_falls_back_when_full_federation_build_is_unavailable(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """A source checkout should still support the local SDK quickstart."""

    def _raise_missing_full_build(*args, **kwargs):
        raise RuntimeError(
            "ZoneManager requires PyO3 build with --features full. "
            "Build with: maturin develop -m rust/nexus_raft/Cargo.toml --features full"
        )

    monkeypatch.setattr(zone_manager, "ZoneManager", _raise_missing_full_build)

    nx = nexus.connect(
        config={
            "profile": "minimal",
            "data_dir": str(tmp_path / "nexus-data"),
        }
    )
    try:
        nx.sys_write("/hello.txt", b"hello")
        assert nx.sys_read("/hello.txt") == b"hello"
    finally:
        nx.close()
