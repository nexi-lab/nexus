"""E2E test for L3 persistent namespace views with real server (Issue #1265).

Verifies that:
1. Server starts with L3 persistent view store enabled
2. Namespace visibility works correctly for users with permissions
3. L3 doesn't break the existing namespace isolation behavior

Uses multi-key StaticAPIKeyAuth for proper per-user identity, and the sync
RPC endpoint (/api/nfs/) which doesn't require AsyncNexusFS (database_url).
"""

from __future__ import annotations

import base64
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import textwrap
import time
from collections.abc import Generator
from pathlib import Path
from typing import Any

import httpx
import pytest

# === Constants ===

PYTHON = sys.executable
SERVER_STARTUP_TIMEOUT = 30  # seconds

# API keys for multi-user auth (must start with sk-)
ADMIN_API_KEY = "sk-admin-l3-e2e"
ALICE_API_KEY = "sk-alice-l3-e2e"
BOB_API_KEY = "sk-bob-l3-e2e"

# Clear proxy env vars so localhost connections work
for _key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
    os.environ.pop(_key, None)
os.environ["NO_PROXY"] = "*"


# === Helpers ===


def _find_free_port() -> int:
    """Find a free TCP port on localhost."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _make_client() -> httpx.Client:
    """Create httpx client for localhost connections (no proxy)."""
    return httpx.Client(timeout=15, trust_env=False)


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


def _rpc_call(
    client: httpx.Client,
    base_url: str,
    method: str,
    params: dict[str, Any],
    headers: dict[str, str],
) -> dict[str, Any]:
    """Make an RPC call to /api/nfs/{method} and return response JSON."""
    resp = client.post(
        f"{base_url}/api/nfs/{method}",
        json={"method": method, "params": params},
        headers=headers,
    )
    return {"status_code": resp.status_code, "body": resp.json()}


def _rpc_write(
    client: httpx.Client,
    base_url: str,
    path: str,
    content: str,
    headers: dict[str, str],
) -> dict[str, Any]:
    """Write a file via RPC. Content is base64-encoded for transport."""
    encoded = base64.b64encode(content.encode("utf-8")).decode("utf-8")
    return _rpc_call(
        client,
        base_url,
        "write",
        {"path": path, "content": {"__type__": "bytes", "data": encoded}},
        headers,
    )


def _rpc_read(
    client: httpx.Client,
    base_url: str,
    path: str,
    headers: dict[str, str],
) -> dict[str, Any]:
    """Read a file via RPC. Returns raw response dict."""
    return _rpc_call(client, base_url, "read", {"path": path}, headers)


def _rpc_grant(
    client: httpx.Client,
    base_url: str,
    subject_id: str,
    path: str,
    headers: dict[str, str],
    zone_id: str = "test",
) -> dict[str, Any]:
    """Grant direct_viewer permission via rebac_create RPC."""
    return _rpc_call(
        client,
        base_url,
        "rebac_create",
        {
            "subject": ["user", subject_id],
            "relation": "direct_viewer",
            "object": ["file", path],
            "zone_id": zone_id,
        },
        headers,
    )


def _build_startup_script(port: int, data_dir: str) -> str:
    """Build Python startup script with multi-key auth and no-cache namespace."""
    return textwrap.dedent(f"""\
        import os, sys, logging
        logging.basicConfig(level=logging.INFO)
        sys.path.insert(0, os.getenv("PYTHONPATH", ""))

        from nexus.server.auth.static_key import StaticAPIKeyAuth
        from nexus.cli import main as cli_main

        # Multi-key auth config
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

        # Patch auth factory to use multi-key provider
        import nexus.server.auth.factory as factory
        _orig = factory.create_auth_provider
        def _patched(auth_type, auth_config_arg=None, **kwargs):
            if auth_type == "static":
                return StaticAPIKeyAuth.from_config(auth_config)
            return _orig(auth_type, auth_config_arg, **kwargs)
        factory.create_auth_provider = _patched

        # Disable namespace cache for deterministic tests
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
def server() -> Generator[dict[str, Any], None, None]:
    """Start a real nexus serve process with PERMISSIONS ENABLED + L3.

    Uses multi-key StaticAPIKeyAuth so each user (admin, alice, bob)
    authenticates independently via Bearer token.
    """
    port = _find_free_port()
    data_dir = tempfile.mkdtemp(prefix="nexus_l3_e2e_")
    backend_root = os.path.join(data_dir, "backend")
    os.makedirs(backend_root, exist_ok=True)

    base_url = f"http://127.0.0.1:{port}"

    # SQLite database for record store (creates rebac_check_cache, etc.)
    db_path = os.path.join(data_dir, "nexus_l3_e2e.db")

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
        "NEXUS_TENANT_ID": "l3-e2e-test",
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
def client(server: dict[str, Any]) -> Generator[httpx.Client, None, None]:
    """Shared httpx client (no proxy)."""
    with _make_client() as c:
        yield c


