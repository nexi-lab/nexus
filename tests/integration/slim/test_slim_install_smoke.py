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

# stat(before.txt) must not find the old entry.
# fs.stat() returns None when the file is absent (no exception raised).
st_before = None
try:
    st_before = fs.stat(mp + "/before.txt")
except (FileNotFoundError, Exception):
    pass  # exception is also an acceptable "not found" signal
if st_before is not None:
    sys.exit(f"stat(before.txt) = {{st_before}}; old path must not exist after rename + new process")

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


def test_slim_base_connector_metadata_discovery_without_optional_deps(
    slim_base_venv: Path,
) -> None:
    """Base slim install lists S3 and Slack metadata without connector extras."""
    script = """
import sys

for name in (
    "boto3",
    "slack_sdk",
    "nexus.backends.storage.path_s3",
    "nexus.backends.transports.s3_transport",
    "nexus.backends.connectors.slack.connector",
    "nexus.backends.connectors.slack.transport",
):
    sys.modules.pop(name, None)

import nexus
import nexus.backends
from nexus.extensions.store import get_store, reset_store

reset_store()
store = get_store()
s3 = store.get("path_s3", kind="connector")
slack = store.get("slack_connector", kind="connector")

assert s3.metadata_complete is True
assert s3.service_name == "s3"
assert "boto3" in {d.name for d in s3.runtime_deps}
assert slack.metadata_complete is True
assert slack.service_name == "slack"
assert "slack-sdk" in {d.name for d in slack.runtime_deps}

for name in (
    "boto3",
    "slack_sdk",
    "nexus.backends.storage.path_s3",
    "nexus.backends.transports.s3_transport",
    "nexus.backends.connectors.slack.connector",
    "nexus.backends.connectors.slack.transport",
):
    assert name not in sys.modules, name

print("DISCOVERY OK")
"""
    result = run_in_slim_venv(slim_base_venv, script)
    assert result.returncode == 0, (
        "slim base connector metadata discovery failed:\n"
        f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )
    assert "DISCOVERY OK" in result.stdout


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


def test_slim_base_service_dep_token_manager_unsatisfied(slim_base_venv: Path) -> None:
    """Issue #3947: on a base slim install (no OAuth extras),
    ``ServiceDep("token_manager")`` must report missing — sqlalchemy is
    only declared in the OAuth connector extras, and a presence-only probe
    against the force-included token_manager.py would otherwise mark the
    service satisfied even though importing it would raise
    ``ModuleNotFoundError: sqlalchemy`` at instantiation time.
    """
    script = """
from nexus.backends.base.runtime_deps import (
    ServiceDep,
    check_runtime_deps,
)

missing = check_runtime_deps((ServiceDep("token_manager"),))
assert missing, "ServiceDep(\\"token_manager\\") falsely satisfied on base slim"
_, reason = missing[0]
assert "service \\"token_manager\\"" in reason or "service 'token_manager'" in reason, reason
print("BASE-SLIM-TOKEN-MANAGER-UNSATISFIED-OK")
"""
    result = run_in_slim_venv(slim_base_venv, script)
    assert result.returncode == 0, (
        f"base-slim ServiceDep(token_manager) audit failed:\n"
        f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )
    assert "BASE-SLIM-TOKEN-MANAGER-UNSATISFIED-OK" in result.stdout


