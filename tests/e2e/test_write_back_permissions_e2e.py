"""E2E tests for write-back with PERMISSIONS ENABLED for normal users (Issue #1129).

Starts a real `nexus serve` process with:
- NEXUS_ENFORCE_PERMISSIONS=true (ReBAC enforced)
- NEXUS_WRITE_BACK=true (bidirectional sync enabled)
- Multi-key static auth (admin + alice + bob)

Tests the full file CRUD pipeline as a normal (non-admin) user:
1. Admin grants permissions -> normal user can write/read/delete
2. Viewer grant -> can read but NOT write (403)
3. Editor grant -> full CRUD (200)
4. No grant -> invisible (404)
5. Write-back service active during all operations
"""

from __future__ import annotations

import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path

import httpx
import pytest

# === Helpers ===

PYTHON = sys.executable
SERVER_STARTUP_TIMEOUT = 30  # seconds

# API keys for multi-user auth
ADMIN_API_KEY = "sk-admin-e2e-key"
ALICE_API_KEY = "sk-alice-e2e-key"
BOB_API_KEY = "sk-bob-e2e-key"

# Clear proxy env vars so localhost connections work
for _key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
    os.environ.pop(_key, None)
os.environ["NO_PROXY"] = "*"


def _find_free_port() -> int:
    """Find a free TCP port on localhost."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _make_client() -> httpx.Client:
    """Create httpx client for localhost connections."""
    return httpx.Client(timeout=10)


def _wait_for_health(base_url: str, timeout: float = SERVER_STARTUP_TIMEOUT) -> None:
    """Poll /health until the server responds or timeout."""
    deadline = time.monotonic() + timeout
    with _make_client() as client:
        while time.monotonic() < deadline:
            try:
                resp = client.get(f"{base_url}/health")
                if resp.status_code == 200:
                    return
            except httpx.ConnectError:
                pass
            time.sleep(0.3)
    raise TimeoutError(f"Server did not start within {timeout}s at {base_url}")


def _build_startup_script(port: int, data_dir: str) -> str:
    """Build a Python startup script that creates the app with multi-key auth.

    Uses StaticAPIKeyAuth with 3 API keys (admin, alice, bob) so each user
    can authenticate independently and get the correct is_admin status.
    """
    return textwrap.dedent(f"""\
        import os, sys, logging
        logging.basicConfig(level=logging.INFO)

        # Setup paths
        sys.path.insert(0, os.getenv("PYTHONPATH", ""))

        from nexus.server.auth.static_key import StaticAPIKeyAuth
        from nexus.cli import main as cli_main

        # Create multi-key auth provider BEFORE starting the CLI
        # The CLI will pick up the auth via the global _auth_override
        auth_config = {{
            "api_keys": {{
                "{ADMIN_API_KEY}": {{
                    "subject_type": "user",
                    "subject_id": "admin",
                    "zone_id": "test",
                    "is_admin": True,
                }},
                "{ALICE_API_KEY}": {{
                    "subject_type": "user",
                    "subject_id": "alice",
                    "zone_id": "test",
                    "is_admin": False,
                }},
                "{BOB_API_KEY}": {{
                    "subject_type": "user",
                    "subject_id": "bob",
                    "zone_id": "test",
                    "is_admin": False,
                }},
            }}
        }}

        # Monkey-patch the factory to return our multi-key provider
        import nexus.server.auth.factory as factory
        _orig = factory.create_auth_provider
        def _patched(auth_type, auth_config_arg=None, **kwargs):
            if auth_type == "static":
                return StaticAPIKeyAuth.from_config(auth_config)
            return _orig(auth_type, auth_config_arg, **kwargs)
        factory.create_auth_provider = _patched

        # Use revision_window=1 so every rebac_write() triggers cache
        # invalidation immediately (bounded staleness ≤ 1 revision).
        # All 3 cache layers (L1 dcache, L2 mount table, L3 persistent view)
        # remain ENABLED — we're testing the real production cache path.
        import nexus.rebac.namespace_manager as ns_mod
        _OrigNS = ns_mod.NamespaceManager
        class _TightRevisionNS(_OrigNS):
            def __init__(self, **kwargs):
                kwargs["revision_window"] = 1  # Every write invalidates
                super().__init__(**kwargs)
        ns_mod.NamespaceManager = _TightRevisionNS
        # Also patch the factory module which imports NamespaceManager at module
        # load time (its own binding won't see the ns_mod patch above).
        import nexus.rebac.namespace_factory as nf_mod
        nf_mod.NamespaceManager = _TightRevisionNS

        cli_main([
            'serve', '--host', '127.0.0.1', '--port', '{port}',
            '--data-dir', '{data_dir}',
            '--auth-type', 'static', '--api-key', '{ADMIN_API_KEY}',
        ])
    """)


# === Fixtures ===


@pytest.fixture(scope="module")
def server():
    """Start a real nexus serve process WITH permissions AND write-back enabled.

    Uses multi-key static auth: admin (sk-admin-*), alice (sk-alice-*), bob (sk-bob-*).
    Each user authenticates via Authorization: Bearer <key> header.
    """
    port = _find_free_port()
    data_dir = tempfile.mkdtemp(prefix="nexus_wb_perm_e2e_")
    backend_root = os.path.join(data_dir, "backend")
    os.makedirs(backend_root, exist_ok=True)

    base_url = f"http://127.0.0.1:{port}"

    # SQLite database for async engine (required by AsyncNexusFS)
    db_path = os.path.join(data_dir, "nexus_e2e.db")

    env = {
        **os.environ,
        # Clear proxies
        "HTTP_PROXY": "",
        "HTTPS_PROXY": "",
        "http_proxy": "",
        "https_proxy": "",
        "NO_PROXY": "*",
        # Source code on PYTHONPATH
        "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src"),
        # Database URL for async engine (AsyncNexusFS requires this)
        "NEXUS_DATABASE_URL": f"sqlite:///{db_path}",
        # AsyncNexusFS settings
        "NEXUS_BACKEND_ROOT": backend_root,
        "NEXUS_TENANT_ID": "wb-perm-e2e",
        # CRITICAL: Permissions ENABLED
        "NEXUS_ENFORCE_PERMISSIONS": "true",
        "NEXUS_ENFORCE_ZONE_ISOLATION": "true",
        # CRITICAL: Write-back ENABLED (bidirectional sync)
        "NEXUS_WRITE_BACK": "true",
        # Disable non-essential services
        "NEXUS_SEARCH_DAEMON": "false",
        "NEXUS_RATE_LIMIT_ENABLED": "false",
        # Tight revision window for deterministic permission tests.
        # Caches remain enabled; every rebac_write() advances the revision
        # bucket so stale entries are evicted immediately.
        "NEXUS_NAMESPACE_REVISION_WINDOW": "1",
    }

    startup_script = _build_startup_script(port, data_dir)

    proc = subprocess.Popen(
        [PYTHON, "-c", startup_script],
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
            "data_dir": data_dir,
            "process": proc,
        }
    except Exception:
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
        shutil.rmtree(data_dir, ignore_errors=True)


@pytest.fixture(scope="module")
def client(server: dict) -> httpx.Client:
    """Shared httpx client."""
    with _make_client() as c:
        yield c


@pytest.fixture()
def base_url(server: dict) -> str:
    """Get the base URL of the running server."""
    return server["base_url"]


@pytest.fixture()
def alice_headers() -> dict[str, str]:
    """Headers for normal user alice (authenticated via API key)."""
    return {
        "Authorization": f"Bearer {ALICE_API_KEY}",
    }


@pytest.fixture()
def bob_headers() -> dict[str, str]:
    """Headers for normal user bob (authenticated via API key)."""
    return {
        "Authorization": f"Bearer {BOB_API_KEY}",
    }


@pytest.fixture()
def admin_headers() -> dict[str, str]:
    """Headers for admin user (authenticated via API key, is_admin=True)."""
    return {
        "Authorization": f"Bearer {ADMIN_API_KEY}",
    }


def _grant_permission(
    client: httpx.Client,
    base_url: str,
    admin_headers: dict,
    *,
    subject_id: str,
    relation: str,
    object_id: str,
    zone_id: str = "test",
) -> str:
    """Grant a ReBAC permission tuple via RPC endpoint. Returns tuple_id."""
    resp = client.post(
        f"{base_url}/api/nfs/rebac_create",
        json={
            "method": "rebac_create",
            "params": {
                "subject": ["user", subject_id],
                "relation": relation,
                "object": ["file", object_id],
                "zone_id": zone_id,
            },
        },
        headers=admin_headers,
    )
    assert resp.status_code == 200, f"Grant failed: {resp.text}"
    result = resp.json()
    # Small delay for permission propagation (namespace cache invalidation)
    time.sleep(0.1)
    # RPC response wraps result in "result" key
    if "result" in result:
        return result["result"].get("tuple_id", "")
    return result.get("tuple_id", "")


# =============================================================================
# Tests: Normal User Write with Permissions
# =============================================================================


def test_health_with_write_back(base_url: str, client: httpx.Client) -> None:
    """Server is healthy with write-back and permissions enabled."""
    resp = client.get(f"{base_url}/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "healthy"


def test_normal_user_no_grant_gets_404(
    base_url: str, client: httpx.Client, alice_headers: dict
) -> None:
    """Normal user with no grants gets 404 (fail-closed namespace)."""
    resp = client.get(
        f"{base_url}/api/v2/files/read",
        params={"path": "/workspace/ungrantable.txt"},
        headers=alice_headers,
    )
    assert resp.status_code == 404


def test_editor_can_write_read_delete(
    base_url: str, client: httpx.Client, alice_headers: dict, admin_headers: dict
) -> None:
    """Normal user with direct_editor grant can do full CRUD.

    Flow:
    1. Admin creates file
    2. Admin grants alice direct_editor
    3. Alice reads the file (200)
    4. Alice overwrites the file (200)
    5. Alice reads updated content (200)
    6. Alice deletes the file (200)
    """
    file_path = "/workspace/editor-crud/doc.txt"

    # Admin creates the file
    resp = client.post(
        f"{base_url}/api/v2/files/write",
        json={"path": file_path, "content": "original"},
        headers=admin_headers,
    )
    assert resp.status_code == 200, f"Admin write failed: {resp.text}"

    # Admin grants alice direct_editor (read + write)
    _grant_permission(
        client,
        base_url,
        admin_headers,
        subject_id="alice",
        relation="direct_editor",
        object_id=file_path,
    )

    # Alice can read
    resp = client.get(
        f"{base_url}/api/v2/files/read",
        params={"path": file_path},
        headers=alice_headers,
    )
    assert resp.status_code == 200, f"Alice read failed: {resp.text}"
    assert resp.json()["content"] == "original"

    # Alice can overwrite
    resp = client.post(
        f"{base_url}/api/v2/files/write",
        json={"path": file_path, "content": "updated by alice"},
        headers=alice_headers,
    )
    assert resp.status_code == 200, f"Alice write failed: {resp.text}"

    # Alice reads updated content
    resp = client.get(
        f"{base_url}/api/v2/files/read",
        params={"path": file_path},
        headers=alice_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["content"] == "updated by alice"

    # Alice can delete
    resp = client.delete(
        f"{base_url}/api/v2/files/delete",
        params={"path": file_path},
        headers=alice_headers,
    )
    assert resp.status_code == 200, f"Alice delete failed: {resp.text}"
    assert resp.json()["deleted"] is True


def test_viewer_can_read_but_not_write(
    base_url: str, client: httpx.Client, alice_headers: dict, admin_headers: dict
) -> None:
    """Normal user with direct_viewer cannot write (403).

    Viewer grant = read-only. Write attempt should return 403 (permission denied,
    not 404 since the path IS visible to the viewer).
    """
    file_path = "/workspace/viewer-only/readme.txt"

    # Admin creates file
    resp = client.post(
        f"{base_url}/api/v2/files/write",
        json={"path": file_path, "content": "read only content"},
        headers=admin_headers,
    )
    assert resp.status_code == 200

    # Grant alice viewer (read-only)
    _grant_permission(
        client,
        base_url,
        admin_headers,
        subject_id="alice",
        relation="direct_viewer",
        object_id=file_path,
    )

    # Alice can read
    resp = client.get(
        f"{base_url}/api/v2/files/read",
        params={"path": file_path},
        headers=alice_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["content"] == "read only content"

    # Alice CANNOT write (403)
    resp = client.post(
        f"{base_url}/api/v2/files/write",
        json={"path": file_path, "content": "hacked"},
        headers=alice_headers,
    )
    assert resp.status_code == 403, f"Expected 403, got {resp.status_code}: {resp.text}"


def test_owner_has_full_control(
    base_url: str, client: httpx.Client, alice_headers: dict, admin_headers: dict
) -> None:
    """Normal user with direct_owner has full control.

    Owner = read + write + execute + permission management.
    Tests: write, read, metadata, delete.
    """
    file_path = "/workspace/owner-test/owned.txt"

    # Admin creates the file
    resp = client.post(
        f"{base_url}/api/v2/files/write",
        json={"path": file_path, "content": "owned by alice"},
        headers=admin_headers,
    )
    assert resp.status_code == 200

    # Grant alice direct_owner
    _grant_permission(
        client,
        base_url,
        admin_headers,
        subject_id="alice",
        relation="direct_owner",
        object_id=file_path,
    )

    # Alice can read
    resp = client.get(
        f"{base_url}/api/v2/files/read",
        params={"path": file_path},
        headers=alice_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["content"] == "owned by alice"

    # Alice can write
    resp = client.post(
        f"{base_url}/api/v2/files/write",
        json={"path": file_path, "content": "updated by owner"},
        headers=alice_headers,
    )
    assert resp.status_code == 200

    # Alice can get metadata
    resp = client.get(
        f"{base_url}/api/v2/files/metadata",
        params={"path": file_path},
        headers=alice_headers,
    )
    assert resp.status_code == 200
    meta = resp.json()
    assert meta["path"] == file_path
    assert meta["version"] == 2

    # Alice can check existence
    resp = client.get(
        f"{base_url}/api/v2/files/exists",
        params={"path": file_path},
        headers=alice_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["exists"] is True

    # Alice can delete
    resp = client.delete(
        f"{base_url}/api/v2/files/delete",
        params={"path": file_path},
        headers=alice_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["deleted"] is True


def test_cross_user_isolation(
    base_url: str,
    client: httpx.Client,
    alice_headers: dict,
    bob_headers: dict,
    admin_headers: dict,
) -> None:
    """Alice's files are invisible/inaccessible to Bob and vice versa.

    Uses SEPARATE parent directories so namespace visibility (which operates
    at the parent-directory level) correctly isolates each user's namespace.
    """
    alice_file = "/workspace/alice-private/data.txt"
    bob_file = "/workspace/bob-private/data.txt"

    # Admin creates both files
    for path, content in [(alice_file, "alice secret"), (bob_file, "bob secret")]:
        resp = client.post(
            f"{base_url}/api/v2/files/write",
            json={"path": path, "content": content},
            headers=admin_headers,
        )
        assert resp.status_code == 200

    # Grant alice editor on her file, bob editor on his
    _grant_permission(
        client,
        base_url,
        admin_headers,
        subject_id="alice",
        relation="direct_editor",
        object_id=alice_file,
    )
    _grant_permission(
        client,
        base_url,
        admin_headers,
        subject_id="bob",
        relation="direct_editor",
        object_id=bob_file,
    )

    # Alice reads her file
    resp = client.get(
        f"{base_url}/api/v2/files/read",
        params={"path": alice_file},
        headers=alice_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["content"] == "alice secret"

    # Alice cannot see Bob's file (404 = invisible, different parent dir)
    resp = client.get(
        f"{base_url}/api/v2/files/read",
        params={"path": bob_file},
        headers=alice_headers,
    )
    assert resp.status_code == 404

    # Bob reads his file
    resp = client.get(
        f"{base_url}/api/v2/files/read",
        params={"path": bob_file},
        headers=bob_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["content"] == "bob secret"

    # Bob cannot see Alice's file (404 = invisible, different parent dir)
    resp = client.get(
        f"{base_url}/api/v2/files/read",
        params={"path": alice_file},
        headers=bob_headers,
    )
    assert resp.status_code == 404

    # Alice can write to her file (editor grant)
    resp = client.post(
        f"{base_url}/api/v2/files/write",
        json={"path": alice_file, "content": "alice updated"},
        headers=alice_headers,
    )
    assert resp.status_code == 200

    # Bob can write to his file (editor grant)
    resp = client.post(
        f"{base_url}/api/v2/files/write",
        json={"path": bob_file, "content": "bob updated"},
        headers=bob_headers,
    )
    assert resp.status_code == 200


def test_editor_write_then_version_increments(
    base_url: str, client: httpx.Client, alice_headers: dict, admin_headers: dict
) -> None:
    """Normal user write increments version correctly.

    Verifies that the file versioning works end-to-end when a normal
    user (with editor grant) writes multiple times.
    """
    file_path = "/workspace/versioning/tracker.txt"

    # Admin creates initial version
    resp = client.post(
        f"{base_url}/api/v2/files/write",
        json={"path": file_path, "content": "v1"},
        headers=admin_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["version"] == 1

    # Grant alice editor
    _grant_permission(
        client,
        base_url,
        admin_headers,
        subject_id="alice",
        relation="direct_editor",
        object_id=file_path,
    )

    # Alice writes v2, v3
    for i in range(2, 4):
        resp = client.post(
            f"{base_url}/api/v2/files/write",
            json={"path": file_path, "content": f"v{i}"},
            headers=alice_headers,
        )
        assert resp.status_code == 200, f"Write v{i} failed: {resp.text}"
        assert resp.json()["version"] == i

    # Alice reads latest
    resp = client.get(
        f"{base_url}/api/v2/files/read",
        params={"path": file_path},
        headers=alice_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["content"] == "v3"


def test_batch_read_with_permission(
    base_url: str, client: httpx.Client, alice_headers: dict, admin_headers: dict
) -> None:
    """Normal user can batch-read files they have permission for.

    Admin creates 2 files, grants alice viewer on both, then alice
    batch-reads them successfully.
    """
    paths = ["/workspace/batch/file1.txt", "/workspace/batch/file2.txt"]

    # Admin creates files
    for p in paths:
        resp = client.post(
            f"{base_url}/api/v2/files/write",
            json={"path": p, "content": f"content-{p}"},
            headers=admin_headers,
        )
        assert resp.status_code == 200

    # Grant alice viewer on both
    for p in paths:
        _grant_permission(
            client,
            base_url,
            admin_headers,
            subject_id="alice",
            relation="direct_viewer",
            object_id=p,
        )

    # Alice batch-reads
    resp = client.post(
        f"{base_url}/api/v2/files/batch-read",
        json={"paths": paths},
        headers=alice_headers,
    )
    assert resp.status_code == 200, f"Batch read failed: {resp.text}"
    data = resp.json()
    for p in paths:
        assert p in data
        assert data[p]["content"] == f"content-{p}"
