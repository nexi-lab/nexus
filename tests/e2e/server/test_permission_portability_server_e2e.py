"""E2E tests for zone portability with ReBAC permissions through real FastAPI server.

Tests that permission tuples survive the export -> import cycle and work
correctly when accessed through the full HTTP server stack with
NEXUS_ENFORCE_PERMISSIONS=true.

Test flow:
1. Create source NexusFS, write files, grant ReBAC permissions
2. Export zone bundle with include_permissions=True
3. Import into fresh target NexusFS (persistent SQLite for ReBAC)
4. Start real nexus serve on target data with NEXUS_ENFORCE_PERMISSIONS=true
   and NEXUS_DATABASE_URL pointing to the target's SQLite (so ReBAC tuples
   are visible to the server subprocess)
5. Verify via HTTP RPC:
   - rebac_list_tuples returns all imported tuples
   - rebac_check confirms granted users have permission
   - rebac_check confirms unauthorized users are denied

Issue #1255: ReBAC permission export/import in portability module
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
import pytest

# Clear proxy env vars so localhost connections work
for _key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
    os.environ.pop(_key, None)
os.environ["NO_PROXY"] = "*"

PYTHON = sys.executable
SERVER_STARTUP_TIMEOUT = 30


def _find_free_port() -> int:
    """Find a free TCP port on localhost."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_health(base_url: str, timeout: float = SERVER_STARTUP_TIMEOUT) -> None:
    """Poll /health until the server responds or timeout."""
    deadline = time.monotonic() + timeout
    with httpx.Client(timeout=5) as client:
        while time.monotonic() < deadline:
            try:
                resp = client.get(f"{base_url}/health")
                if resp.status_code == 200:
                    return
            except httpx.ConnectError:
                pass
            time.sleep(0.3)
    raise TimeoutError(f"Server did not start within {timeout}s at {base_url}")


def _rpc_call(
    client: httpx.Client,
    base_url: str,
    method: str,
    params: dict,
    headers: dict,
) -> dict | list | bool | None:
    """Make an RPC call to the server and return the result."""
    resp = client.post(
        f"{base_url}/api/nfs/{method}",
        json={"jsonrpc": "2.0", "method": method, "params": params, "id": 1},
        headers=headers,
    )
    data = resp.json()
    if "error" in data:
        raise RuntimeError(f"RPC error in {method}: {data['error']}")
    return data.get("result")


def _make_nexus_fs(data_dir: Path, *, enforce_permissions: bool = False):
    """Create a NexusFS with RaftMetadataStore + persistent SQLAlchemyRecordStore.

    Uses RaftMetadataStore.embedded() for file metadata (sled KV) and
    SQLAlchemyRecordStore backed by a SQLite file for ReBAC tuples, so
    permission data persists to disk and can be shared with the server
    subprocess via NEXUS_DATABASE_URL.
    """
    from nexus.backends.local import LocalBackend
    from nexus.factory import create_nexus_fs
    from nexus.storage.raft_metadata_store import RaftMetadataStore
    from nexus.storage.record_store import SQLAlchemyRecordStore

    data_dir.mkdir(parents=True, exist_ok=True)
    db_path = data_dir / "metadata.db"

    return create_nexus_fs(
        backend=LocalBackend(data_dir),
        metadata_store=RaftMetadataStore.embedded(str(data_dir / "raft-metadata")),
        record_store=SQLAlchemyRecordStore(db_path=db_path),
        auto_parse=False,
        enforce_permissions=enforce_permissions,
    )


