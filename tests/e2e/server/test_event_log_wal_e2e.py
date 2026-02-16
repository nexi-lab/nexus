"""E2E tests for Event Log WAL integration with nexus serve.

Starts a real server process, publishes events via the API,
and verifies WAL segment files are created on disk.

Issue #1397
"""

from __future__ import annotations

import base64
import os
import subprocess
import sys

import httpx
import pytest


def _encode_bytes(data: bytes) -> dict:
    """Encode bytes for JSON-RPC transport."""
    return {"__type__": "bytes", "data": base64.b64encode(data).decode()}


def _rpc(client: httpx.Client, method: str, params: dict, api_key: str) -> dict:
    """Send a JSON-RPC request to the server."""
    resp = client.post(
        f"/api/nfs/{method}",
        json={"jsonrpc": "2.0", "id": "1", "method": method, "params": params},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    return resp.json()


@pytest.fixture(scope="function")
def nexus_server_with_wal(isolated_db, tmp_path):
    """Start nexus serve with explicit WAL directory for e2e testing."""
    import signal
    import socket
    from contextlib import closing, suppress

    from tests.e2e.conftest import _src_path, wait_for_server

    storage_path = tmp_path / "storage"
    storage_path.mkdir(exist_ok=True)

    wal_dir = tmp_path / "wal"

    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("", 0))
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        port = s.getsockname()[1]

    base_url = f"http://127.0.0.1:{port}"

    env = os.environ.copy()
    env["NEXUS_JWT_SECRET"] = "test-secret-key-for-e2e-12345"
    env["NEXUS_DATABASE_URL"] = f"sqlite:///{isolated_db}"
    env["NEXUS_API_KEY"] = "test-e2e-api-key-12345"
    env["NEXUS_WAL_DIR"] = str(wal_dir)
    env["NEXUS_REDIS_URL"] = "redis://127.0.0.1:6379/15"
    env["PYTHONPATH"] = str(_src_path)

    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                f"from nexus.cli import main; "
                f"main(['serve', '--host', '127.0.0.1', '--port', '{port}', "
                f"'--data-dir', '{tmp_path}'])"
            ),
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        preexec_fn=os.setsid if sys.platform != "win32" else None,
    )

    if not wait_for_server(base_url, timeout=30.0):
        process.terminate()
        stdout, stderr = process.communicate(timeout=5)
        pytest.fail(
            f"Server failed to start.\n"
            f"stdout: {stdout.decode()[:2000]}\n"
            f"stderr: {stderr.decode()[:2000]}"
        )

    yield {
        "port": port,
        "base_url": base_url,
        "process": process,
        "wal_dir": wal_dir,
        "tmp_path": tmp_path,
    }

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


class TestWALServerIntegration:
    """E2E: server startup with WAL, file operations create WAL segments."""

    def test_wal_initializes_and_persists_events(self, nexus_server_with_wal):
        """Start server, write files via API, verify WAL segment files exist."""
        api_key = "test-e2e-api-key-12345"
        base_url = nexus_server_with_wal["base_url"]
        wal_dir = nexus_server_with_wal["wal_dir"]

        with httpx.Client(base_url=base_url, timeout=30.0, trust_env=False) as client:
            # 1. Health check — server is up
            resp = client.get("/health")
            assert resp.status_code == 200

            # 2. Write several files to trigger events
            for i in range(5):
                result = _rpc(
                    client,
                    "write",
                    {
                        "path": f"/test-wal-{i}.txt",
                        "content": _encode_bytes(f"content-{i}".encode()),
                    },
                    api_key,
                )
                # Write should succeed (2xx or result without error)
                assert "error" not in result or result.get("result") is not None, (
                    f"Write failed: {result}"
                )

        # 3. Give async event publishing a moment to flush
        import time

        time.sleep(1.0)

        # 3b. Check WAL directory exists and has segment files
        assert wal_dir.exists(), (
            f"WAL directory not created at {wal_dir}. Server may not have initialized event log."
        )

        seg_files = list(wal_dir.glob("wal-*.seg"))
        all_files = list(wal_dir.iterdir()) if wal_dir.exists() else []

        assert len(seg_files) > 0, (
            f"WAL dir exists ({wal_dir}) but no segment files. "
            f"Files in WAL dir: {[f.name for f in all_files]}. "
            f"Event bus may not be wired to event log."
        )

        # 4. Verify segment file has non-trivial size (header=8 + records)
        total_bytes = sum(f.stat().st_size for f in seg_files)
        assert total_bytes > 8, f"WAL segments too small ({total_bytes} bytes)"

        # 5. Verify segment naming convention: wal-{seq}-{epoch}.seg
        for seg in seg_files:
            parts = seg.stem.split("-")
            assert parts[0] == "wal", f"Bad segment name: {seg.name}"
            assert len(parts) == 3, f"Expected wal-{{seq}}-{{epoch}}, got: {seg.name}"


