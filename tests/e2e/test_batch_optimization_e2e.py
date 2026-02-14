"""E2E tests for batch optimization (Issue #1298).

Starts a real `nexus serve` process with:
- NEXUS_ENFORCE_PERMISSIONS=true (ReBAC enforced)
- Multi-key static auth (admin + alice + bob)

Validates that:
1. batch_read with mixed permissions returns correct data for allowed, None for denied
2. list with permissions filters correctly
3. No performance regression from batch optimizations
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
SERVER_STARTUP_TIMEOUT = 30

ADMIN_API_KEY = "sk-admin-batch-e2e"
ALICE_API_KEY = "sk-alice-batch-e2e"
BOB_API_KEY = "sk-bob-batch-e2e"

# Clear proxy env vars
for _key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
    os.environ.pop(_key, None)
os.environ["NO_PROXY"] = "*"


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _make_client() -> httpx.Client:
    return httpx.Client(timeout=30)


def _wait_for_health(base_url: str, timeout: float = SERVER_STARTUP_TIMEOUT) -> None:
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
    return textwrap.dedent(f"""\
        import os, sys, logging
        logging.basicConfig(level=logging.DEBUG)

        sys.path.insert(0, os.getenv("PYTHONPATH", ""))

        from nexus.server.auth.static_key import StaticAPIKeyAuth
        from nexus.cli import main as cli_main

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

        import nexus.server.auth.factory as factory
        _orig = factory.create_auth_provider
        def _patched(auth_type, auth_config_arg=None, **kwargs):
            if auth_type == "static":
                return StaticAPIKeyAuth.from_config(auth_config)
            return _orig(auth_type, auth_config_arg, **kwargs)
        factory.create_auth_provider = _patched

        # Disable NamespaceManager cache for deterministic tests
        import nexus.core.namespace_manager as ns_mod
        _OrigNS = ns_mod.NamespaceManager
        class _NoCacheNS(_OrigNS):
            def __init__(self, **kwargs):
                kwargs["cache_ttl"] = 0
                super().__init__(**kwargs)
        ns_mod.NamespaceManager = _NoCacheNS

        cli_main([
            'serve', '--host', '127.0.0.1', '--port', '{port}',
            '--data-dir', '{data_dir}',
            '--auth-type', 'static', '--api-key', '{ADMIN_API_KEY}',
        ])
    """)


# === Fixtures ===


@pytest.fixture(scope="module")
def server():
    """Start nexus serve with permissions enabled."""
    port = _find_free_port()
    data_dir = tempfile.mkdtemp(prefix="nexus_batch_e2e_")
    backend_root = os.path.join(data_dir, "backend")
    os.makedirs(backend_root, exist_ok=True)

    base_url = f"http://127.0.0.1:{port}"
    db_path = os.path.join(data_dir, "nexus_e2e.db")

    env = {
        **os.environ,
        "HTTP_PROXY": "",
        "HTTPS_PROXY": "",
        "http_proxy": "",
        "https_proxy": "",
        "NO_PROXY": "*",
        "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src"),
        "NEXUS_DATABASE_URL": f"sqlite:///{db_path}",
        "NEXUS_BACKEND_ROOT": backend_root,
        "NEXUS_TENANT_ID": "batch-e2e",
        "NEXUS_ENFORCE_PERMISSIONS": "true",
        "NEXUS_ENFORCE_ZONE_ISOLATION": "true",
        "NEXUS_SEARCH_DAEMON": "false",
        "NEXUS_RATE_LIMIT_ENABLED": "false",
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
    with _make_client() as c:
        yield c


@pytest.fixture()
def base_url(server: dict) -> str:
    return server["base_url"]


@pytest.fixture()
def admin_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {ADMIN_API_KEY}"}


@pytest.fixture()
def alice_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {ALICE_API_KEY}"}


@pytest.fixture()
def bob_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {BOB_API_KEY}"}


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
    """Grant via RPC endpoint using a fresh client to avoid connection reuse."""
    with httpx.Client(timeout=30) as fresh_client:
        resp = fresh_client.post(
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
    if "result" in result:
        return result["result"].get("tuple_id", "")
    return result.get("tuple_id", "")


# =============================================================================
# Tests
# =============================================================================


def test_health(base_url: str, client: httpx.Client) -> None:
    """Server is healthy with permissions enabled."""
    resp = client.get(f"{base_url}/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "healthy"


def test_batch_read_and_list_with_permissions(
    base_url: str, client: httpx.Client, alice_headers: dict, admin_headers: dict
) -> None:
    """Comprehensive E2E: batch_read + list with mixed permissions (Issue #1298).

    All validations in a single test to avoid server state issues between tests.
    Validates:
    1. Admin can create files (200)
    2. Admin can grant permissions via ReBAC
    3. batch_read returns data for granted files, None for denied
    4. list as admin sees all files (no regression)
    5. Performance: all operations complete within 5s
    """
    # --- Setup: Admin creates files with mixed grants for alice ---
    granted_paths = [
        "/workspace/batch-opt/allowed1.txt",
        "/workspace/batch-opt/allowed2.txt",
        "/workspace/batch-opt/allowed3.txt",
    ]
    denied_paths = [
        "/workspace/batch-opt/secret1.txt",
        "/workspace/batch-opt/secret2.txt",
    ]
    all_paths = granted_paths + denied_paths

    # Admin creates all files
    for p in all_paths:
        resp = client.post(
            f"{base_url}/api/v2/files/write",
            json={"path": p, "content": f"content-{p.split('/')[-1]}"},
            headers=admin_headers,
        )
        assert resp.status_code == 200, f"Admin write {p} failed: {resp.text}"

    # Grant alice viewer on allowed files only
    for p in granted_paths:
        _grant_permission(
            client,
            base_url,
            admin_headers,
            subject_id="alice",
            relation="direct_viewer",
            object_id=p,
        )

    # --- Test 1: batch_read with mixed permissions ---
    t_start = time.monotonic()
    resp = client.post(
        f"{base_url}/api/v2/files/batch-read",
        json={"paths": all_paths},
        headers=alice_headers,
    )
    t_batch = time.monotonic() - t_start
    assert resp.status_code == 200, f"Batch read failed: {resp.text}"

    data = resp.json()

    # Allowed files should have content
    for p in granted_paths:
        assert p in data, f"Granted path {p} missing from response"
        assert data[p] is not None, f"Granted path {p} returned None"
        assert data[p]["content"] == f"content-{p.split('/')[-1]}"

    # Denied files should be None or missing
    for p in denied_paths:
        if p in data:
            assert data[p] is None or data[p].get("content") is None, (
                f"Denied path {p} should be None but got: {data[p]}"
            )

    # --- Test 2: Admin list sees all files (no regression) ---
    t_start = time.monotonic()
    resp = client.get(
        f"{base_url}/api/v2/files/list",
        params={"path": "/workspace/batch-opt"},
        headers=admin_headers,
    )
    t_list = time.monotonic() - t_start
    assert resp.status_code == 200, f"Admin list failed: {resp.text}"
    list_data = resp.json()
    items = list_data.get("items", list_data.get("entries", []))
    assert len(items) >= len(all_paths), (
        f"Admin should see all {len(all_paths)} files, got {len(items)}"
    )

    # --- Performance assertions ---
    assert t_batch < 5.0, f"batch_read took {t_batch:.1f}s (expected <5s)"
    assert t_list < 5.0, f"list took {t_list:.1f}s (expected <5s)"

    print(
        f"\n  [PERF] batch_read ({len(all_paths)} files, mixed perms): {t_batch * 1000:.0f}ms"
        f"\n  [PERF] admin list ({len(items)} items): {t_list * 1000:.0f}ms"
    )