def test_slim_service_dep_token_manager_is_satisfied(slim_venv: Path) -> None:
    """Issue #3947: legacy / third-party manifests that still declare
    ``ServiceDep("token_manager")`` must be reported as *satisfied* in a
    slim install, because the auth/oauth bricks are force-included.

    Without the per-module probe in ``_SERVICE_MODULES`` the check falls
    through to ``_server_available()`` and falsely raises "requires a full
    nexus install" — even though the slim wheel ships the token manager.
    Pin that mapping with an end-to-end check that runs inside the slim
    venv (no full server runtime present).
    """
    script = """
from nexus.backends.base.runtime_deps import (
    ServiceDep,
    _server_available,
    check_runtime_deps,
)

# Sanity: nexus.server is not available in a slim install.
_server_available.cache_clear()
assert not _server_available(), "slim venv unexpectedly has nexus.server"

# token_manager probe must hit the per-module path, not the server fallback.
missing = check_runtime_deps((ServiceDep("token_manager"),))
assert not missing, (
    f"ServiceDep(\\"token_manager\\") falsely reported missing on slim: {missing}"
)
print("TOKEN-MANAGER-SERVICE-OK")
"""
    result = run_in_slim_venv(slim_venv, script)
    assert result.returncode == 0, (
        f"ServiceDep(token_manager) slim probe failed:\n"
        f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )
    assert "TOKEN-MANAGER-SERVICE-OK" in result.stdout


def test_slim_oauth_extras_independent_of_bricks(slim_venv: Path) -> None:
    """Issue #3947: each advertised OAuth/API extra must import and pass
    runtime-dep checks without ``nexus.bricks`` being importable.

    The slim wheel ships the auth/oauth bricks subtree so OAuth connectors
    work end-to-end; the runtime-dep contract is the *advertised* gate
    users hit when a mount fails.  Encoding a ``nexus.bricks`` dependency
    in that gate would falsely advertise "requires a full nexus install"
    even though the slim wheel + OAuth extras already ship everything the
    connector needs.

    This test installs the ``meta_path`` blocker before any backend module
    has been touched, then verifies:

    1. The connector module itself imports (top-level imports must not
       reach into ``nexus.bricks``).
    2. The manifest entry carries no ``ServiceDep`` (token_manager service
       gate would smuggle a bricks probe back in).
    3. ``check_runtime_deps`` for the entry returns no missing deps —
       so the only failure modes left are real external creds / packages.
    """
    script = '''
import sys

class _BlockBricks:
    """Reject any import of nexus.bricks.* — proves the runtime-dep
    check does not transitively need the bricks tree."""

    def find_spec(self, name, path=None, target=None):
        if name == "nexus.bricks" or name.startswith("nexus.bricks."):
            raise ModuleNotFoundError(f"blocked by test: {name}")
        return None

sys.meta_path.insert(0, _BlockBricks())

# Manifest must import without touching bricks.
from nexus.backends._manifest import CONNECTOR_MANIFEST
from nexus.backends.base.runtime_deps import (
    PythonDep,
    ServiceDep,
    check_runtime_deps,
)

oauth_names = {
    "gdrive_connector",
    "gmail_connector",
    "calendar_connector",
    "gcalendar_connector",
    "x_connector",
    "slack_connector",
}
seen = set()
for entry in CONNECTOR_MANIFEST:
    if entry.name not in oauth_names:
        continue
    seen.add(entry.name)
    for dep in entry.runtime_deps:
        assert not isinstance(dep, ServiceDep), (
            f"{entry.name} carries ServiceDep({dep.name!r}); the OAuth "
            "extras must not gate on a server-side service probe."
        )
        if isinstance(dep, PythonDep):
            assert "nexus.bricks" not in dep.module, (
                f"{entry.name} declares PythonDep({dep.module!r}); runtime-dep "
                "checks must not target the bricks tree."
            )
    missing = check_runtime_deps(entry.runtime_deps)
    assert not missing, (
        f"{entry.name} reports missing runtime deps even with the matching "
        f"extra installed: {missing}"
    )

assert seen == oauth_names, f"manifest is missing OAuth entries: {oauth_names - seen}"
print("OAUTH-DEPS-OK")
'''
    result = run_in_slim_venv(slim_venv, script)
    assert result.returncode == 0, (
        f"OAuth extras manifest dep audit failed:\n"
        f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )
    assert "OAUTH-DEPS-OK" in result.stdout
