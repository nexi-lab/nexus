"""Tests for the macOS launchd installer (#3804)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from nexus.bricks.auth.daemon.installer import PLIST_LABEL, render_plist


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


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS-only")
def test_install_paths() -> None:
    from nexus.bricks.auth.daemon.installer import install_plist_path

    p = install_plist_path()
    assert p.name == "com.nexus.daemon.plist"
    assert "LaunchAgents" in str(p)
