"""E2E tests for namespace dcache with DATABASE AUTH + PostgreSQL (Issue #1244).

Tests the dcache layer (namespace resolution cache) with a real FastAPI server
using database authentication (--auth-type database) and real admin → user
permission flow against PostgreSQL:

1. Pre-seed PostgreSQL with admin + user API keys
2. Start server with permissions enabled
3. Admin creates files and grants ReBAC permissions
4. Users access files — dcache populated transparently
5. Unmounted paths return 404 (negative dcache entries)
6. Grant revocation → path becomes invisible
7. Performance: repeated access benefits from O(1) dcache hits

Auth type: database (DiscriminatingAuthProvider with DatabaseAPIKeyAuth)

Requirements:
    - PostgreSQL running at postgresql://scorpio@localhost:5432/nexus_e2e_test
    - Start with: docker start scorpio-postgres

Run with:
    pytest tests/e2e/test_namespace_dcache_database_e2e.py -v --override-ini="addopts="
"""

from __future__ import annotations

import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

# === Configuration ===

PYTHON = sys.executable
SERVER_STARTUP_TIMEOUT = 45  # seconds

# PostgreSQL connection — same as test_agent_registry_e2e.py
POSTGRES_URL = os.getenv(
    "NEXUS_E2E_DATABASE_URL",
    "postgresql://scorpio@localhost:5432/nexus_e2e_test",
)

_src_path = Path(__file__).parent.parent.parent / "src"
if str(_src_path) not in sys.path:
    sys.path.insert(0, str(_src_path))

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
    """Create httpx client for localhost connections."""
    return httpx.Client(timeout=15, trust_env=False)


def _auth_headers(api_key: str, zone_id: str = "test") -> dict[str, str]:
    """Build Authorization + Zone headers for a given API key."""
    return {
        "Authorization": f"Bearer {api_key}",
        "X-Nexus-Zone-ID": zone_id,
    }


def _rebac_create(
    client: httpx.Client,
    base_url: str,
    headers: dict[str, str],
    subject: tuple[str, str],
    relation: str,
    obj: tuple[str, str],
    zone_id: str = "test",
) -> dict:
    """Create a ReBAC tuple via RPC. Returns response JSON with tuple_id."""
    resp = client.post(
        f"{base_url}/api/nfs/rebac_create",
        json={
            "params": {
                "subject": list(subject),
                "relation": relation,
                "object": list(obj),
                "zone_id": zone_id,
            }
        },
        headers=headers,
    )
    assert resp.status_code == 200, f"rebac_create failed: {resp.text}"
    data = resp.json()
    # RPC response: {"result": {"tuple_id": "...", "revision": ..., ...}}
    return data.get("result", data)


def _rebac_delete(
    client: httpx.Client,
    base_url: str,
    headers: dict[str, str],
    tuple_id: str,
) -> None:
    """Delete a ReBAC tuple via RPC."""
    resp = client.post(
        f"{base_url}/api/nfs/rebac_delete",
        json={"params": {"tuple_id": tuple_id}},
        headers=headers,
    )
    assert resp.status_code == 200, f"rebac_delete failed: {resp.text}"


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


def _wait_for_ready(base_url: str, admin_key: str, timeout: float = SERVER_STARTUP_TIMEOUT) -> None:
    """Wait for AsyncNexusFS to be initialized (not just health check).

    Polls a v2 endpoint until it responds without 500 (503 = not ready yet).
    """
    deadline = time.monotonic() + timeout
    headers = _auth_headers(admin_key)
    with _make_client() as client:
        while time.monotonic() < deadline:
            try:
                resp = client.get(
                    f"{base_url}/api/v2/files/read",
                    params={"path": "/__readiness_probe__"},
                    headers=headers,
                )
                # Any status other than 500 means AsyncNexusFS is initialized
                # (404 = not found = server is ready, just file doesn't exist)
                if resp.status_code != 500:
                    return
            except httpx.ConnectError:
                pass
            time.sleep(0.5)
    raise TimeoutError(f"AsyncNexusFS not ready within {timeout}s")


def _check_postgres() -> bool:
    """Check if PostgreSQL is available."""
    try:
        engine = create_engine(POSTGRES_URL, echo=False, pool_pre_ping=True)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        engine.dispose()
        return True
    except Exception:
        return False


