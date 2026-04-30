"""End-to-end smoke: install the slim wheel into a fresh venv and
exercise local:// CRUD through the public nexus.fs facade.

Regression net for #3943 — proves a clean slim install supports
write/read/delete/mkdir/rename/copy without extra imports.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from .conftest import run_in_slim_venv


def test_slim_local_crud(slim_venv: Path, tmp_path: Path) -> None:
    """In a clean slim venv: mount local://<tmp>, perform full CRUD."""
    workdir = tmp_path / "data"
    workdir.mkdir()

    # The script runs inside the slim venv, discovers its own mount point,
    # then exercises write/read/mkdir/rename/copy/delete.
    script = f"""
import sys
import nexus.fs

fs = nexus.fs.mount_sync("local://{workdir}")

# Discover actual mount point (uri-derived path may differ from raw workdir)
entries = fs.ls("/")
mount_pts = [e for e in entries if "data" in e or "{workdir.name}" in e]
if not mount_pts:
    print("mounts:", entries, file=sys.stderr)
    sys.exit("Could not find mount point under /")
mp = mount_pts[0].rstrip("/")

# CRUD
fs.write(mp + "/hello.txt", b"hi from slim")
content = fs.read(mp + "/hello.txt")
assert content == b"hi from slim", repr(content)

fs.mkdir(mp + "/sub")
assert any("sub" in e for e in fs.ls(mp + "/")), "mkdir failed"

fs.write(mp + "/old.txt", b"old")
fs.rename(mp + "/old.txt", mp + "/new.txt")
assert fs.read(mp + "/new.txt") == b"old", "rename failed"

fs.copy(mp + "/hello.txt", mp + "/copy.txt")
assert fs.read(mp + "/copy.txt") == b"hi from slim", "copy failed"

fs.delete(mp + "/hello.txt")
try:
    fs.read(mp + "/hello.txt")
    sys.exit("expected delete to raise FileNotFoundError")
except FileNotFoundError:
    pass

print("CRUD OK")
"""
    result = run_in_slim_venv(slim_venv, script)
    assert result.returncode == 0, (
        f"slim CRUD failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )
    assert "CRUD OK" in result.stdout


@pytest.mark.parametrize(
    "connector_module",
    [
        "nexus.backends.connectors.x.connector",
        "nexus.backends.connectors.gmail.connector",
        "nexus.backends.connectors.slack.connector",
        "nexus.backends.connectors.gdrive.connector",
        "nexus.backends.connectors.calendar.connector",
        "nexus.backends.connectors.oauth_base",
    ],
)
def test_slim_connector_imports(slim_venv: Path, connector_module: str) -> None:
    """Each connector that imports nexus.bricks.* must import cleanly in slim."""
    script = f"import {connector_module}; print('OK')"
    result = run_in_slim_venv(slim_venv, script)
    assert result.returncode == 0, (
        f"importing {connector_module} failed in slim venv:\n"
        f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )
    assert "OK" in result.stdout
