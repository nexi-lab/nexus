"""Tests for the platform-specific daemon installers (#3804)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from nexus.bricks.auth.daemon.installer import (
    PLIST_LABEL,
    SYSTEMD_UNIT_NAME,
    render_plist,
    render_systemd_unit,
)


def test_render_plist_substitutes_values() -> None:
    rendered = render_plist(
        executable="/usr/local/bin/nexus",
        config_path=Path("/home/a/.nexus/daemon.toml"),
        stdout_path=Path("/home/a/Library/Logs/nexus-daemon.out.log"),
        stderr_path=Path("/home/a/Library/Logs/nexus-daemon.err.log"),
    )
    assert "/usr/local/bin/nexus" in rendered
    assert "/home/a/.nexus/daemon.toml" in rendered
    assert PLIST_LABEL in rendered


def test_render_systemd_unit_substitutes_values() -> None:
    rendered = render_systemd_unit(
        executable="/usr/bin/nexus",
        config_path=Path("/home/b/.nexus/daemon.toml"),
        stdout_path=Path("/home/b/.local/state/nexus/daemon/out.log"),
        stderr_path=Path("/home/b/.local/state/nexus/daemon/err.log"),
    )
    assert "ExecStart=/usr/bin/nexus daemon run" in rendered
    assert "/home/b/.nexus/daemon.toml" in rendered
    assert "[Service]" in rendered
    assert "[Install]" in rendered
    assert "WantedBy=default.target" in rendered


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS-only")
def test_install_plist_path_on_darwin() -> None:
    from nexus.bricks.auth.daemon.installer import install_plist_path

    p = install_plist_path()
    assert p.name == "com.nexus.daemon.plist"
    assert "LaunchAgents" in str(p)


@pytest.mark.skipif(sys.platform != "linux", reason="Linux-only")
def test_install_systemd_unit_path_on_linux() -> None:
    from nexus.bricks.auth.daemon.installer import install_systemd_unit_path

    p = install_systemd_unit_path()
    assert p.name == SYSTEMD_UNIT_NAME
    assert "systemd/user" in str(p)


def test_install_rejects_unsupported_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    from nexus.bricks.auth.daemon import installer

    monkeypatch.setattr(installer.sys, "platform", "win32")
    with pytest.raises(NotImplementedError, match="not supported"):
        installer.install(executable="nexus", config_path=Path("/tmp/x"))
    with pytest.raises(NotImplementedError, match="not supported"):
        installer.uninstall()