@pytest.fixture(scope="module")
def e2e_env():
    """Set up source -> export -> import -> start target server.

    Creates source NexusFS with files and ReBAC permissions, exports a zone
    bundle, imports into a fresh target NexusFS (with persistent SQLite), then
    starts a real nexus serve process with NEXUS_ENFORCE_PERMISSIONS=true and
    NEXUS_DATABASE_URL pointing to the target's SQLite so imported ReBAC
    tuples are accessible through the HTTP stack.

    Yields server info dict for test methods.
    """
    from nexus.core.permissions import OperationContext
    from nexus.portability import export_zone_bundle, import_zone_bundle

    with tempfile.TemporaryDirectory(prefix="nexus_perm_e2e_") as tmpdir:
        tmpdir_path = Path(tmpdir)
        source_dir = tmpdir_path / "source"
        target_dir = tmpdir_path / "target"
        source_dir.mkdir()
        target_dir.mkdir()

        # ============================================================
        # Phase 1: Create source NexusFS, write files, add permissions
        # ============================================================
        source_fs = _make_nexus_fs(source_dir)

        admin = OperationContext(user="admin", groups=[], is_admin=True)

        # Write test files
        source_fs.write(
            "/workspace/readme.md",
            b"# Permissions E2E Test",
            context=admin,
        )
        source_fs.write(
            "/workspace/src/main.py",
            b"print('hello')",
            context=admin,
        )
        source_fs.write(
            "/docs/guide.txt",
            b"User guide content",
            context=admin,
        )

        # Grant ReBAC permissions in source zone
        rebac = source_fs._rebac_manager
        assert rebac is not None, "ReBAC manager not initialized"

        # Use "direct_viewer"/"direct_editor" relations which are recognized by
        # the default file namespace permission system:
        #   "read"  → ["editor", "viewer", "owner"]
        #   "viewer" → union ["direct_viewer", "parent_viewer", "group_viewer", ...]
        #   "editor" → union ["direct_editor", "parent_editor", "group_editor", ...]
        rebac.rebac_write(
            subject=("user", "alice"),
            relation="direct_viewer",
            object=("file", "/workspace/readme.md"),
            zone_id="source-zone",
        )
        rebac.rebac_write(
            subject=("user", "bob"),
            relation="direct_editor",
            object=("file", "/workspace/src/main.py"),
            zone_id="source-zone",
        )
        rebac.rebac_write(
            subject=("group", "team-alpha"),
            relation="direct_viewer",
            object=("directory", "/workspace"),
            zone_id="source-zone",
        )

        # ============================================================
        # Phase 2: Export zone bundle with permissions
        # ============================================================
        bundle_path = tmpdir_path / "export.nexus"
        manifest = export_zone_bundle(
            nexus_fs=source_fs,
            zone_id="source-zone",
            output_path=bundle_path,
            include_content=True,
            include_permissions=True,
        )
        assert manifest.permission_count == 3, (
            f"Expected 3 permission tuples in export, got {manifest.permission_count}"
        )
        source_fs.close()

        # ============================================================
        # Phase 3: Import into target NexusFS (persistent SQLite)
        # ============================================================
        target_fs = _make_nexus_fs(target_dir)

        result = import_zone_bundle(
            nexus_fs=target_fs,
            bundle_path=bundle_path,
            target_zone_id="target-zone",
            import_permissions=True,
        )
        assert result.success is True, f"Import failed: {result.errors}"
        assert result.permissions_imported == 3, (
            f"Expected 3 permissions imported, got {result.permissions_imported}"
        )
        target_fs.close()

        # ============================================================
        # Phase 4: Start real nexus serve on target data
        #          with NEXUS_ENFORCE_PERMISSIONS=true
        #          and NEXUS_DATABASE_URL pointing to target's SQLite
        #          so the server's SQLAlchemyRecordStore can read the
        #          imported ReBAC tuples.
        # ============================================================
        port = _find_free_port()
        base_url = f"http://127.0.0.1:{port}"
        target_db_path = target_dir / "metadata.db"

        env = {
            **os.environ,
            "HTTP_PROXY": "",
            "HTTPS_PROXY": "",
            "http_proxy": "",
            "https_proxy": "",
            "NO_PROXY": "*",
            "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src"),
            # KEY: Server's SQLAlchemyRecordStore reads ReBAC from this DB
            "NEXUS_DATABASE_URL": f"sqlite:///{target_db_path}",
            # KEY: Enable permission enforcement on the server
            "NEXUS_ENFORCE_PERMISSIONS": "true",
            "NEXUS_ENFORCE_ZONE_ISOLATION": "false",
            "NEXUS_SEARCH_DAEMON": "false",
            "NEXUS_RATE_LIMIT_ENABLED": "false",
        }

        proc = subprocess.Popen(
            [
                PYTHON,
                "-c",
                (
                    "from nexus.cli import main; "
                    f"main(['serve', '--host', '127.0.0.1', '--port', '{port}', "
                    f"'--data-dir', '{target_dir}'])"
                ),
            ],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            preexec_fn=os.setsid if sys.platform != "win32" else None,
        )

        try:
            _wait_for_health(base_url)
            yield {
                "base_url": base_url,
                "port": port,
                "process": proc,
                "target_dir": str(target_dir),
            }
        except Exception:
            # Dump server output on startup failure
            if sys.platform != "win32":
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                except (ProcessLookupError, PermissionError):
                    pass
            else:
                proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=3)
            stdout = proc.stdout.read() if proc.stdout else ""
            pytest.fail(f"Server failed to start. Output:\n{stdout}")
        finally:
            # Graceful shutdown
            if proc.poll() is None:
                if sys.platform != "win32":
                    try:
                        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                    except (ProcessLookupError, PermissionError):
                        proc.terminate()
                else:
                    proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)


# =============================================================================
# Tests: Permission round-trip verification via real HTTP server
# =============================================================================


