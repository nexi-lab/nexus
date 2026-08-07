"""Unit coverage for ``scripts/wedge_probe.py`` — the bounded mount probe.

The probe shells out to POSIX ``stat``/``ls``/``mkdir``/``rmdir``, so it is a
macOS/Linux-only tool (the Windows FUSE E2E uses a separate PowerShell probe).
These tests exercise it against a real, always-serving directory (the happy
path) and a missing path (the failure path) for both modes.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="wedge_probe uses POSIX stat/ls/mkdir/rmdir; macOS/Linux-only tool",
)

WEDGE_PROBE = Path(__file__).resolve().parents[3] / "scripts" / "wedge_probe.py"


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(WEDGE_PROBE), *args],
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_full_probe_healthy_dir(tmp_path: Path) -> None:
    # A real directory answers stat + readdir + mkdir + rmdir within timeout.
    assert _run(str(tmp_path)).returncode == 0


def test_readonly_probe_healthy_dir(tmp_path: Path) -> None:
    assert _run(str(tmp_path), "--readonly").returncode == 0


def test_readonly_probe_is_side_effect_free(tmp_path: Path) -> None:
    # A readiness poll runs this every couple of seconds; it must not mutate
    # the mount (no leftover probe dir).
    before = set(tmp_path.iterdir())
    assert _run(str(tmp_path), "--readonly").returncode == 0
    assert set(tmp_path.iterdir()) == before


def test_readonly_probe_missing_path_fails(tmp_path: Path) -> None:
    result = _run(str(tmp_path / "does-not-exist"), "--readonly")
    assert result.returncode == 1
    assert "::error::" in result.stdout


def test_full_probe_missing_path_fails(tmp_path: Path) -> None:
    assert _run(str(tmp_path / "does-not-exist")).returncode == 1