def _mint_jwt(secret: str, subject_id: str, zone_id: str, is_admin: bool = False) -> str:
    """Mint a JWT token for testing."""
    import time as _time

    from authlib.jose import jwt as jose_jwt

    header = {"alg": "HS256"}
    payload = {
        "sub": subject_id,
        "email": f"{subject_id}@test.local",
        "subject_type": "user",
        "subject_id": subject_id,
        "zone_id": zone_id,
        "is_admin": is_admin,
        "name": subject_id,
        "iat": int(_time.time()),
        "exp": int(_time.time()) + 3600,
    }
    token = jose_jwt.encode(header, payload, secret)
    return token.decode() if isinstance(token, bytes) else token


@pytest.fixture(scope="function")
def nexus_server_with_permissions(isolated_db, tmp_path):
    """Start nexus serve with permissions enforced and WAL enabled."""
    import signal
    import socket
    from contextlib import closing, suppress

    from tests.e2e.conftest import _src_path, wait_for_server

    wal_dir = tmp_path / "wal"
    jwt_secret = "test-jwt-secret-for-permissions-e2e"

    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("", 0))
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        port = s.getsockname()[1]

    base_url = f"http://127.0.0.1:{port}"

    env = os.environ.copy()
    env["NEXUS_JWT_SECRET"] = jwt_secret
    env["NEXUS_DATABASE_URL"] = f"sqlite:///{isolated_db}"
    env["NEXUS_WAL_DIR"] = str(wal_dir)
    env["NEXUS_REDIS_URL"] = "redis://127.0.0.1:6379/15"
    env["NEXUS_ENFORCE_PERMISSIONS"] = "true"
    env["NEXUS_ALLOW_ADMIN_BYPASS"] = "true"
    env["PYTHONPATH"] = str(_src_path)

    # Use --auth-type local so JWT tokens are validated (not open access mode)
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                f"from nexus.cli import main; "
                f"main(['serve', '--host', '127.0.0.1', '--port', '{port}', "
                f"'--auth-type', 'local', "
                f"'--data-dir', '{tmp_path}'])"
            ),
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        preexec_fn=os.setsid if sys.platform != "win32" else None,
    )

    if not wait_for_server(base_url, timeout=30.0):
        process.terminate()
        stdout, stderr = process.communicate(timeout=5)
        pytest.fail(
            f"Server failed to start (permissions mode).\n"
            f"stdout: {stdout.decode()[:2000]}\n"
            f"stderr: {stderr.decode()[:2000]}"
        )

    # Mint admin JWT token (--auth-type local validates JWTs, not static keys)
    admin_token = _mint_jwt(jwt_secret, "admin", "default", is_admin=True)

    yield {
        "port": port,
        "base_url": base_url,
        "process": process,
        "wal_dir": wal_dir,
        "jwt_secret": jwt_secret,
        "admin_token": admin_token,
    }

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