def _preseed_database(db_url: str) -> dict[str, str]:
    """Pre-seed PostgreSQL with tables and admin/user API keys.

    Returns dict mapping role names to raw API keys:
        {"admin": "sk-...", "alice": "sk-...", "bob": "sk-..."}
    """
    from nexus.server.auth.database_key import DatabaseAPIKeyAuth
    from nexus.storage.models import Base

    engine = create_engine(db_url, echo=False, pool_pre_ping=True)

    # Create all tables (idempotent)
    Base.metadata.create_all(engine)

    # Clean up any stale test data from previous runs
    with engine.connect() as conn:
        trans = conn.begin()
        try:
            conn.execute(text("DELETE FROM api_keys WHERE name LIKE 'dcache-e2e-%'"))
            trans.commit()
        except Exception:
            trans.rollback()

    session_factory = sessionmaker(bind=engine)
    keys: dict[str, str] = {}
    expires_at = datetime.now(UTC) + timedelta(days=1)

    with session_factory() as session:
        # Admin key
        _, admin_key = DatabaseAPIKeyAuth.create_key(
            session,
            user_id="admin",
            name="dcache-e2e-admin",
            zone_id="test",
            is_admin=True,
            expires_at=expires_at,
        )
        keys["admin"] = admin_key

        # Alice (regular user)
        _, alice_key = DatabaseAPIKeyAuth.create_key(
            session,
            user_id="alice",
            name="dcache-e2e-alice",
            zone_id="test",
            is_admin=False,
            expires_at=expires_at,
        )
        keys["alice"] = alice_key

        # Bob (regular user)
        _, bob_key = DatabaseAPIKeyAuth.create_key(
            session,
            user_id="bob",
            name="dcache-e2e-bob",
            zone_id="test",
            is_admin=False,
            expires_at=expires_at,
        )
        keys["bob"] = bob_key

        session.commit()

    engine.dispose()
    return keys


def _cleanup_database(db_url: str) -> None:
    """Clean up test data from PostgreSQL after tests."""
    try:
        engine = create_engine(db_url, echo=False)
        with engine.connect() as conn:
            trans = conn.begin()
            try:
                conn.execute(text("DELETE FROM api_keys WHERE name LIKE 'dcache-e2e-%'"))
                # Clean up ReBAC tuples created during tests
                conn.execute(
                    text(
                        "DELETE FROM rebac_tuples WHERE zone_id = 'test' AND subject_id IN ('alice', 'bob')"
                    )
                )
                trans.commit()
            except Exception:
                trans.rollback()
        engine.dispose()
    except Exception:
        pass  # Best-effort cleanup


# === Fixtures ===