@pytest.fixture()
def base_url(server: dict[str, Any]) -> str:
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


# =============================================================================
# Tests: L3 Persistent Views with Real Server
# =============================================================================


def test_health_shows_server_ready(base_url: str, client: httpx.Client) -> None:
    """Health endpoint responds and server is ready."""
    resp = client.get(f"{base_url}/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "healthy"


def test_zero_grants_invisible(
    base_url: str, client: httpx.Client, alice_headers: dict[str, str]
) -> None:
    """User with no grants sees nothing (fail-closed, even with L3)."""
    result = _rpc_read(client, base_url, "/workspace/secret.txt", alice_headers)
    # RPC returns 200 with error in body, or 404/403
    # With permissions enforced: file not visible → NexusFileNotFoundError
    body = result["body"]
    # RPC wraps errors in {"error": ...}
    assert "error" in body or result["status_code"] != 200, (
        f"Expected error for invisible path, got: {body}"
    )


def test_namespace_isolation_with_l3(
    base_url: str,
    client: httpx.Client,
    admin_headers: dict[str, str],
    alice_headers: dict[str, str],
    bob_headers: dict[str, str],
) -> None:
    """Per-subject namespace isolation works with L3 enabled.

    1. Admin creates files for alice and bob
    2. Admin grants viewer permissions via ReBAC
    3. Alice sees only her files, bob sees only his
    4. Unmounted paths return error (invisible)
    """
    alice_path = "/workspace/alice-l3-proj/data.txt"
    bob_path = "/workspace/bob-l3-proj/data.txt"

    # Admin creates files
    result = _rpc_write(client, base_url, alice_path, "Alice's L3 data", admin_headers)
    assert result["status_code"] == 200, f"Admin write alice failed: {result}"

    result = _rpc_write(client, base_url, bob_path, "Bob's L3 data", admin_headers)
    assert result["status_code"] == 200, f"Admin write bob failed: {result}"

    # Grant alice viewer-of alice_path
    result = _rpc_grant(client, base_url, "alice", alice_path, admin_headers)
    assert result["status_code"] == 200, f"Grant alice failed: {result}"

    # Grant bob viewer-of bob_path
    result = _rpc_grant(client, base_url, "bob", bob_path, admin_headers)
    assert result["status_code"] == 200, f"Grant bob failed: {result}"

    # Alice can read her file
    result = _rpc_read(client, base_url, alice_path, alice_headers)
    assert result["status_code"] == 200, f"Alice should see {alice_path}: {result}"
    assert "error" not in result["body"], f"Alice read error: {result['body']}"

    # Alice cannot see Bob's file
    result = _rpc_read(client, base_url, bob_path, alice_headers)
    body = result["body"]
    assert "error" in body, f"Alice should NOT see {bob_path}: {body}"

    # Bob can read his file
    result = _rpc_read(client, base_url, bob_path, bob_headers)
    assert result["status_code"] == 200, f"Bob should see {bob_path}: {result}"
    assert "error" not in result["body"], f"Bob read error: {result['body']}"

    # Bob cannot see Alice's file
    result = _rpc_read(client, base_url, alice_path, bob_headers)
    body = result["body"]
    assert "error" in body, f"Bob should NOT see {alice_path}: {body}"


def test_admin_bypass_with_l3(
    base_url: str,
    client: httpx.Client,
    admin_headers: dict[str, str],
    alice_headers: dict[str, str],
) -> None:
    """Admin bypasses namespace checks (L3 doesn't affect admin)."""
    secret_path = "/admin-l3/secret.txt"

    result = _rpc_write(client, base_url, secret_path, "L3 secret", admin_headers)
    assert result["status_code"] == 200

    # Alice can't see it (no grant)
    result = _rpc_read(client, base_url, secret_path, alice_headers)
    assert "error" in result["body"], "Alice should not see admin secret"

    # Admin can see it (admin bypass)
    result = _rpc_read(client, base_url, secret_path, admin_headers)
    assert result["status_code"] == 200
    assert "error" not in result["body"], f"Admin read failed: {result['body']}"
