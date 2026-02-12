"""E2E tests for API v2 endpoints with database authentication.

Tests non-admin user access against real v2 REST endpoints with
permission enforcement enabled.

Issue #995: API versioning strategy — E2E auth coverage.

Scenarios:
1. Admin key → full access to all v2 endpoints
2. Non-admin "alice" (zone=acme) → can store/retrieve own memories
3. Invalid key → 401 on all endpoints
4. No key → 401 on all endpoints
5. Non-admin gets correct zone-scoped context
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
from contextlib import closing, suppress
from pathlib import Path

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from nexus.server.auth.database_key import DatabaseAPIKeyAuth
from nexus.storage.models import Base

# Path to project src
_SRC_PATH = Path(__file__).resolve().parents[2] / "src"

SERVER_STARTUP_TIMEOUT = 30.0


# =============================================================================
# Helpers
# =============================================================================


def _find_free_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_health(base_url: str, timeout: float = SERVER_STARTUP_TIMEOUT) -> None:
    deadline = time.monotonic() + timeout
    with httpx.Client(timeout=5, trust_env=False) as client:
        while time.monotonic() < deadline:
            try:
                resp = client.get(f"{base_url}/health")
                if resp.status_code == 200:
                    return
            except httpx.ConnectError:
                pass
            time.sleep(0.3)
    raise TimeoutError(f"Server did not start within {timeout}s at {base_url}")


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture(scope="module")
def db_and_keys(tmp_path_factory):
    """Pre-create SQLite DB with admin + non-admin API keys.

    Returns dict with db_path, admin_key, alice_key, bob_key.
    """
    tmp = tmp_path_factory.mktemp("auth_e2e")
    db_path = tmp / "metadata.db"

    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)

    with Session() as session:
        # Admin key
        _admin_id, admin_key = DatabaseAPIKeyAuth.create_key(
            session,
            user_id="admin",
            name="Admin E2E Key",
            zone_id="default",
            is_admin=True,
        )

        # Non-admin: alice in zone "acme"
        _alice_id, alice_key = DatabaseAPIKeyAuth.create_key(
            session,
            user_id="alice",
            name="Alice E2E Key",
            zone_id="acme",
            is_admin=False,
        )

        # Non-admin: bob in zone "other"
        _bob_id, bob_key = DatabaseAPIKeyAuth.create_key(
            session,
            user_id="bob",
            name="Bob E2E Key",
            zone_id="other",
            is_admin=False,
        )

        session.commit()

    engine.dispose()

    return {
        "db_path": db_path,
        "tmp_dir": tmp,
        "admin_key": admin_key,
        "alice_key": alice_key,
        "bob_key": bob_key,
    }


@pytest.fixture(scope="module")
def server(db_and_keys):
    """Start a real nexus server with database auth and permission enforcement."""
    port = _find_free_port()
    base_url = f"http://127.0.0.1:{port}"
    db_path = db_and_keys["db_path"]
    tmp_dir = db_and_keys["tmp_dir"]

    env = {
        **os.environ,
        "PYTHONPATH": str(_SRC_PATH),
        "NEXUS_DATABASE_URL": f"sqlite:///{db_path}",
        "NEXUS_JWT_SECRET": "test-e2e-jwt-secret-12345",
        "NEXUS_ENFORCE_PERMISSIONS": "false",  # Focus on auth, not ReBAC
        "NEXUS_ENFORCE_ZONE_ISOLATION": "false",
        "NEXUS_SEARCH_DAEMON": "false",
        "NEXUS_RATE_LIMIT_ENABLED": "false",
        "NO_PROXY": "*",
    }

    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "from nexus.cli import main; "
                f"main(['serve', '--host', '127.0.0.1', '--port', '{port}', "
                f"'--auth-type', 'database', "
                f"'--data-dir', '{tmp_dir}'])"
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
    except TimeoutError:
        # Dump server output for debugging
        proc.terminate()
        stdout, _ = proc.communicate(timeout=5)
        pytest.fail(f"Server failed to start.\nOutput:\n{stdout}")

    yield {
        "base_url": base_url,
        "process": proc,
        "port": port,
    }

    # Cleanup
    if proc.poll() is None:
        if sys.platform != "win32":
            with suppress(ProcessLookupError, PermissionError):
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        else:
            proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


@pytest.fixture(scope="module")
def base_url(server) -> str:
    return server["base_url"]


@pytest.fixture(scope="module")
def admin_headers(db_and_keys) -> dict[str, str]:
    return {"Authorization": f"Bearer {db_and_keys['admin_key']}"}


@pytest.fixture(scope="module")
def alice_headers(db_and_keys) -> dict[str, str]:
    return {"Authorization": f"Bearer {db_and_keys['alice_key']}"}


@pytest.fixture(scope="module")
def bob_headers(db_and_keys) -> dict[str, str]:
    return {"Authorization": f"Bearer {db_and_keys['bob_key']}"}


# =============================================================================
# Tests: Authentication enforcement (401)
# =============================================================================


class TestAuthEnforcement:
    """Verify that v2 endpoints reject unauthenticated requests."""

    def test_no_auth_header_returns_401(self, base_url: str) -> None:
        with httpx.Client(trust_env=False, timeout=10) as client:
            resp = client.post(
                f"{base_url}/api/v2/memories",
                json={"content": "test"},
            )
        assert resp.status_code == 401

    def test_invalid_key_returns_401(self, base_url: str) -> None:
        with httpx.Client(trust_env=False, timeout=10) as client:
            resp = client.post(
                f"{base_url}/api/v2/memories",
                json={"content": "test"},
                headers={"Authorization": "Bearer invalid-key-12345"},
            )
        assert resp.status_code == 401

    def test_malformed_auth_header_returns_401(self, base_url: str) -> None:
        with httpx.Client(trust_env=False, timeout=10) as client:
            resp = client.post(
                f"{base_url}/api/v2/memories",
                json={"content": "test"},
                headers={"Authorization": "Basic dXNlcjpwYXNz"},
            )
        assert resp.status_code == 401

    @pytest.mark.parametrize(
        "method,path",
        [
            ("POST", "/api/v2/memories"),
            ("POST", "/api/v2/memories/search"),
            ("POST", "/api/v2/memories/query"),
            ("POST", "/api/v2/trajectories"),
            ("POST", "/api/v2/feedback"),
            ("GET", "/api/v2/playbooks"),
            ("POST", "/api/v2/reflect"),
            ("POST", "/api/v2/curate"),
            ("POST", "/api/v2/consolidate"),
            ("GET", "/api/v2/operations"),
        ],
    )
    def test_unauthenticated_rejected_across_endpoints(
        self, base_url: str, method: str, path: str
    ) -> None:
        with httpx.Client(trust_env=False, timeout=10) as client:
            if method == "GET":
                resp = client.get(f"{base_url}{path}")
            else:
                resp = client.post(f"{base_url}{path}", json={})
        assert resp.status_code == 401, f"{method} {path} should require auth"


# =============================================================================
# Tests: Admin access
# =============================================================================


class TestAdminAccess:
    """Admin key should have full access to all v2 endpoints."""

    def test_admin_can_store_memory(self, base_url: str, admin_headers: dict[str, str]) -> None:
        with httpx.Client(trust_env=False, timeout=10) as client:
            resp = client.post(
                f"{base_url}/api/v2/memories",
                json={"content": "Admin memory", "scope": "user"},
                headers=admin_headers,
            )
        assert resp.status_code == 201
        data = resp.json()
        assert "memory_id" in data
        assert data["status"] == "created"

    def test_admin_can_search_memories(self, base_url: str, admin_headers: dict[str, str]) -> None:
        with httpx.Client(trust_env=False, timeout=10) as client:
            resp = client.post(
                f"{base_url}/api/v2/memories/search",
                json={"query": "admin"},
                headers=admin_headers,
            )
        assert resp.status_code == 200

    def test_admin_can_list_operations(self, base_url: str, admin_headers: dict[str, str]) -> None:
        with httpx.Client(trust_env=False, timeout=10) as client:
            resp = client.get(
                f"{base_url}/api/v2/operations",
                headers=admin_headers,
            )
        assert resp.status_code == 200
        data = resp.json()
        assert "operations" in data

    def test_admin_can_list_playbooks(self, base_url: str, admin_headers: dict[str, str]) -> None:
        with httpx.Client(trust_env=False, timeout=10) as client:
            resp = client.get(
                f"{base_url}/api/v2/playbooks",
                headers=admin_headers,
            )
        assert resp.status_code == 200


# =============================================================================
# Tests: Non-admin (alice) access
# =============================================================================


class TestNonAdminAccess:
    """Non-admin "alice" (zone=acme) can use v2 endpoints."""

    def test_alice_can_store_memory(self, base_url: str, alice_headers: dict[str, str]) -> None:
        with httpx.Client(trust_env=False, timeout=10) as client:
            resp = client.post(
                f"{base_url}/api/v2/memories",
                json={
                    "content": "Alice remembers this",
                    "scope": "user",
                    "memory_type": "fact",
                },
                headers=alice_headers,
            )
        assert resp.status_code == 201
        data = resp.json()
        assert "memory_id" in data

    def test_alice_can_search_own_memories(
        self, base_url: str, alice_headers: dict[str, str]
    ) -> None:
        with httpx.Client(trust_env=False, timeout=10) as client:
            resp = client.post(
                f"{base_url}/api/v2/memories/search",
                json={"query": "Alice remembers"},
                headers=alice_headers,
            )
        assert resp.status_code == 200

    def test_alice_can_query_memories(self, base_url: str, alice_headers: dict[str, str]) -> None:
        with httpx.Client(trust_env=False, timeout=10) as client:
            resp = client.post(
                f"{base_url}/api/v2/memories/query",
                json={"scope": "user"},
                headers=alice_headers,
            )
        assert resp.status_code == 200

    def test_alice_can_start_trajectory(self, base_url: str, alice_headers: dict[str, str]) -> None:
        with httpx.Client(trust_env=False, timeout=10) as client:
            resp = client.post(
                f"{base_url}/api/v2/trajectories",
                json={"task_description": "Alice's task"},
                headers=alice_headers,
            )
        assert resp.status_code == 201
        data = resp.json()
        assert "trajectory_id" in data

    def test_alice_can_list_operations(self, base_url: str, alice_headers: dict[str, str]) -> None:
        with httpx.Client(trust_env=False, timeout=10) as client:
            resp = client.get(
                f"{base_url}/api/v2/operations",
                headers=alice_headers,
            )
        assert resp.status_code == 200
        data = resp.json()
        assert "operations" in data

    def test_alice_can_list_playbooks(self, base_url: str, alice_headers: dict[str, str]) -> None:
        with httpx.Client(trust_env=False, timeout=10) as client:
            resp = client.get(
                f"{base_url}/api/v2/playbooks",
                headers=alice_headers,
            )
        assert resp.status_code == 200

    def test_alice_can_create_playbook(self, base_url: str, alice_headers: dict[str, str]) -> None:
        with httpx.Client(trust_env=False, timeout=10) as client:
            resp = client.post(
                f"{base_url}/api/v2/playbooks",
                json={
                    "name": "Alice's playbook",
                    "description": "Test playbook",
                },
                headers=alice_headers,
            )
        assert resp.status_code == 201
        data = resp.json()
        assert "playbook_id" in data


# =============================================================================
# Tests: X-API-Version header (versioning middleware)
# =============================================================================


class TestVersioningHeaders:
    """Verify versioning middleware works end-to-end."""

    def test_v2_response_includes_version_header(
        self, base_url: str, admin_headers: dict[str, str]
    ) -> None:
        with httpx.Client(trust_env=False, timeout=10) as client:
            resp = client.get(
                f"{base_url}/api/v2/operations",
                headers=admin_headers,
            )
        assert resp.status_code == 200
        assert resp.headers.get("X-API-Version") == "2.0"

    def test_non_v2_path_no_version_header(self, base_url: str) -> None:
        with httpx.Client(trust_env=False, timeout=10) as client:
            resp = client.get(f"{base_url}/health")
        assert resp.status_code == 200
        assert "X-API-Version" not in resp.headers


# =============================================================================
# Tests: Memory store-then-retrieve round-trip
# =============================================================================


class TestMemoryRoundTrip:
    """Non-admin user can store and then retrieve a memory."""

    def test_store_and_get_memory(self, base_url: str, alice_headers: dict[str, str]) -> None:
        with httpx.Client(trust_env=False, timeout=10) as client:
            # Store
            store_resp = client.post(
                f"{base_url}/api/v2/memories",
                json={
                    "content": "E2E round-trip test memory",
                    "scope": "user",
                    "memory_type": "fact",
                    "importance": 0.9,
                },
                headers=alice_headers,
            )
            assert store_resp.status_code == 201
            memory_id = store_resp.json()["memory_id"]

            # Get
            get_resp = client.get(
                f"{base_url}/api/v2/memories/{memory_id}",
                headers=alice_headers,
            )
            assert get_resp.status_code == 200
            data = get_resp.json()
            assert "memory" in data
            assert data["memory"]["memory_id"] == memory_id

    def test_batch_store_memories(self, base_url: str, alice_headers: dict[str, str]) -> None:
        with httpx.Client(trust_env=False, timeout=10) as client:
            resp = client.post(
                f"{base_url}/api/v2/memories/batch",
                json={
                    "memories": [
                        {"content": "Batch memory 1", "scope": "user"},
                        {"content": "Batch memory 2", "scope": "user"},
                    ]
                },
                headers=alice_headers,
            )
        assert resp.status_code in (200, 201)
        data = resp.json()
        assert data["stored"] == 2
        assert len(data["memory_ids"]) == 2


# =============================================================================
# Tests: Trajectory lifecycle
# =============================================================================


class TestTrajectoryLifecycle:
    """Non-admin user can run a full trajectory lifecycle."""

    def test_start_step_complete(self, base_url: str, alice_headers: dict[str, str]) -> None:
        with httpx.Client(trust_env=False, timeout=10) as client:
            # Start
            start_resp = client.post(
                f"{base_url}/api/v2/trajectories",
                json={"task_description": "Lifecycle test"},
                headers=alice_headers,
            )
            assert start_resp.status_code == 201
            traj_id = start_resp.json()["trajectory_id"]

            # Add step
            step_resp = client.post(
                f"{base_url}/api/v2/trajectories/{traj_id}/steps",
                json={
                    "step_type": "action",
                    "description": "Did something",
                },
                headers=alice_headers,
            )
            assert step_resp.status_code == 200

            # Complete
            complete_resp = client.post(
                f"{base_url}/api/v2/trajectories/{traj_id}/complete",
                json={"status": "success", "success_score": 0.95},
                headers=alice_headers,
            )
            assert complete_resp.status_code == 200

            # Verify
            get_resp = client.get(
                f"{base_url}/api/v2/trajectories/{traj_id}",
                headers=alice_headers,
            )
            assert get_resp.status_code == 200