class TestWALWithPermissions:
    """E2E: WAL works for non-admin users with enforced permissions."""

    def test_non_admin_write_creates_wal_segments(self, nexus_server_with_permissions):
        """Non-admin user with granted permissions writes files, WAL captures events."""
        import time

        info = nexus_server_with_permissions
        base_url = info["base_url"]
        wal_dir = info["wal_dir"]
        admin_token = info["admin_token"]
        jwt_secret = info["jwt_secret"]

        user_token = _mint_jwt(jwt_secret, "alice", "default", is_admin=False)

        with httpx.Client(base_url=base_url, timeout=30.0, trust_env=False) as client:
            # 1. Health check
            resp = client.get("/health")
            assert resp.status_code == 200

            # 2. Admin: create workspace dir so non-admin can write into it
            result = _rpc(client, "mkdir", {"path": "/workspace"}, admin_token)
            assert "error" not in result or result.get("result") is not None, (
                f"Admin mkdir failed: {result}"
            )

            # 3. Admin: grant non-admin user WRITE permission via ReBAC
            # Use "direct_editor" relation (file namespace: write → editor → direct_editor)
            grant_result = _rpc(
                client,
                "rebac_create",
                {
                    "subject": ["user", "alice"],
                    "relation": "direct_editor",
                    "object": ["file", "/workspace"],
                    "zone_id": "default",
                },
                admin_token,
            )
            assert "error" not in grant_result or grant_result.get("result") is not None, (
                f"Grant failed: {grant_result}"
            )

            # 4. Non-admin: write files using JWT token
            for i in range(3):
                result = _rpc(
                    client,
                    "write",
                    {
                        "path": f"/workspace/alice-file-{i}.txt",
                        "content": _encode_bytes(f"alice-content-{i}".encode()),
                    },
                    user_token,
                )
                assert "error" not in result or result.get("result") is not None, (
                    f"Non-admin write failed: {result}"
                )

            # 5. Non-admin: verify can read back
            read_result = _rpc(
                client,
                "read",
                {"path": "/workspace/alice-file-0.txt"},
                user_token,
            )
            assert "error" not in read_result or read_result.get("result") is not None, (
                f"Non-admin read failed: {read_result}"
            )

        # 6. Check WAL segments (admin mkdir + 3 alice writes = 4+ events)
        time.sleep(1.0)

        assert wal_dir.exists(), f"WAL dir not created: {wal_dir}"
        seg_files = list(wal_dir.glob("wal-*.seg"))
        assert len(seg_files) > 0, f"No WAL segments. Files: {[f.name for f in wal_dir.iterdir()]}"
        total_bytes = sum(f.stat().st_size for f in seg_files)
        assert total_bytes > 8, f"WAL segments too small ({total_bytes} bytes)"

    def test_non_admin_denied_without_permission(self, nexus_server_with_permissions):
        """Non-admin user without permissions gets denied."""
        info = nexus_server_with_permissions
        base_url = info["base_url"]
        jwt_secret = info["jwt_secret"]
        admin_token = info["admin_token"]

        # Create a user with no grants
        user_token = _mint_jwt(jwt_secret, "bob", "default", is_admin=False)

        with httpx.Client(base_url=base_url, timeout=30.0, trust_env=False) as client:
            # Admin: create the directory
            _rpc(client, "mkdir", {"path": "/restricted"}, admin_token)

            # Bob: attempt write without permission
            result = _rpc(
                client,
                "write",
                {
                    "path": "/restricted/bob-file.txt",
                    "content": _encode_bytes(b"should fail"),
                },
                user_token,
            )
            # Should get a permission error (JSON-RPC error response)
            assert "error" in result, f"Expected permission error, got: {result}"


class TestPGFallback:
    """E2E: verify PG fallback when Rust extension is hidden."""

    def test_server_starts_without_rust_wal(self, isolated_db, tmp_path):
        """Server should start with PG fallback when _nexus_wal is unavailable."""
        from tests.e2e.conftest import find_free_port, wait_for_server

        port = find_free_port()
        base_url = f"http://127.0.0.1:{port}"

        env = os.environ.copy()
        env["NEXUS_JWT_SECRET"] = "test-secret-fallback"
        env["NEXUS_DATABASE_URL"] = f"sqlite:///{isolated_db}"
        env["NEXUS_API_KEY"] = "test-fallback-key"
        env["PYTHONPATH"] = str(
            (tmp_path / ".." / ".." / "src").resolve()
            if not os.getenv("PYTHONPATH")
            else os.getenv("PYTHONPATH")
        )

        # Block _nexus_wal import by prepending a script that poisons the module
        startup_code = (
            "import sys; "
            "sys.modules['_nexus_wal'] = None; "
            "sys.modules['nexus._nexus_wal'] = None; "
            f"from nexus.cli import main; "
            f"main(['serve', '--host', '127.0.0.1', '--port', '{port}', "
            f"'--data-dir', '{tmp_path}'])"
        )

        process = subprocess.Popen(
            [sys.executable, "-c", startup_code],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            preexec_fn=os.setsid if sys.platform != "win32" else None,
        )

        try:
            if not wait_for_server(base_url, timeout=30.0):
                process.terminate()
                stdout, stderr = process.communicate(timeout=5)
                pytest.fail(
                    f"Server failed to start (fallback mode).\n"
                    f"stdout: {stdout.decode()[:2000]}\n"
                    f"stderr: {stderr.decode()[:2000]}"
                )

            # Server is up — verify health
            with httpx.Client(base_url=base_url, timeout=10.0, trust_env=False) as client:
                resp = client.get("/health")
                assert resp.status_code == 200

                # Write a file — should succeed even without Rust WAL
                result = _rpc(
                    client,
                    "write",
                    {
                        "path": "/fallback-test.txt",
                        "content": _encode_bytes(b"fallback works"),
                    },
                    "test-fallback-key",
                )
                assert "error" not in result or result.get("result") is not None

            # No WAL segments should exist (PG fallback doesn't create .seg files)
            wal_candidates = [
                tmp_path / "wal",
                tmp_path / ".nexus-data" / "wal",
            ]
            for candidate in wal_candidates:
                if candidate.exists():
                    seg_files = list(candidate.glob("wal-*.seg"))
                    assert len(seg_files) == 0, f"Found WAL segments in fallback mode: {seg_files}"

        finally:
            import signal
            from contextlib import suppress

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
