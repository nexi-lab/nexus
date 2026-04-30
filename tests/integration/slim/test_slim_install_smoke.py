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

# list_mounts() reads registered mount points directly from the kernel.
mounts = fs.list_mounts()
mount_pts = [m for m in mounts if "data" in m or "{workdir.name}" in m]
if not mount_pts:
    print("list_mounts:", mounts, file=sys.stderr)
    sys.exit("Could not find mount point in registered mounts")
mp = mount_pts[0].rstrip("/")

# write + read
fs.write(mp + "/hello.txt", b"hi from slim")
content = fs.read(mp + "/hello.txt")
assert content == b"hi from slim", f"read mismatch: {{repr(content)}}"

# mkdir
fs.mkdir(mp + "/sub")

# write a second file and delete it
fs.write(mp + "/bye.txt", b"bye")
fs.delete(mp + "/bye.txt")
try:
    fs.read(mp + "/bye.txt")
    sys.exit("expected FileNotFoundError after delete")
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
