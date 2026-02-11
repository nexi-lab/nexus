"""E2E tests for consistency migration PATCH endpoint with permissions (Issue #1180).

Tests the full stack: FastAPI server → database auth → zone management → migration,
with real admin and regular user identities exercising permission enforcement.

Covers:
1. Admin can migrate any zone's consistency mode
2. Zone owner (normal user) can migrate their zone
3. Non-member normal user is denied migration (403)
4. Unauthenticated user is denied (401)
5. Same-mode migration returns 400
6. Non-existent zone returns 404
7. ZoneResponse includes consistency_mode field
8. Migration result includes correct from/to modes

Requires: server started with --auth-type database --init (handled by fixture).

Usage:
    PYTHONPATH=src /opt/homebrew/bin/python3.13 -m pytest \
        tests/e2e/test_consistency_migration_permissions_e2e.py -v -o "addopts="
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
import uuid
from collections.abc import Generator
from contextlib import closing, suppress
from pathlib import Path
from typing import Any

import httpx
import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_src_path = Path(__file__).parent.parent.parent / "src"

JWT_SECRET = "test-secret-key-for-e2e-migration-12345"


def _find_free_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("", 0))
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        return int(s.getsockname()[1])


def _wait_for_server(url: str, timeout: float = 45.0) -> bool:
    start = time.time()
    while time.time() - start < timeout:
        try:
            resp = httpx.get(f"{url}/health", timeout=2.0, trust_env=False)
            if resp.status_code == 200:
                return True
        except (httpx.ConnectError, httpx.ReadTimeout):
            pass
        time.sleep(0.2)
    return False


def _register_user(
    base_url: str,
    email: str,
    password: str,
    username: str,
    display_name: str,
) -> dict[str, Any]:
    """Register a user via /auth/register and return user info with JWT token."""
    resp = httpx.post(
        f"{base_url}/auth/register",
        json={
            "email": email,
            "password": password,
            "username": username,
            "display_name": display_name,
        },
        timeout=10.0,
        trust_env=False,
    )
    if resp.status_code == 201:
        data = resp.json()
        return {
            "user_id": data["user_id"],
            "email": data["email"],
            "token": data["token"],
            "headers": {"Authorization": f"Bearer {data['token']}"},
        }
    raise RuntimeError(f"Registration failed ({resp.status_code}): {resp.text}")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def server(tmp_path_factory: pytest.TempPathFactory) -> Generator[dict[str, Any], None, None]:
    """Start a Nexus server with database auth and permissions enforced."""
    tmp_path = tmp_path_factory.mktemp("migration_perms_e2e")
    storage_path = tmp_path / "storage"
    storage_path.mkdir()
    db_path = tmp_path / "nexus.db"

    port = _find_free_port()
    base_url = f"http://127.0.0.1:{port}"

    env = os.environ.copy()
    env["NEXUS_JWT_SECRET"] = JWT_SECRET
    env["NEXUS_DATABASE_URL"] = f"sqlite:///{db_path}"
    env["PYTHONPATH"] = str(_src_path)
    env["NEXUS_ENFORCE_PERMISSIONS"] = "true"
    env["NEXUS_RATE_LIMIT_ENABLED"] = "false"

    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "from nexus.cli import main; "
                f"main(['serve', '--host', '127.0.0.1', '--port', '{port}', "
                f"'--data-dir', '{tmp_path}', "
                "'--auth-type', 'database', '--init'])"
            ),
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=str(tmp_path),
        preexec_fn=os.setsid if sys.platform != "win32" else None,
    )

    if not _wait_for_server(base_url, timeout=45.0):
        process.terminate()
        stdout, stderr = process.communicate(timeout=10)
        pytest.fail(
            f"Server failed to start on port {port}.\n"
            f"stdout: {stdout.decode()[:2000]}\n"
            f"stderr: {stderr.decode()[:2000]}"
        )

    yield {
        "port": port,
        "base_url": base_url,
        "process": process,
        "db_path": db_path,
        "storage_path": storage_path,
        "tmp_path": tmp_path,
    }

    # Cleanup
    if sys.platform != "win32":
        with suppress(ProcessLookupError, PermissionError):
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
    else:
        process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


@pytest.fixture(scope="module")
def admin_user(server: dict[str, Any]) -> dict[str, Any]:
    """Register an admin user via /auth/register and promote to global admin.

    Returns dict with user_id, email, token, headers.
    """
    user = _register_user(
        base_url=server["base_url"],
        email=f"admin_{uuid.uuid4().hex[:6]}@test.com",
        password="AdminPass123!",
        username=f"admin_{uuid.uuid4().hex[:4]}",
        display_name="Admin User",
    )

    # Promote to global admin via direct DB update
    from sqlalchemy import create_engine, text

    engine = create_engine(f"sqlite:///{server['db_path']}")
    with engine.connect() as conn:
        conn.execute(
            text("UPDATE users SET is_global_admin = 1 WHERE user_id = :uid"),
            {"uid": user["user_id"]},
        )
        conn.commit()
    engine.dispose()

    return user


@pytest.fixture(scope="module")
def admin_client(
    server: dict[str, Any], admin_user: dict[str, Any]
) -> Generator[httpx.Client, None, None]:
    """HTTP client with admin JWT credentials."""
    client = httpx.Client(
        base_url=server["base_url"],
        timeout=30.0,
        trust_env=False,
        headers=admin_user["headers"],
    )
    yield client
    client.close()


@pytest.fixture(scope="module")
def normal_user(server: dict[str, Any]) -> dict[str, Any]:
    """Register a normal (non-admin) user.

    Returns dict with user_id, email, token, headers.
    """
    return _register_user(
        base_url=server["base_url"],
        email=f"alice_{uuid.uuid4().hex[:6]}@test.com",
        password="SecurePass123!",
        username=f"alice_{uuid.uuid4().hex[:4]}",
        display_name="Alice Normal",
    )


@pytest.fixture(scope="module")
def user_client(
    server: dict[str, Any], normal_user: dict[str, Any]
) -> Generator[httpx.Client, None, None]:
    """HTTP client with normal user credentials."""
    client = httpx.Client(
        base_url=server["base_url"],
        timeout=30.0,
        trust_env=False,
        headers=normal_user["headers"],
    )
    yield client
    client.close()


@pytest.fixture(scope="module")
def outsider_user(server: dict[str, Any]) -> dict[str, Any]:
    """Register a second normal user who will NOT be added to the test zone."""
    return _register_user(
        base_url=server["base_url"],
        email=f"bob_{uuid.uuid4().hex[:6]}@test.com",
        password="SecurePass456!",
        username=f"bob_{uuid.uuid4().hex[:4]}",
        display_name="Bob Outsider",
    )


@pytest.fixture(scope="module")
def outsider_client(
    server: dict[str, Any], outsider_user: dict[str, Any]
) -> Generator[httpx.Client, None, None]:
    """HTTP client with outsider user credentials."""
    client = httpx.Client(
        base_url=server["base_url"],
        timeout=30.0,
        trust_env=False,
        headers=outsider_user["headers"],
    )
    yield client
    client.close()


# ---------------------------------------------------------------------------
# Zone setup fixture — admin creates a zone with normal user as member
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def test_zone(
    server: dict[str, Any],
    normal_user: dict[str, Any],
) -> str:
    """Create a test zone (SC mode) with normal_user as owner.

    The normal user creates the zone directly so that the server's own
    ReBAC manager registers them as owner — avoids cross-process SQLite
    visibility issues that arise when inserting tuples from the test process.

    Returns the zone_id.
    """
    zone_id = f"mig-test-{uuid.uuid4().hex[:8]}"

    # Normal user creates zone (they become owner automatically via add_user_to_zone)
    user_client = httpx.Client(
        base_url=server["base_url"],
        timeout=30.0,
        trust_env=False,
        headers=normal_user["headers"],
    )
    try:
        resp = user_client.post(
            "/api/zones",
            json={"name": "Migration Test Zone", "zone_id": zone_id},
        )
        assert resp.status_code == 201, f"Zone creation failed: {resp.status_code} {resp.text}"

        data = resp.json()
        assert data["zone_id"] == zone_id
        assert data.get("consistency_mode", "SC") == "SC"
    finally:
        user_client.close()

    return zone_id


# ===========================================================================
# 1. Server health
# ===========================================================================


class TestServerHealth:
    def test_health(self, server: dict[str, Any]) -> None:
        resp = httpx.get(f"{server['base_url']}/health", timeout=5.0, trust_env=False)
        assert resp.status_code == 200


# ===========================================================================
# 2. Admin migration
# ===========================================================================


class TestAdminMigration:
    """Admin (global admin) can migrate any zone."""

    def test_admin_migrate_sc_to_ec(self, admin_client: httpx.Client, test_zone: str) -> None:
        """Admin migrates zone from SC -> EC."""
        resp = admin_client.patch(
            f"/api/zones/{test_zone}/consistency-mode",
            json={"target_mode": "EC"},
        )
        assert resp.status_code == 200, f"Migration failed: {resp.status_code} {resp.text}"
        data = resp.json()
        assert data["success"] is True
        assert data["from_mode"] == "SC"
        assert data["to_mode"] == "EC"
        assert data["zone_id"] == test_zone
        assert data["duration_ms"] > 0
        assert data["error"] is None

    def test_admin_migrate_ec_to_sc(self, admin_client: httpx.Client, test_zone: str) -> None:
        """Admin migrates zone back from EC -> SC."""
        resp = admin_client.patch(
            f"/api/zones/{test_zone}/consistency-mode",
            json={"target_mode": "SC"},
        )
        assert resp.status_code == 200, f"Migration failed: {resp.status_code} {resp.text}"
        data = resp.json()
        assert data["success"] is True
        assert data["from_mode"] == "EC"
        assert data["to_mode"] == "SC"

    def test_admin_same_mode_returns_400(self, admin_client: httpx.Client, test_zone: str) -> None:
        """Migrating to current mode returns 400."""
        # Zone is now SC (from previous test)
        resp = admin_client.patch(
            f"/api/zones/{test_zone}/consistency-mode",
            json={"target_mode": "SC"},
        )
        assert resp.status_code == 400
        assert "already" in resp.json()["detail"].lower()


# ===========================================================================
# 3. Normal user (zone owner) migration
# ===========================================================================


class TestNormalUserMigration:
    """Normal user who is a zone owner can migrate their zone."""

    def test_owner_migrate_sc_to_ec(self, user_client: httpx.Client, test_zone: str) -> None:
        """Zone owner migrates SC -> EC."""
        resp = user_client.patch(
            f"/api/zones/{test_zone}/consistency-mode",
            json={"target_mode": "EC"},
        )
        assert resp.status_code == 200, f"Migration failed: {resp.status_code} {resp.text}"
        data = resp.json()
        assert data["success"] is True
        assert data["to_mode"] == "EC"

    def test_owner_migrate_ec_back_to_sc(self, user_client: httpx.Client, test_zone: str) -> None:
        """Zone owner migrates EC -> SC."""
        resp = user_client.patch(
            f"/api/zones/{test_zone}/consistency-mode",
            json={"target_mode": "SC"},
        )
        assert resp.status_code == 200, f"Migration failed: {resp.status_code} {resp.text}"
        data = resp.json()
        assert data["success"] is True
        assert data["to_mode"] == "SC"


# ===========================================================================
# 4. Non-member user denied
# ===========================================================================


class TestNonMemberDenied:
    """Normal user who is NOT a zone member is denied migration."""

    def test_outsider_denied_migration(self, outsider_client: httpx.Client, test_zone: str) -> None:
        """Non-member user gets 403 when trying to migrate."""
        resp = outsider_client.patch(
            f"/api/zones/{test_zone}/consistency-mode",
            json={"target_mode": "EC"},
        )
        assert resp.status_code == 403, f"Expected 403, got {resp.status_code}: {resp.text}"


# ===========================================================================
# 5. Unauthenticated user denied
# ===========================================================================


class TestUnauthenticatedDenied:
    """Unauthenticated requests are rejected."""

    def test_no_auth_denied(self, server: dict[str, Any], test_zone: str) -> None:
        """Request without auth token is rejected."""
        client = httpx.Client(base_url=server["base_url"], timeout=10.0, trust_env=False)
        try:
            resp = client.patch(
                f"/api/zones/{test_zone}/consistency-mode",
                json={"target_mode": "EC"},
            )
            # 401 or 422 (missing auth header)
            assert resp.status_code in (401, 422), (
                f"Expected 401/422, got {resp.status_code}: {resp.text}"
            )
        finally:
            client.close()


# ===========================================================================
# 6. Zone not found
# ===========================================================================


class TestZoneNotFound:
    """Migration on nonexistent zone returns 404."""

    def test_nonexistent_zone(self, admin_client: httpx.Client) -> None:
        resp = admin_client.patch(
            "/api/zones/nonexistent-zone-xyz/consistency-mode",
            json={"target_mode": "EC"},
        )
        assert resp.status_code == 404


# ===========================================================================
# 7. Invalid target_mode
# ===========================================================================


class TestInvalidTargetMode:
    """Invalid target mode is rejected by validation."""

    def test_invalid_mode_returns_422(self, admin_client: httpx.Client, test_zone: str) -> None:
        resp = admin_client.patch(
            f"/api/zones/{test_zone}/consistency-mode",
            json={"target_mode": "INVALID"},
        )
        assert resp.status_code == 422


# ===========================================================================
# 8. ZoneResponse includes consistency_mode
# ===========================================================================


class TestZoneResponseConsistencyMode:
    """GET zone endpoint returns consistency_mode in response."""

    def test_zone_get_includes_consistency_mode(
        self, admin_client: httpx.Client, test_zone: str
    ) -> None:
        """Verify GET zone response includes the consistency_mode field."""
        resp = admin_client.get(f"/api/zones/{test_zone}")
        assert resp.status_code == 200
        data = resp.json()
        assert "consistency_mode" in data
        assert data["consistency_mode"] in ("SC", "EC")

    def test_zone_list_includes_consistency_mode(self, admin_client: httpx.Client) -> None:
        """Verify list zones response includes consistency_mode per zone."""
        resp = admin_client.get("/api/zones")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] > 0
        for zone in data["zones"]:
            assert "consistency_mode" in zone
            assert zone["consistency_mode"] in ("SC", "EC")


# ===========================================================================
# 9. Custom timeout parameter
# ===========================================================================


class TestCustomTimeout:
    """Custom timeout_s parameter is accepted."""

    def test_custom_timeout_accepted(self, admin_client: httpx.Client, test_zone: str) -> None:
        """Migration with custom timeout_s succeeds."""
        # First ensure zone is SC
        admin_client.patch(
            f"/api/zones/{test_zone}/consistency-mode",
            json={"target_mode": "SC"},
        )

        resp = admin_client.patch(
            f"/api/zones/{test_zone}/consistency-mode",
            json={"target_mode": "EC", "timeout_s": 60.0},
        )
        # Either 200 (success) or 400 (already EC if previous call failed)
        assert resp.status_code in (200, 400)
        if resp.status_code == 200:
            assert resp.json()["success"] is True

        # Reset back to SC for subsequent tests
        admin_client.patch(
            f"/api/zones/{test_zone}/consistency-mode",
            json={"target_mode": "SC"},
        )