class TestPermissionPortabilityServerE2E:
    """E2E tests for permission round-trip through real FastAPI server.

    All tests use the same server instance (module-scoped fixture) to verify
    that imported ReBAC permissions are correctly accessible and enforceable
    through the full HTTP stack.
    """

    def test_server_healthy(self, e2e_env: dict) -> None:
        """Target server is healthy after starting on imported data."""
        with httpx.Client(timeout=10) as client:
            resp = client.get(f"{e2e_env['base_url']}/health")
            assert resp.status_code == 200
            assert resp.json()["status"] == "healthy"

    def test_imported_tuples_visible_via_rpc(self, e2e_env: dict) -> None:
        """rebac_list_tuples returns all 3 imported permission tuples."""
        base_url = e2e_env["base_url"]
        headers = {
            "X-Nexus-Subject": "user:admin",
            "X-Nexus-Zone-ID": "target-zone",
        }

        with httpx.Client(timeout=10) as client:
            tuples = _rpc_call(
                client,
                base_url,
                "rebac_list_tuples",
                {},
                headers,
            )

            assert isinstance(tuples, list)
            assert len(tuples) >= 3, f"Expected at least 3 tuples, got {len(tuples)}: {tuples}"

            # Verify all 3 expected subjects are present
            subjects = {(t["subject_type"], t["subject_id"]) for t in tuples}
            assert ("user", "alice") in subjects
            assert ("user", "bob") in subjects
            assert ("group", "team-alpha") in subjects

    def test_tuple_details_match_source(self, e2e_env: dict) -> None:
        """Imported tuples match exact (subject, relation, object) from source."""
        base_url = e2e_env["base_url"]
        headers = {
            "X-Nexus-Subject": "user:admin",
            "X-Nexus-Zone-ID": "target-zone",
        }

        with httpx.Client(timeout=10) as client:
            tuples = _rpc_call(
                client,
                base_url,
                "rebac_list_tuples",
                {},
                headers,
            )

            assert isinstance(tuples, list)

            # Build set of (subject_type, subject_id, relation, object_type, object_id)
            tuple_set = {
                (
                    t["subject_type"],
                    t["subject_id"],
                    t["relation"],
                    t["object_type"],
                    t["object_id"],
                )
                for t in tuples
            }

            assert ("user", "alice", "direct_viewer", "file", "/workspace/readme.md") in tuple_set
            assert ("user", "bob", "direct_editor", "file", "/workspace/src/main.py") in tuple_set
            assert ("group", "team-alpha", "direct_viewer", "directory", "/workspace") in tuple_set

    def test_rebac_check_alice_can_read(self, e2e_env: dict) -> None:
        """rebac_check confirms alice has 'read' permission on readme.md."""
        base_url = e2e_env["base_url"]
        headers = {
            "X-Nexus-Subject": "user:alice",
            "X-Nexus-Zone-ID": "target-zone",
        }

        with httpx.Client(timeout=10) as client:
            result = _rpc_call(
                client,
                base_url,
                "rebac_check",
                {
                    "subject": ["user", "alice"],
                    "permission": "read",
                    "object": ["file", "/workspace/readme.md"],
                    "zone_id": "target-zone",
                },
                headers,
            )
            assert result is True, "alice should have 'read' on readme.md via 'reader' relation"

    def test_rebac_check_bob_can_write(self, e2e_env: dict) -> None:
        """rebac_check confirms bob has 'write' permission on main.py."""
        base_url = e2e_env["base_url"]
        headers = {
            "X-Nexus-Subject": "user:bob",
            "X-Nexus-Zone-ID": "target-zone",
        }

        with httpx.Client(timeout=10) as client:
            result = _rpc_call(
                client,
                base_url,
                "rebac_check",
                {
                    "subject": ["user", "bob"],
                    "permission": "write",
                    "object": ["file", "/workspace/src/main.py"],
                    "zone_id": "target-zone",
                },
                headers,
            )
            assert result is True, "bob should have 'write' on main.py via 'writer' relation"

    def test_rebac_check_charlie_denied(self, e2e_env: dict) -> None:
        """rebac_check confirms charlie (no permissions) is denied read."""
        base_url = e2e_env["base_url"]
        headers = {
            "X-Nexus-Subject": "user:charlie",
            "X-Nexus-Zone-ID": "target-zone",
        }

        with httpx.Client(timeout=10) as client:
            result = _rpc_call(
                client,
                base_url,
                "rebac_check",
                {
                    "subject": ["user", "charlie"],
                    "permission": "read",
                    "object": ["file", "/workspace/readme.md"],
                    "zone_id": "target-zone",
                },
                headers,
            )
            assert result is False, "charlie should NOT have 'read' on readme.md"

    def test_alice_denied_on_ungranted_file(self, e2e_env: dict) -> None:
        """rebac_check confirms alice cannot write a file she has no grant for."""
        base_url = e2e_env["base_url"]
        headers = {
            "X-Nexus-Subject": "user:alice",
            "X-Nexus-Zone-ID": "target-zone",
        }

        with httpx.Client(timeout=10) as client:
            result = _rpc_call(
                client,
                base_url,
                "rebac_check",
                {
                    "subject": ["user", "alice"],
                    "permission": "write",
                    "object": ["file", "/workspace/src/main.py"],
                    "zone_id": "target-zone",
                },
                headers,
            )
            assert result is False, "alice should NOT have 'write' on main.py (only bob has writer)"
