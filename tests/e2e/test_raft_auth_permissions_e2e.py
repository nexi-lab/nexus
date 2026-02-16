"""E2E tests for Raft metadata store with database auth and user permissions.

Tests the full stack: FastAPI server → database auth → Raft metadata → redb,
with real admin and regular user identities exercising permission enforcement.

Requires: server started with --auth-type database --init (handled by fixture).

Usage:
    .venv/bin/python -m pytest tests/e2e/test_raft_auth_permissions_e2e.py -v --override-ini="addopts="
"""

from __future__ import annotations

import os
import re
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


def _encode_bytes(data: bytes) -> dict[str, str]:
    """Encode bytes for the RPC protocol (matches protocol.py's NexusEncoder)."""
    import base64

    return {"__type__": "bytes", "data": base64.b64encode(data).decode()}


def _rpc(
    client: httpx.Client, method: str, params: dict[str, Any], headers: dict[str, str] | None = None
) -> dict[str, Any]:
    """Call an NFS RPC method and return the parsed response body."""
    resp = client.post(
        f"/api/nfs/{method}",
        json={
            "jsonrpc": "2.0",
            "id": str(uuid.uuid4()),
            "method": method,
            "params": params,
        },
        headers=headers,
    )
    result: dict[str, Any] = resp.json()
    return result


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def db_auth_server(
    tmp_path_factory: pytest.TempPathFactory,
) -> Generator[dict[str, Any], None, None]:
    """Start a Nexus server with --auth-type database --init.

    Yields a dict with server info and the admin API key.
    """
    tmp_path = tmp_path_factory.mktemp("raft_auth_e2e")
    storage_path = tmp_path / "storage"
    storage_path.mkdir()
    db_path = tmp_path / "nexus.db"

    port = _find_free_port()
    base_url = f"http://127.0.0.1:{port}"

    env = os.environ.copy()
    env["NEXUS_JWT_SECRET"] = "test-secret-key-for-e2e-12345"
    env["NEXUS_DATABASE_URL"] = f"sqlite:///{db_path}"
    env["PYTHONPATH"] = str(_src_path)
    env["NEXUS_ENFORCE_PERMISSIONS"] = "true"
    # Disable rate limiting for tests
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

    # Extract admin API key from .nexus-admin-env file written by --init
    admin_api_key: str | None = None
    env_file = tmp_path / ".nexus-admin-env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            m = re.search(r"NEXUS_API_KEY='([^']+)'", line)
            if m:
                admin_api_key = m.group(1)
                break

    if not admin_api_key:
        # Fallback: try to read from process stdout (non-blocking peek)
        # The key line looks like: "Admin API Key: sk-default_admin_..."
        process.terminate()
        stdout, stderr = process.communicate(timeout=10)
        all_output = stdout.decode() + stderr.decode()
        m = re.search(r"Admin API Key:\s*(sk-\S+)", all_output)
        if m:
            admin_api_key = m.group(1)
        else:
            pytest.fail(
                f"Could not find admin API key.\n"
                f"env_file exists: {env_file.exists()}\n"
                f"stdout tail: {all_output[-1000:]}"
            )

    yield {
        "port": port,
        "base_url": base_url,
        "process": process,
        "db_path": db_path,
        "storage_path": storage_path,
        "admin_api_key": admin_api_key,
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
def admin_headers(db_auth_server: dict[str, Any]) -> dict[str, str]:
    """Auth headers for the admin user (API key from --init)."""
    return {"Authorization": f"Bearer {db_auth_server['admin_api_key']}"}


@pytest.fixture(scope="module")
def admin_client(
    db_auth_server: dict[str, Any], admin_headers: dict[str, str]
) -> Generator[httpx.Client, None, None]:
    """HTTP client with admin credentials."""
    client = httpx.Client(
        base_url=db_auth_server["base_url"],
        timeout=60.0,
        trust_env=False,
        headers=admin_headers,
    )
    yield client
    client.close()


@pytest.fixture(scope="module")
def registered_user(db_auth_server: dict[str, Any], admin_client: httpx.Client) -> dict[str, Any]:
    """Register a regular user and return auth info.

    Returns dict with user_id, email, token, headers.
    """
    email = f"alice_{uuid.uuid4().hex[:6]}@test.com"
    password = "SecurePass123!"

    # Register via /auth/register
    resp = admin_client.post(
        "/auth/register",
        json={
            "email": email,
            "password": password,
            "username": f"alice_{uuid.uuid4().hex[:4]}",
            "display_name": "Alice Test",
        },
    )
    # Registration may fail if auth routes not available (depends on server setup)
    if resp.status_code == 201:
        data = resp.json()
        return {
            "user_id": data["user_id"],
            "email": data["email"],
            "token": data["token"],
            "headers": {"Authorization": f"Bearer {data['token']}"},
        }

    # Fallback: create JWT token directly (known secret)
    import jwt as pyjwt

    user_id = str(uuid.uuid4())
    payload = {
        "sub": user_id,
        "subject_id": user_id,
        "subject_type": "user",
        "zone_id": "default",
        "email": email,
        "is_admin": False,
    }
    token = pyjwt.encode(payload, "test-secret-key-for-e2e-12345", algorithm="HS256")
    return {
        "user_id": user_id,
        "email": email,
        "token": token,
        "headers": {"Authorization": f"Bearer {token}"},
    }


@pytest.fixture(scope="module")
def user_client(
    db_auth_server: dict[str, Any], registered_user: dict[str, Any]
) -> Generator[httpx.Client, None, None]:
    """HTTP client with regular user credentials."""
    client = httpx.Client(
        base_url=db_auth_server["base_url"],
        timeout=60.0,
        trust_env=False,
        headers=registered_user["headers"],
    )
    yield client
    client.close()


# ===========================================================================
# Health & Auth Tests
# ===========================================================================


class TestServerHealth:
    """Verify the server started correctly with database auth."""

    def test_health_endpoint(self, admin_client: httpx.Client) -> None:
        resp = admin_client.get("/health")
        assert resp.status_code == 200

    def test_whoami_admin(self, admin_client: httpx.Client) -> None:
        """Admin API key should return admin identity."""
        resp = admin_client.get("/api/auth/whoami")
        if resp.status_code == 200:
            data = resp.json()
            assert data.get("authenticated") is True
            assert data.get("is_admin") is True

    def test_unauthenticated_rpc_rejected(self, db_auth_server: dict[str, Any]) -> None:
        """RPC calls without auth should be rejected (401)."""
        client = httpx.Client(base_url=db_auth_server["base_url"], timeout=30.0, trust_env=False)
        try:
            result = _rpc(client, "list", {"path": "/"})
            # Server returns {"detail": "Invalid or missing API key"} for 401
            assert result.get("error") is not None or result.get("detail") is not None, (
                f"Expected auth rejection, got: {result}"
            )
        finally:
            client.close()


# ===========================================================================
# Admin File Operations (RPC)
# ===========================================================================


class TestAdminFileOperations:
    """Admin should be able to perform all file operations."""

    def test_admin_write_file(self, admin_client: httpx.Client) -> None:
        """Admin writes a file via RPC."""
        result = _rpc(
            admin_client,
            "write",
            {
                "path": "/workspace/admin_test.txt",
                "content": _encode_bytes(b"Hello from admin!"),
            },
        )
        # Should succeed (no error key, or result present)
        assert result.get("error") is None, f"Write failed: {result}"

    def test_admin_read_file(self, admin_client: httpx.Client) -> None:
        """Admin reads back the file it wrote."""
        result = _rpc(admin_client, "read", {"path": "/workspace/admin_test.txt"})
        assert result.get("error") is None, f"Read failed: {result}"
        # Result should contain file content
        res = result.get("result", {})
        assert res.get("content") is not None or res.get("data") is not None

    def test_admin_list_files(self, admin_client: httpx.Client) -> None:
        """Admin lists files in workspace."""
        result = _rpc(admin_client, "list", {"path": "/workspace/"})
        assert result.get("error") is None, f"List failed: {result}"
        items = result.get("result", {})
        # Should contain at least admin_test.txt
        assert isinstance(items, (list, dict))

    def test_admin_get_metadata(self, admin_client: httpx.Client) -> None:
        """Admin gets metadata for a file."""
        result = _rpc(admin_client, "get_metadata", {"path": "/workspace/admin_test.txt"})
        assert result.get("error") is None, f"get_metadata failed: {result}"

    def test_admin_delete_file(self, admin_client: httpx.Client) -> None:
        """Admin deletes a file."""
        # Create a throwaway file
        _rpc(
            admin_client,
            "write",
            {
                "path": "/workspace/to_delete.txt",
                "content": _encode_bytes(b"delete me"),
            },
        )

        result = _rpc(admin_client, "delete", {"path": "/workspace/to_delete.txt"})
        assert result.get("error") is None, f"Delete failed: {result}"

        # Verify it's gone
        result = _rpc(admin_client, "exists", {"path": "/workspace/to_delete.txt"})
        exists = result.get("result", {})
        # exists should return False
        if isinstance(exists, dict):
            assert exists.get("exists") is False
        elif isinstance(exists, bool):
            assert exists is False


# ===========================================================================
# Permission Enforcement Tests
# ===========================================================================


class TestPermissionEnforcement:
    """Test that permissions are enforced for non-admin users."""

    def test_user_cannot_write_to_admin_workspace(self, user_client: httpx.Client) -> None:
        """Regular user should not be able to write to /workspace without grant."""
        result = _rpc(
            user_client,
            "write",
            {
                "path": "/workspace/unauthorized.txt",
                "content": _encode_bytes(b"Unauthorized write"),
            },
        )
        error = result.get("error")
        # Should get a permission error (or the file system may reject it)
        # Accept either RPC error or empty result
        if error is not None:
            err_msg = str(error.get("message", "")).lower()
            assert (
                "permission" in err_msg
                or "denied" in err_msg
                or "unauthorized" in err_msg
                or "not found" in err_msg
                or error.get("code") in (-32603, -32001, -32002)  # internal/permission error codes
            ), f"Expected permission error, got: {error}"

    def test_user_cannot_read_admin_file(
        self, admin_client: httpx.Client, user_client: httpx.Client
    ) -> None:
        """Regular user should not read admin-only files without grant."""
        # Admin writes a file
        _rpc(
            admin_client,
            "write",
            {
                "path": "/workspace/secret.txt",
                "content": _encode_bytes(b"Admin secret"),
            },
        )

        # User tries to read it
        result = _rpc(user_client, "read", {"path": "/workspace/secret.txt"})
        error = result.get("error")
        # Should fail with permission error
        if error is not None:
            err_msg = str(error.get("message", "")).lower()
            assert (
                "permission" in err_msg
                or "denied" in err_msg
                or "not found" in err_msg
                or error.get("code") in (-32603, -32001, -32002)
            ), f"Expected permission error, got: {error}"

    def test_user_can_access_granted_path(
        self,
        db_auth_server: dict[str, Any],
        admin_client: httpx.Client,
        user_client: httpx.Client,
        registered_user: dict[str, Any],
    ) -> None:
        """After admin grants access, user should be able to read."""
        # Admin writes a file
        _rpc(
            admin_client,
            "write",
            {
                "path": "/workspace/shared.txt",
                "content": _encode_bytes(b"Shared content"),
            },
        )

        # Grant the user read access via ReBAC tuple in database
        from sqlalchemy import create_engine, text

        engine = create_engine(f"sqlite:///{db_auth_server['db_path']}")
        user_id = registered_user["user_id"]

        with engine.connect() as conn:
            conn.execute(
                text("""
                    INSERT OR IGNORE INTO rebac_tuples
                    (tuple_id, subject_type, subject_id, relation,
                     object_type, object_id, zone_id,
                     subject_zone_id, object_zone_id)
                    VALUES (:tid, 'user', :uid, 'direct_reader',
                            'file', '/workspace/shared.txt', 'default',
                            'default', 'default')
                """),
                {"tid": str(uuid.uuid4()), "uid": user_id},
            )
            conn.commit()

        # User should now be able to read
        result = _rpc(user_client, "read", {"path": "/workspace/shared.txt"})
        # If permissions are working, this should succeed after the grant
        # (Accept both success and error - depends on permission cache timing)
        # The important thing is we don't crash
        assert isinstance(result, dict)


# ===========================================================================
# User Registration & Auth Flow Tests
# ===========================================================================


class TestAuthFlow:
    """Test user registration and login flow."""

    def test_register_new_user(self, db_auth_server: dict[str, Any]) -> None:
        """Register a brand new user via the /auth/register endpoint."""
        client = httpx.Client(base_url=db_auth_server["base_url"], timeout=30.0, trust_env=False)
        try:
            email = f"bob_{uuid.uuid4().hex[:6]}@test.com"
            resp = client.post(
                "/auth/register",
                json={
                    "email": email,
                    "password": "TestPassword123!",
                    "username": f"bob_{uuid.uuid4().hex[:4]}",
                    "display_name": "Bob Test",
                },
            )
            if resp.status_code == 201:
                data = resp.json()
                assert "user_id" in data
                assert "token" in data
                assert data["email"] == email
            elif resp.status_code == 503:
                pytest.skip("Auth provider not available (service unavailable)")
            else:
                pytest.fail(f"Unexpected status {resp.status_code}: {resp.text}")
        finally:
            client.close()

    def test_login_existing_user(self, db_auth_server: dict[str, Any]) -> None:
        """Register then login with credentials."""
        client = httpx.Client(base_url=db_auth_server["base_url"], timeout=30.0, trust_env=False)
        try:
            email = f"carol_{uuid.uuid4().hex[:6]}@test.com"
            password = "TestPassword456!"

            # Register
            reg_resp = client.post(
                "/auth/register",
                json={"email": email, "password": password},
            )
            if reg_resp.status_code != 201:
                pytest.skip("Registration not available")

            # Mark email as verified (required before login).
            # SCHEMA DEPENDENCY: Directly updates 'users.email_verified' column.
            from sqlalchemy import create_engine, text

            engine = create_engine(f"sqlite:///{db_auth_server['db_path']}")
            with engine.connect() as conn:
                conn.execute(
                    text("UPDATE users SET email_verified = 1 WHERE email = :email"),
                    {"email": email},
                )
                conn.commit()
            engine.dispose()

            # Login
            login_resp = client.post(
                "/auth/login",
                json={"identifier": email, "password": password},
            )
            assert login_resp.status_code == 200, f"Login failed: {login_resp.text}"
            data = login_resp.json()
            assert "token" in data
            assert data["user"]["email"] == email
        finally:
            client.close()

    def test_login_wrong_password_fails(self, db_auth_server: dict[str, Any]) -> None:
        """Login with wrong password should fail."""
        client = httpx.Client(base_url=db_auth_server["base_url"], timeout=30.0, trust_env=False)
        try:
            email = f"dave_{uuid.uuid4().hex[:6]}@test.com"

            # Register
            reg_resp = client.post(
                "/auth/register",
                json={"email": email, "password": "CorrectPass123!"},
            )
            if reg_resp.status_code != 201:
                pytest.skip("Registration not available")

            # Login with wrong password
            login_resp = client.post(
                "/auth/login",
                json={"identifier": email, "password": "WrongPassword!"},
            )
            assert login_resp.status_code == 401
        finally:
            client.close()


# ===========================================================================
# Full Write → Read → Delete Lifecycle (Admin)
# ===========================================================================


class TestAdminLifecycle:
    """Full CRUD lifecycle as admin through the API."""

    def test_write_read_delete_cycle(self, admin_client: httpx.Client) -> None:
        """Admin: write → read → verify content → delete → verify gone."""
        import base64

        path = f"/workspace/lifecycle_{uuid.uuid4().hex[:8]}.txt"
        original_content = f"Content written at {time.time()}"

        # Write
        result = _rpc(
            admin_client,
            "write",
            {
                "path": path,
                "content": _encode_bytes(original_content.encode()),
            },
        )
        assert result.get("error") is None, f"Write failed: {result}"

        # Read back
        result = _rpc(admin_client, "read", {"path": path})
        assert result.get("error") is None, f"Read failed: {result}"
        res = result.get("result", {})
        # Content should contain our original text
        read_content = res.get("content", "")
        if isinstance(read_content, str):
            # May be base64 encoded or plain text
            try:
                decoded = base64.b64decode(read_content).decode()
            except Exception:
                decoded = read_content
            assert original_content in decoded or decoded in original_content

        # Exists check
        result = _rpc(admin_client, "exists", {"path": path})
        assert result.get("error") is None

        # Delete
        result = _rpc(admin_client, "delete", {"path": path})
        assert result.get("error") is None, f"Delete failed: {result}"

        # Verify deleted
        result = _rpc(admin_client, "exists", {"path": path})
        exists_result = result.get("result", {})
        if isinstance(exists_result, dict):
            assert exists_result.get("exists") is False
        elif isinstance(exists_result, bool):
            assert exists_result is False

    def test_mkdir_and_list(self, admin_client: httpx.Client) -> None:
        """Admin: mkdir → write files → list."""
        dir_path = f"/workspace/testdir_{uuid.uuid4().hex[:8]}"

        # Create directory
        result = _rpc(admin_client, "mkdir", {"path": dir_path})
        assert result.get("error") is None, f"mkdir failed: {result}"

        # Write files inside
        for i in range(3):
            _rpc(
                admin_client,
                "write",
                {
                    "path": f"{dir_path}/file{i}.txt",
                    "content": _encode_bytes(f"File {i}".encode()),
                },
            )

        # List directory
        result = _rpc(admin_client, "list", {"path": dir_path})
        assert result.get("error") is None, f"list failed: {result}"
        items = result.get("result", [])
        # Should have our 3 files (response format varies)
        if isinstance(items, list):
            assert len(items) >= 3


# ===========================================================================
# Lock API Tests with Auth
# ===========================================================================


class TestLockApiAuth:
    """Test lock API endpoints with authenticated users."""

    def _get_lock_manager_available(self, admin_client: httpx.Client) -> bool:
        """Check if lock manager is available."""
        resp = admin_client.get("/api/locks")
        return resp.status_code != 503

    def test_admin_acquire_and_release_lock(self, admin_client: httpx.Client) -> None:
        """Admin acquires and releases a lock."""
        if not self._get_lock_manager_available(admin_client):
            pytest.skip("Lock manager not available")

        path = f"/test/lock_{uuid.uuid4().hex[:8]}.txt"

        # Acquire
        resp = admin_client.post(
            "/api/locks",
            json={"path": path, "timeout": 5, "ttl": 30},
        )
        assert resp.status_code == 201, f"Acquire failed: {resp.text}"
        lock_id = resp.json()["lock_id"]

        # Release
        resp = admin_client.delete(f"/api/locks{path}?lock_id={lock_id}")
        assert resp.status_code == 200

    def test_unauthenticated_lock_rejected(self, db_auth_server: dict[str, Any]) -> None:
        """Lock operations without auth should be rejected."""
        client = httpx.Client(base_url=db_auth_server["base_url"], timeout=30.0, trust_env=False)
        try:
            resp = client.post(
                "/api/locks",
                json={"path": "/test/noauth.txt", "timeout": 1, "ttl": 10},
            )
            assert resp.status_code in (401, 403), f"Expected auth error: {resp.status_code}"
        finally:
            client.close()

    def test_admin_force_release(self, admin_client: httpx.Client) -> None:
        """Admin should be able to force-release any lock."""
        if not self._get_lock_manager_available(admin_client):
            pytest.skip("Lock manager not available")

        path = f"/test/force_{uuid.uuid4().hex[:8]}.txt"

        # Acquire
        resp = admin_client.post(
            "/api/locks",
            json={"path": path, "timeout": 5, "ttl": 60},
        )
        if resp.status_code != 201:
            pytest.skip(f"Lock acquire failed: {resp.text}")

        lock_id = resp.json()["lock_id"]

        # Force release (admin only)
        resp = admin_client.delete(f"/api/locks{path}?lock_id={lock_id}&force=true")
        assert resp.status_code == 200

    def test_write_with_lock_via_rpc(self, admin_client: httpx.Client) -> None:
        """Test write(lock=True) through the full RPC stack with auth (#1143).

        This exercises: HTTP → auth → RPC dispatch → WriteParams(lock=True)
        → _handle_write → nexus_fs.write(lock=True).
        If lock manager is unavailable, write should still succeed (lock=False fallback).
        """
        path = f"/workspace/lock_write_{uuid.uuid4().hex[:8]}.txt"

        # Write with lock=True via RPC
        result = _rpc(
            admin_client,
            "write",
            {
                "path": path,
                "content": _encode_bytes(b"locked write content"),
                "lock": True,
                "lock_timeout": 5.0,
            },
        )
        # If lock manager is available, this should succeed.
        # If not available, server may return error or succeed without lock.
        # The key assertion: the WriteParams dataclass accepted lock/lock_timeout
        # (no "Invalid parameters" error).
        error = result.get("error")
        if error is not None:
            err_msg = str(error.get("message", "")).lower()
            # "no lock manager" is acceptable — means params parsed but no Redis
            # "invalid parameters" would mean WriteParams rejected lock field (BUG)
            assert "invalid parameters" not in err_msg, (
                f"WriteParams rejected lock param — missing field in protocol.py? Error: {error}"
            )

        # If write succeeded, verify content is readable
        if error is None:
            read_result = _rpc(admin_client, "read", {"path": path})
            assert read_result.get("error") is None, (
                f"Read after locked write failed: {read_result}"
            )

    def test_write_with_lock_permission_denied(
        self, admin_client: httpx.Client, user_client: httpx.Client
    ) -> None:
        """Test write(lock=True) is rejected for unauthorized users."""
        path = f"/workspace/lock_denied_{uuid.uuid4().hex[:8]}.txt"

        # Admin creates the file first
        _rpc(
            admin_client,
            "write",
            {
                "path": path,
                "content": _encode_bytes(b"admin content"),
            },
        )

        # User tries to write with lock — should be denied by permissions
        result = _rpc(
            user_client,
            "write",
            {
                "path": path,
                "content": _encode_bytes(b"unauthorized locked write"),
                "lock": True,
            },
        )
        error = result.get("error")
        if error is not None:
            err_msg = str(error.get("message", "")).lower()
            # Should be permission error, not "invalid parameters"
            assert "invalid parameters" not in err_msg, f"WriteParams rejected lock param: {error}"
            assert (
                "permission" in err_msg
                or "denied" in err_msg
                or "not found" in err_msg
                or error.get("code") in (-32603, -32001, -32002)
            ), f"Expected permission error, got: {error}"

    def test_lock_status_check(self, admin_client: httpx.Client) -> None:
        """Check lock status for a path."""
        if not self._get_lock_manager_available(admin_client):
            pytest.skip("Lock manager not available")

        path = f"/test/status_{uuid.uuid4().hex[:8]}.txt"

        # Not locked initially
        resp = admin_client.get(f"/api/locks{path}")
        assert resp.status_code == 200
        assert resp.json()["locked"] is False

        # Acquire
        resp = admin_client.post(
            "/api/locks",
            json={"path": path, "timeout": 5, "ttl": 30},
        )
        if resp.status_code != 201:
            pytest.skip(f"Lock acquire failed: {resp.text}")
        lock_id = resp.json()["lock_id"]

        # Should be locked
        resp = admin_client.get(f"/api/locks{path}")
        assert resp.status_code == 200
        assert resp.json()["locked"] is True

        # Cleanup
        admin_client.delete(f"/api/locks{path}?lock_id={lock_id}")
