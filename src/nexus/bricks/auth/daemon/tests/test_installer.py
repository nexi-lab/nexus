"""Tests for the per-profile platform-specific daemon installers (#3804)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from nexus.bricks.auth.daemon.installer import (
    plist_label_for,
    render_plist,
    render_systemd_unit,
    systemd_unit_name_for,
)


def test_render_plist_substitutes_values() -> None:
    rendered = render_plist(
        label="com.nexus.daemon.work",
        executable="/usr/local/bin/nexus",
        config_path=Path("/home/a/.nexus/daemons/work/daemon.toml"),
        profile="work",
        stdout_path=Path("/home/a/Library/Logs/nexus-daemon.work.out.log"),
        stderr_path=Path("/home/a/Library/Logs/nexus-daemon.work.err.log"),
    )
    assert "/usr/local/bin/nexus" in rendered
    assert "com.nexus.daemon.work" in rendered
    assert "<string>--profile</string>" in rendered
    assert "<string>work</string>" in rendered


def test_render_systemd_unit_substitutes_values() -> None:
    rendered = render_systemd_unit(
        executable="/usr/bin/nexus",
        config_path=Path("/home/b/.nexus/daemons/home/daemon.toml"),
        profile="home",
        stdout_path=Path("/home/b/.local/state/nexus/daemon/home/out.log"),
        stderr_path=Path("/home/b/.local/state/nexus/daemon/home/err.log"),
    )
    assert "ExecStart=/usr/bin/nexus daemon run --profile home" in rendered
    assert "Description=Nexus daemon (profile=home)" in rendered
    assert "[Install]" in rendered
    assert "WantedBy=default.target" in rendered


def test_label_and_unit_name_helpers() -> None:
    assert plist_label_for("work") == "com.nexus.daemon.work"
    assert plist_label_for("localhost-2026") == "com.nexus.daemon.localhost-2026"
    assert systemd_unit_name_for("work") == "nexus-daemon-work.service"
    assert systemd_unit_name_for("home") == "nexus-daemon-home.service"


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS-only")
def test_install_plist_path_on_darwin() -> None:
    from nexus.bricks.auth.daemon.installer import install_plist_path

    p = install_plist_path("work")
    assert p.name == "com.nexus.daemon.work.plist"
    assert "LaunchAgents" in str(p)


@pytest.mark.skipif(sys.platform != "linux", reason="Linux-only")
def test_install_systemd_unit_path_on_linux() -> None:
    from nexus.bricks.auth.daemon.installer import install_systemd_unit_path

    p = install_systemd_unit_path("work")
    assert p.name == "nexus-daemon-work.service"
    assert "systemd/user" in str(p)


def test_install_rejects_unsupported_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    from nexus.bricks.auth.daemon import installer

    monkeypatch.setattr(installer.sys, "platform", "win32")
    with pytest.raises(NotImplementedError, match="not supported"):
        installer.install(executable="nexus", config_path=Path("/tmp/x"), profile="work")
    with pytest.raises(NotImplementedError, match="not supported"):
        installer.uninstall(profile="work")
