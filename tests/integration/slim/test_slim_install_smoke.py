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

# rename (PAS content_id fix in meta_store/mod.rs)
fs.write(mp + "/old.txt", b"rename-me")
fs.rename(mp + "/old.txt", mp + "/new.txt")
renamed = fs.read(mp + "/new.txt")
assert renamed == b"rename-me", f"rename mismatch: {{repr(renamed)}}"

# copy (PAS content_id fix in path_local.rs)
fs.copy(mp + "/hello.txt", mp + "/copy.txt")
copied = fs.read(mp + "/copy.txt")
assert copied == b"hi from slim", f"copy mismatch: {{repr(copied)}}"

# delete
fs.delete(mp + "/hello.txt")
try:
    fs.read(mp + "/hello.txt")
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


def test_slim_local_rename_cold_cache(slim_venv: Path, tmp_path: Path) -> None:
    """Rename persists through a genuinely cold process (fresh kernel + empty DCache).

    Two separate subprocesses: the first writes and renames, then exits (kernel
    torn down, DCache gone). The second mounts fresh and verifies via stat() and
    read() that the metastore persisted the rename correctly.  Using a separate
    process is the only reliable way to ensure no warm DCache from the first
    process leaks through process-shared state.
    """
    workdir = tmp_path / "cold"
    workdir.mkdir()

    write_script = f"""
import sys
import nexus.fs

fs = nexus.fs.mount_sync("local://{workdir}")
mounts = [m for m in fs.list_mounts() if "{workdir.name}" in m]
if not mounts:
    sys.exit(f"mount not found: {{fs.list_mounts()}}")
mp = mounts[0].rstrip("/")
fs.write(mp + "/before.txt", b"cold-cache-check")
fs.rename(mp + "/before.txt", mp + "/after.txt")
print("WRITE OK")
"""

    read_script = f"""
import sys
import nexus.fs

fs = nexus.fs.mount_sync("local://{workdir}")
mounts = [m for m in fs.list_mounts() if "{workdir.name}" in m]
if not mounts:
    sys.exit(f"mount not found: {{fs.list_mounts()}}")
mp = mounts[0].rstrip("/")

# stat(before.txt) must fail — metastore must not have the old entry.
try:
    st = fs.stat(mp + "/before.txt")
    sys.exit(f"stat(before.txt) = {{st}}; old path must not exist after rename + new process")
except (FileNotFoundError, Exception) as e:
    if "not found" not in str(e).lower() and "noent" not in str(e).lower():
        if fs.stat(mp + "/before.txt") is not None:
            sys.exit(f"stat(before.txt) still returns metadata: {{e}}")

# stat(after.txt) must succeed with committed metadata.
st = fs.stat(mp + "/after.txt")
if st is None:
    sys.exit("stat(after.txt) returned None — metastore rename did not persist")
size = st.get("size", 0) if isinstance(st, dict) else getattr(st, "size", 0)
assert size == len(b"cold-cache-check"), f"stat size mismatch: {{st}}"

# read must return correct content.
content = fs.read(mp + "/after.txt")
assert content == b"cold-cache-check", f"read mismatch: {{repr(content)}}"

print("COLD OK")
"""

    r1 = run_in_slim_venv(slim_venv, write_script)
    assert r1.returncode == 0, (
        f"cold-cache WRITE phase failed:\nSTDOUT:\n{r1.stdout}\nSTDERR:\n{r1.stderr}"
    )
    assert "WRITE OK" in r1.stdout

    r2 = run_in_slim_venv(slim_venv, read_script)
    assert r2.returncode == 0, (
        f"cold-cache READ phase failed:\nSTDOUT:\n{r2.stdout}\nSTDERR:\n{r2.stderr}"
    )
    assert "COLD OK" in r2.stdout


@pytest.mark.parametrize(
    "base_module",
    [
        # Core slim modules that must import with NO extras — only base deps.
        # This catches modules shipped in the base wheel that secretly depend
        # on extras-only packages (e.g. cachetools only in [x]).
        "nexus.fs",
        "nexus.bricks.auth.oauth.pending",
        "nexus.bricks.auth.oauth.factory",
        "nexus.bricks.search.primitives.glob_helpers",
        "nexus.backends.connectors.oauth_base",
    ],
)
def test_slim_base_module_imports(slim_base_venv: Path, base_module: str) -> None:
    """Base-wheel modules must import without connector extras installed."""
    script = f"import {base_module}; print('OK')"
    result = run_in_slim_venv(slim_base_venv, script)
    assert result.returncode == 0, (
        f"base module {base_module} failed in a no-extras slim install — "
        f"it likely requires a dep that is not in base dependencies:\n"
        f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )
    assert "OK" in result.stdout


@pytest.mark.parametrize(
    "connector_module",
    [
        "nexus.backends.connectors.x.connector",
        "nexus.backends.connectors.gmail.connector",
        "nexus.backends.connectors.slack.connector",
        "nexus.backends.connectors.gdrive.connector",
        "nexus.backends.connectors.calendar.connector",
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