@pytest.fixture(scope="module")
def server():
    """Start a real nexus serve process WITH DATABASE AUTH (PostgreSQL) and PERMISSIONS.

    Pre-seeds PostgreSQL with admin + user API keys, then starts the server.
    Skips if PostgreSQL is not available.
    Yields dict with base_url, data_dir, and api_keys.
    """
    if not _check_postgres():
        pytest.skip(f"PostgreSQL not available at {POSTGRES_URL}")

    port = _find_free_port()
    data_dir = tempfile.mkdtemp(prefix="nexus_dcache_e2e_")
    backend_root = os.path.join(data_dir, "backend")
    os.makedirs(backend_root, exist_ok=True)

    # Pre-seed database with API keys
    api_keys = _preseed_database(POSTGRES_URL)

    base_url = f"http://127.0.0.1:{port}"

    # Build env: DATABASE AUTH + PERMISSIONS ENABLED + PostgreSQL
    env = {
        **os.environ,
        # Clear proxies
        "HTTP_PROXY": "",
        "HTTPS_PROXY": "",
        "http_proxy": "",
        "https_proxy": "",
        "NO_PROXY": "*",
        # Source code on PYTHONPATH
        "PYTHONPATH": str(_src_path),
        # PostgreSQL for database auth + async operations
        "NEXUS_DATABASE_URL": POSTGRES_URL,
        "NEXUS_JWT_SECRET": "dcache-e2e-jwt-secret-key-12345",
        # AsyncNexusFS settings
        "NEXUS_BACKEND_ROOT": backend_root,
        "NEXUS_TENANT_ID": "dcache-e2e-test",
        # CRITICAL: Permissions ENABLED for namespace + dcache testing
        "NEXUS_ENFORCE_PERMISSIONS": "true",
        "NEXUS_ENFORCE_ZONE_ISOLATION": "true",
        # Disable search daemon and rate limiting
        "NEXUS_SEARCH_DAEMON": "false",
        "NEXUS_RATE_LIMIT_ENABLED": "false",
        # NamespaceManager: very short TTL so mount table cache expires quickly
        # between tests. Default (300s, window=10) causes staleness across tests.
        "NEXUS_NAMESPACE_CACHE_TTL": "2",
        "NEXUS_NAMESPACE_REVISION_WINDOW": "1",
    }

    # Start nexus serve with --auth-type database (no --init, we pre-seeded)
    proc = subprocess.Popen(
        [
            PYTHON,
            "-c",
            (
                "from nexus.cli import main; "
                f"main(['serve', '--host', '127.0.0.1', '--port', '{port}', "
                f"'--data-dir', '{data_dir}', '--auth-type', 'database'])"
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
        _wait_for_ready(base_url, api_keys["admin"])

        yield {
            "base_url": base_url,
            "port": port,
            "data_dir": data_dir,
            "process": proc,
            "api_keys": api_keys,
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

        # Cleanup
        shutil.rmtree(data_dir, ignore_errors=True)
        _cleanup_database(POSTGRES_URL)


@pytest.fixture(scope="module")
def client(server: dict) -> httpx.Client:
    """Shared httpx client."""
    with _make_client() as c:
        yield c


@pytest.fixture()
def base_url(server: dict) -> str:
    return server["base_url"]


@pytest.fixture()
def api_keys(server: dict) -> dict[str, str]:
    """API keys: {"admin": "sk-...", "alice": "sk-...", "bob": "sk-..."}."""
    return server["api_keys"]


# =============================================================================
# Health Check
# =============================================================================


def test_health(base_url: str, client: httpx.Client) -> None:
    """Server started successfully with database auth + PostgreSQL."""
    resp = client.get(f"{base_url}/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "healthy"


# =============================================================================
# Admin → User Permission Flow (Issue #1244 dcache validation)
# =============================================================================


def test_admin_can_write_files(
    base_url: str, client: httpx.Client, api_keys: dict[str, str]
) -> None:
    """Admin (database-auth'd via PostgreSQL) can write files."""
    admin_headers = _auth_headers(api_keys["admin"])
    resp = client.post(
        f"{base_url}/api/v2/files/write",
        json={"path": "/workspace/dcache-test/hello.txt", "content": "Hello from admin"},
        headers=admin_headers,
    )
    assert resp.status_code == 200, f"Admin write failed: {resp.text}"


def test_user_without_grants_gets_404(
    base_url: str, client: httpx.Client, api_keys: dict[str, str]
) -> None:
    """User with no ReBAC grants sees nothing (fail-closed → 404).

    The dcache should cache this as a NEGATIVE entry.
    Uses Bob (not Alice) to avoid poisoning Alice's mount table cache
    for subsequent tests that grant Alice access.
    """
    bob_headers = _auth_headers(api_keys["bob"])
    resp = client.get(
        f"{base_url}/api/v2/files/read",
        params={"path": "/workspace/dcache-test/hello.txt"},
        headers=bob_headers,
    )
    # 404 (invisible), not 403 (denied) — namespace visibility model
    assert resp.status_code == 404, f"Expected 404 for ungranted path: {resp.text}"


def test_admin_grants_then_user_reads(
    base_url: str, client: httpx.Client, api_keys: dict[str, str]
) -> None:
    """Full admin → user permission flow with database auth.

    1. Admin creates a file
    2. Admin grants alice direct_viewer via ReBAC
    3. Alice reads the file (200) — dcache positive entry
    4. Bob cannot read the file (404) — dcache negative entry
    """
    admin_headers = _auth_headers(api_keys["admin"])
    alice_headers = _auth_headers(api_keys["alice"])
    bob_headers = _auth_headers(api_keys["bob"])

    test_path = "/workspace/dcache-admin-user/report.txt"

    # 1. Admin creates file
    resp = client.post(
        f"{base_url}/api/v2/files/write",
        json={"path": test_path, "content": "Confidential report v1"},
        headers=admin_headers,
    )
    assert resp.status_code == 200, f"Admin write failed: {resp.text}"

    # 2. Admin grants alice direct_viewer via RPC
    _rebac_create(
        client,
        base_url,
        admin_headers,
        subject=("user", "alice"),
        relation="direct_viewer",
        obj=("file", test_path),
    )

    time.sleep(2.5)  # Wait for mount table cache TTL (2s) to expire

    # 3. Alice reads the file (200 → dcache positive entry)
    resp = client.get(
        f"{base_url}/api/v2/files/read",
        params={"path": test_path},
        headers=alice_headers,
    )
    assert resp.status_code == 200, f"Alice should see {test_path}: {resp.text}"
    assert resp.json()["content"] == "Confidential report v1"

    # 4. Bob cannot read the file (404 → dcache negative entry)
    resp = client.get(
        f"{base_url}/api/v2/files/read",
        params={"path": test_path},
        headers=bob_headers,
    )
    assert resp.status_code == 404, f"Bob should NOT see {test_path}: {resp.text}"


def test_per_subject_namespace_isolation_database_auth(
    base_url: str, client: httpx.Client, api_keys: dict[str, str]
) -> None:
    """Each subject sees only their granted paths (database auth variant).

    - Admin creates two files
    - Admin grants alice one, bob the other
    - Each user can only see their own file (dcache isolation)
    """
    admin_headers = _auth_headers(api_keys["admin"])
    alice_headers = _auth_headers(api_keys["alice"])
    bob_headers = _auth_headers(api_keys["bob"])

    # IMPORTANT: Use different parent directories for true namespace isolation.
    # build_mount_entries() mounts at the PARENT DIRECTORY level, so files in
    # the same directory would share a mount prefix and both be visible.
    alice_path = "/workspace/isolation-alice/data.txt"
    bob_path = "/workspace/isolation-bob/data.txt"

    # Admin creates both files
    for path, content in [(alice_path, "Alice's data"), (bob_path, "Bob's data")]:
        resp = client.post(
            f"{base_url}/api/v2/files/write",
            json={"path": path, "content": content},
            headers=admin_headers,
        )
        assert resp.status_code == 200

    # Grant alice → alice_path, bob → bob_path
    for user_id, path in [("alice", alice_path), ("bob", bob_path)]:
        _rebac_create(
            client,
            base_url,
            admin_headers,
            subject=("user", user_id),
            relation="direct_viewer",
            obj=("file", path),
        )

    time.sleep(2.5)  # Wait for mount table cache TTL (2s) to expire

    # Alice sees alice_path (200), NOT bob_path (404)
    resp = client.get(
        f"{base_url}/api/v2/files/read",
        params={"path": alice_path},
        headers=alice_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["content"] == "Alice's data"

    resp = client.get(
        f"{base_url}/api/v2/files/read",
        params={"path": bob_path},
        headers=alice_headers,
    )
    assert resp.status_code == 404

    # Bob sees bob_path (200), NOT alice_path (404)
    resp = client.get(
        f"{base_url}/api/v2/files/read",
        params={"path": bob_path},
        headers=bob_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["content"] == "Bob's data"

    resp = client.get(
        f"{base_url}/api/v2/files/read",
        params={"path": alice_path},
        headers=bob_headers,
    )
    assert resp.status_code == 404


def test_admin_bypasses_namespace_database_auth(
    base_url: str, client: httpx.Client, api_keys: dict[str, str]
) -> None:
    """Admin user (database auth) bypasses namespace visibility."""
    admin_headers = _auth_headers(api_keys["admin"])
    alice_headers = _auth_headers(api_keys["alice"])

    secret_path = "/admin-only/secret-dcache.txt"

    # Admin creates file
    resp = client.post(
        f"{base_url}/api/v2/files/write",
        json={"path": secret_path, "content": "Top secret dcache"},
        headers=admin_headers,
    )
    assert resp.status_code == 200

    # Alice cannot see it (no grant)
    resp = client.get(
        f"{base_url}/api/v2/files/read",
        params={"path": secret_path},
        headers=alice_headers,
    )
    assert resp.status_code == 404  # Invisible

    # Admin can see it (admin bypass)
    resp = client.get(
        f"{base_url}/api/v2/files/read",
        params={"path": secret_path},
        headers=admin_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["content"] == "Top secret dcache"


def test_grant_revocation_makes_path_invisible_database_auth(
    base_url: str, client: httpx.Client, api_keys: dict[str, str]
) -> None:
    """Revoking a grant makes the path invisible (dcache invalidated).

    1. Admin grants alice viewer on a path
    2. Alice reads it (200 → dcache positive)
    3. Admin revokes the grant
    4. Alice gets 404 (dcache invalidated by revision roll)
    """
    admin_headers = _auth_headers(api_keys["admin"])
    alice_headers = _auth_headers(api_keys["alice"])

    revoke_path = "/workspace/revoke-test/data.txt"

    # Admin creates file
    resp = client.post(
        f"{base_url}/api/v2/files/write",
        json={"path": revoke_path, "content": "Revocable data"},
        headers=admin_headers,
    )
    assert resp.status_code == 200

    # Admin grants alice viewer
    result = _rebac_create(
        client,
        base_url,
        admin_headers,
        subject=("user", "alice"),
        relation="direct_viewer",
        obj=("file", revoke_path),
    )
    tuple_id = result["tuple_id"]
    assert isinstance(tuple_id, str), f"Expected str tuple_id, got {type(tuple_id)}"

    time.sleep(2.5)  # Wait for mount table cache TTL (2s) to expire

    # Alice reads (200)
    resp = client.get(
        f"{base_url}/api/v2/files/read",
        params={"path": revoke_path},
        headers=alice_headers,
    )
    assert resp.status_code == 200

    # Admin revokes grant
    _rebac_delete(client, base_url, admin_headers, tuple_id)

    time.sleep(2.5)  # Wait for mount table cache TTL (2s) to expire

    # Alice now gets 404 (dcache + mount table invalidated)
    resp = client.get(
        f"{base_url}/api/v2/files/read",
        params={"path": revoke_path},
        headers=alice_headers,
    )
    assert resp.status_code == 404


def test_fine_grained_rebac_after_namespace_database_auth(
    base_url: str, client: httpx.Client, api_keys: dict[str, str]
) -> None:
    """Defense in depth: namespace visibility + ReBAC fine-grained check.

    Alice has viewer-of (read-only):
    - GET /read → 200 (visible + read permission)
    - POST /write → 403 (visible but no write permission, NOT 404)
    """
    admin_headers = _auth_headers(api_keys["admin"])
    alice_headers = _auth_headers(api_keys["alice"])

    doc_path = "/workspace/fine-grained/doc.txt"

    # Admin creates file and grants alice viewer (read-only)
    resp = client.post(
        f"{base_url}/api/v2/files/write",
        json={"path": doc_path, "content": "Read-only doc"},
        headers=admin_headers,
    )
    assert resp.status_code == 200

    _rebac_create(
        client,
        base_url,
        admin_headers,
        subject=("user", "alice"),
        relation="direct_viewer",
        obj=("file", doc_path),
    )

    time.sleep(2.5)  # Wait for mount table cache TTL (2s) to expire

    # Alice can READ (200)
    resp = client.get(
        f"{base_url}/api/v2/files/read",
        params={"path": doc_path},
        headers=alice_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["content"] == "Read-only doc"

    # Alice CANNOT WRITE (403, not 404 — path IS visible)
    resp = client.post(
        f"{base_url}/api/v2/files/write",
        json={"path": doc_path, "content": "Unauthorized edit"},
        headers=alice_headers,
    )
    assert resp.status_code == 403, f"Expected 403 for write without permission: {resp.text}"


# =============================================================================
# Performance: dcache benefits
# =============================================================================


def test_dcache_performance_repeated_reads(
    base_url: str, client: httpx.Client, api_keys: dict[str, str]
) -> None:
    """Repeated reads benefit from dcache (O(1) hits vs O(log m) bisect).

    Setup: Grant alice 50 paths, then repeatedly read one path.
    First read populates dcache, subsequent reads should be faster.
    """
    admin_headers = _auth_headers(api_keys["admin"])
    alice_headers = _auth_headers(api_keys["alice"])

    # Create 50 paths and grant alice viewer
    paths = [f"/workspace/perf-dcache/file-{i:03d}.txt" for i in range(50)]
    test_path = paths[25]  # Middle of sorted list

    resp = client.post(
        f"{base_url}/api/v2/files/write",
        json={"path": test_path, "content": "perf test data"},
        headers=admin_headers,
    )
    assert resp.status_code == 200

    for p in paths:
        _rebac_create(
            client,
            base_url,
            admin_headers,
            subject=("user", "alice"),
            relation="direct_viewer",
            obj=("file", p),
        )

    time.sleep(2.5)  # Wait for mount table cache TTL (2s) to expire

    # Warm-up: first read populates dcache
    resp = client.get(
        f"{base_url}/api/v2/files/read",
        params={"path": test_path},
        headers=alice_headers,
    )
    assert resp.status_code == 200

    # Measure 50 repeated reads (should all be dcache hits)
    start = time.perf_counter()
    for _ in range(50):
        resp = client.get(
            f"{base_url}/api/v2/files/read",
            params={"path": test_path},
            headers=alice_headers,
        )
        assert resp.status_code == 200

    elapsed_ms = (time.perf_counter() - start) * 1000
    avg_ms = elapsed_ms / 50

    print(f"\n[DCACHE-PERF] 50 repeated reads: {elapsed_ms:.1f}ms total, {avg_ms:.2f}ms avg")

    # Reasonable latency: HTTP roundtrip + dcache hit should be well under 100ms
    assert avg_ms < 100, f"Dcache reads too slow: {avg_ms:.2f}ms avg (expected <100ms)"
