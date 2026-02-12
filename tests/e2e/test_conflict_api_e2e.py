"""E2E tests for Conflict REST API with PERMISSIONS ENABLED (Issue #1130).

Starts a real `nexus serve` process with:
- NEXUS_ENFORCE_PERMISSIONS=true (ReBAC enforced)
- NEXUS_WRITE_BACK=true (bidirectional sync + conflict log store)
- Multi-key static auth (admin + alice)

Tests the conflict management endpoints end-to-end:
1. Unauthenticated requests -> 401
2. Authenticated admin can list/get/resolve conflicts
3. Conflict log store is wired and accessible via REST
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

ADMIN_API_KEY = "sk-admin-conflict-e2e"
ALICE_API_KEY = "sk-alice-conflict-e2e"

# Clear proxy env vars so localhost connections work
for _key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
    os.environ.pop(_key, None)
os.environ["NO_PROXY"] = "*"


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _make_client() -> httpx.Client:
    return httpx.Client(timeout=10)


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
        logging.basicConfig(level=logging.INFO)
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
            }}
        }}

        import nexus.server.auth.factory as factory
        _orig = factory.create_auth_provider
        def _patched(auth_type, auth_config_arg=None, **kwargs):
            if auth_type == "static":
                return StaticAPIKeyAuth.from_config(auth_config)
            return _orig(auth_type, auth_config_arg, **kwargs)
        factory.create_auth_provider = _patched

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
    """Start a real nexus serve with permissions + write-back enabled."""
    port = _find_free_port()
    data_dir = tempfile.mkdtemp(prefix="nexus_conflict_e2e_")
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
        "NEXUS_TENANT_ID": "conflict-e2e",
        "NEXUS_ENFORCE_PERMISSIONS": "true",
        "NEXUS_ENFORCE_ZONE_ISOLATION": "true",
        "NEXUS_WRITE_BACK": "true",
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


# =============================================================================
# Tests: Health check with conflict store wired
# =============================================================================


def test_health(base_url: str, client: httpx.Client) -> None:
    """Server is healthy with write-back + permissions enabled."""
    resp = client.get(f"{base_url}/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "healthy"


# =============================================================================
# Tests: Auth enforcement on conflict endpoints
# =============================================================================


def test_list_conflicts_unauthenticated_returns_401(base_url: str, client: httpx.Client) -> None:
    """GET /api/v2/sync/conflicts without auth -> 401."""
    resp = client.get(f"{base_url}/api/v2/sync/conflicts")
    assert resp.status_code == 401


def test_get_conflict_unauthenticated_returns_401(base_url: str, client: httpx.Client) -> None:
    """GET /api/v2/sync/conflicts/{id} without auth -> 401."""
    resp = client.get(f"{base_url}/api/v2/sync/conflicts/some-id")
    assert resp.status_code == 401


def test_resolve_conflict_unauthenticated_returns_401(base_url: str, client: httpx.Client) -> None:
    """POST /api/v2/sync/conflicts/{id}/resolve without auth -> 401."""
    resp = client.post(
        f"{base_url}/api/v2/sync/conflicts/some-id/resolve",
        json={"outcome": "nexus_wins"},
    )
    assert resp.status_code == 401


# =============================================================================
# Tests: Authenticated access to conflict endpoints
# =============================================================================


def test_list_conflicts_authenticated_returns_200(
    base_url: str, client: httpx.Client, admin_headers: dict
) -> None:
    """GET /api/v2/sync/conflicts with valid auth -> 200 (empty list initially)."""
    resp = client.get(f"{base_url}/api/v2/sync/conflicts", headers=admin_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert "conflicts" in body
    assert "total" in body
    assert isinstance(body["conflicts"], list)


def test_get_nonexistent_conflict_returns_404(
    base_url: str, client: httpx.Client, admin_headers: dict
) -> None:
    """GET /api/v2/sync/conflicts/{id} for nonexistent -> 404."""
    resp = client.get(
        f"{base_url}/api/v2/sync/conflicts/nonexistent-id",
        headers=admin_headers,
    )
    assert resp.status_code == 404


def test_resolve_nonexistent_conflict_returns_404(
    base_url: str, client: httpx.Client, admin_headers: dict
) -> None:
    """POST resolve for nonexistent conflict -> 404."""
    resp = client.post(
        f"{base_url}/api/v2/sync/conflicts/nonexistent-id/resolve",
        json={"outcome": "nexus_wins"},
        headers=admin_headers,
    )
    assert resp.status_code == 404


def test_list_conflicts_with_filters(
    base_url: str, client: httpx.Client, admin_headers: dict
) -> None:
    """GET /api/v2/sync/conflicts with query params accepted by server."""
    resp = client.get(
        f"{base_url}/api/v2/sync/conflicts",
        params={"status": "auto_resolved", "limit": 10, "offset": 0},
        headers=admin_headers,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 0
    assert body["conflicts"] == []


def test_non_admin_list_conflicts_returns_403(
    base_url: str, client: httpx.Client, alice_headers: dict
) -> None:
    """Non-admin authenticated user gets 403 on list conflicts."""
    resp = client.get(f"{base_url}/api/v2/sync/conflicts", headers=alice_headers)
    assert resp.status_code == 403
    assert "Admin role required" in resp.json()["detail"]


def test_non_admin_get_conflict_returns_403(
    base_url: str, client: httpx.Client, alice_headers: dict
) -> None:
    """Non-admin authenticated user gets 403 on get conflict."""
    resp = client.get(f"{base_url}/api/v2/sync/conflicts/some-id", headers=alice_headers)
    assert resp.status_code == 403


def test_non_admin_resolve_conflict_returns_403(
    base_url: str, client: httpx.Client, alice_headers: dict
) -> None:
    """Non-admin authenticated user gets 403 on resolve conflict."""
    resp = client.post(
        f"{base_url}/api/v2/sync/conflicts/some-id/resolve",
        json={"outcome": "nexus_wins"},
        headers=alice_headers,
    )
    assert resp.status_code == 403


def test_resolve_invalid_outcome_returns_422(
    base_url: str, client: httpx.Client, admin_headers: dict
) -> None:
    """POST resolve with invalid outcome -> 422 (Pydantic validation)."""
    resp = client.post(
        f"{base_url}/api/v2/sync/conflicts/some-id/resolve",
        json={"outcome": "totally_invalid"},
        headers=admin_headers,
    )
    assert resp.status_code == 422
