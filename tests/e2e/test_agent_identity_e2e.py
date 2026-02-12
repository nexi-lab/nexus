"""E2E tests for Agent Identity (KYA Phase 1, Issue #1355).

Tests the full identity stack against real PostgreSQL and FastAPI server:
1. AgentKeyService with PostgreSQL (direct)
2. Identity API endpoints with admin user (admin bypass)
3. Identity API endpoints with non-admin agent (ownership enforcement)
4. Unauthenticated request rejection
5. Agent unregistration cascades to key deletion

Requirements:
    - PostgreSQL running at postgresql://scorpio@localhost:5432/nexus_e2e_test
    - Start with: docker start scorpio-postgres

Run with:
    pytest tests/e2e/test_agent_identity_e2e.py -v --override-ini="addopts="
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
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from nexus.core.agent_key_service import AgentKeyService
from nexus.server.auth.oauth_crypto import OAuthCrypto
from nexus.storage.models import Base

POSTGRES_URL = os.getenv(
    "NEXUS_E2E_DATABASE_URL",
    "postgresql://scorpio@localhost:5432/nexus_e2e_test",
)

_src_path = Path(__file__).parent.parent.parent / "src"
if str(_src_path) not in sys.path:
    sys.path.insert(0, str(_src_path))


def find_free_port() -> int:
    """Find a free port on localhost."""
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("", 0))
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        return s.getsockname()[1]


def wait_for_server(url: str, timeout: float = 30.0) -> bool:
    """Wait for server to be ready by polling /health endpoint."""
    start = time.time()
    while time.time() - start < timeout:
        try:
            response = httpx.get(f"{url}/health", timeout=1.0, trust_env=False)
            if response.status_code == 200:
                return True
        except (httpx.ConnectError, httpx.ReadTimeout):
            pass
        time.sleep(0.1)
    return False


# ---------------------------------------------------------------------------
# PostgreSQL fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def pg_engine():
    """Create PostgreSQL engine for E2E testing."""
    try:
        engine = create_engine(POSTGRES_URL, echo=False, pool_pre_ping=True)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as e:
        pytest.skip(f"PostgreSQL not available at {POSTGRES_URL}: {e}")

    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def pg_session_factory(pg_engine):
    """Create a session factory for PostgreSQL."""
    return sessionmaker(bind=pg_engine, expire_on_commit=False)


@pytest.fixture
def pg_crypto():
    """OAuthCrypto for test key encryption."""
    return OAuthCrypto()


@pytest.fixture
def pg_key_service(pg_session_factory, pg_crypto):
    """AgentKeyService backed by PostgreSQL."""
    service = AgentKeyService(
        session_factory=pg_session_factory,
        crypto=pg_crypto,
        cache_maxsize=100,
        cache_ttl=10,
    )
    yield service

    # Cleanup: remove test keys
    with pg_session_factory() as session:
        session.execute(text("DELETE FROM agent_keys WHERE agent_id LIKE 'e2e-%'"))
        session.commit()


# ---------------------------------------------------------------------------
# 1. AgentKeyService with PostgreSQL
# ---------------------------------------------------------------------------


class TestAgentKeyServicePostgreSQL:
    """Test AgentKeyService operations against real PostgreSQL."""

    def test_generate_and_retrieve(self, pg_key_service, pg_session_factory):
        """Generate a key pair and retrieve it from PostgreSQL."""
        with pg_session_factory() as session:
            record = pg_key_service.generate_key_pair("e2e-key-agent-1", "default", session)
            session.commit()

        assert record.algorithm == "Ed25519"
        assert record.public_key_jwk["kty"] == "OKP"

        # Retrieve
        retrieved = pg_key_service.get_public_key("e2e-key-agent-1")
        assert retrieved is not None
        assert retrieved.key_id == record.key_id

    def test_revoke_key_postgres(self, pg_key_service, pg_session_factory):
        """Revoke a key in PostgreSQL."""
        with pg_session_factory() as session:
            record = pg_key_service.generate_key_pair("e2e-key-agent-2", None, session)
            session.commit()

        assert pg_key_service.revoke_key("e2e-key-agent-2", record.key_id) is True

        # Clear cache and verify
        with pg_key_service._cache_lock:
            pg_key_service._key_cache.clear()

        assert pg_key_service.get_public_key("e2e-key-agent-2") is None

    def test_key_rotation_postgres(self, pg_key_service, pg_session_factory):
        """Key rotation with PostgreSQL."""
        with pg_session_factory() as session:
            old_record = pg_key_service.generate_key_pair("e2e-key-agent-3", None, session)
            session.commit()

        with pg_session_factory() as session:
            new_record = pg_key_service.generate_key_pair("e2e-key-agent-3", None, session)
            session.commit()

        assert old_record.key_id != new_record.key_id

        keys = pg_key_service.list_keys("e2e-key-agent-3")
        assert len(keys) == 2

    def test_verify_identity_postgres(self, pg_key_service, pg_session_factory):
        """verify_identity works with PostgreSQL."""
        with pg_session_factory() as session:
            pg_key_service.generate_key_pair("e2e-key-agent-4", "zone-1", session)
            session.commit()

        info = pg_key_service.verify_identity("e2e-key-agent-4", "alice", "zone-1")
        assert info is not None
        assert info.agent_id == "e2e-key-agent-4"
        assert info.owner_id == "alice"
        assert info.algorithm == "Ed25519"

    def test_table_and_indexes_exist(self, pg_engine):
        """agent_keys table and indexes exist in PostgreSQL."""
        with pg_engine.connect() as conn:
            result = conn.execute(
                text(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_name = 'agent_keys'"
                )
            )
            tables = [row[0] for row in result]
            assert "agent_keys" in tables

            result = conn.execute(
                text("SELECT indexname FROM pg_indexes WHERE tablename = 'agent_keys'")
            )
            indexes = {row[0] for row in result}
            assert "idx_agent_keys_agent_id" in indexes
            assert "idx_agent_keys_agent_active" in indexes
            assert "idx_agent_keys_zone" in indexes


# ---------------------------------------------------------------------------
# Shared server fixture for API tests
# ---------------------------------------------------------------------------


@pytest.fixture(scope="class")
def nexus_server_pg(tmp_path_factory, pg_engine):
    """Start nexus server with PostgreSQL and database auth.

    Scope=class so the server is shared across tests within a class,
    reducing startup overhead.
    """
    tmp_path = tmp_path_factory.mktemp("identity_e2e")
    storage_path = tmp_path / "storage"
    storage_path.mkdir(exist_ok=True)

    port = find_free_port()
    base_url = f"http://127.0.0.1:{port}"

    env = os.environ.copy()
    env["NEXUS_JWT_SECRET"] = "test-secret-key-for-e2e-12345"
    env["NEXUS_DATABASE_URL"] = POSTGRES_URL
    env["PYTHONPATH"] = str(_src_path)
    env["NEXUS_ENFORCE_PERMISSIONS"] = "true"

    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "from nexus.cli import main; "
                f"main(['serve', '--host', '127.0.0.1', '--port', '{port}', "
                f"'--data-dir', '{tmp_path}', '--auth-type', 'database', "
                f"'--init', '--reset', '--admin-user', 'e2e-identity-admin'])"
            ),
        ],
        env=env,
        cwd=str(tmp_path),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        preexec_fn=os.setsid if sys.platform != "win32" else None,
    )

    if not wait_for_server(base_url, timeout=30.0):
        process.terminate()
        stdout, stderr = process.communicate(timeout=5)
        pytest.fail(
            f"Server failed to start on port {port}.\n"
            f"stdout: {stdout.decode()[:2000]}\n"
            f"stderr: {stderr.decode()[:2000]}"
        )

    admin_env_file = tmp_path / ".nexus-admin-env"
    api_key = None
    if admin_env_file.exists():
        for line in admin_env_file.read_text().splitlines():
            if "NEXUS_API_KEY=" in line:
                value = line.split("NEXUS_API_KEY=", 1)[1].strip()
                api_key = value.strip("'\"")
                break

    yield {
        "port": port,
        "base_url": base_url,
        "process": process,
        "api_key": api_key,
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


def _register_agent(base_url: str, api_key: str, agent_suffix: str) -> dict:
    """Register an agent via RPC and return {agent_id, api_key}.

    Creates agent with generate_api_key=True so the agent gets its own
    non-admin API key for authentication.
    """
    agent_id = f"e2e-identity-admin,{agent_suffix}"
    response = httpx.post(
        f"{base_url}/api/nfs/register_agent",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "jsonrpc": "2.0",
            "method": "register_agent",
            "params": {
                "agent_id": agent_id,
                "name": f"E2E {agent_suffix}",
                "generate_api_key": True,
            },
            "id": 1,
        },
        timeout=10.0,
        trust_env=False,
    )
    assert response.status_code == 200, f"register_agent failed: {response.text}"
    data = response.json()
    assert data.get("error") is None, f"RPC error: {data.get('error')}"
    return {
        "agent_id": agent_id,
        "api_key": data.get("result", {}).get("api_key"),
    }


def _ensure_agent_has_key(base_url: str, api_key: str, agent_id: str) -> str:
    """Rotate a key for the agent and return the new key_id."""
    resp = httpx.post(
        f"{base_url}/api/v2/agents/{agent_id}/keys/rotate",
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=10.0,
        trust_env=False,
    )
    assert resp.status_code == 200, f"rotate failed: {resp.text}"
    return resp.json()["new_key"]["key_id"]


# ---------------------------------------------------------------------------
# 2. Identity API with admin user
# ---------------------------------------------------------------------------


class TestIdentityAPIAdmin:
    """E2E tests using admin API key — admin bypasses ownership checks."""

    def test_verify_agent_with_key(self, nexus_server_pg):
        """POST /agents/{id}/verify returns identity info (admin)."""
        api_key = nexus_server_pg["api_key"]
        if not api_key:
            pytest.skip("Admin API key not found")

        base_url = nexus_server_pg["base_url"]
        agent = _register_agent(base_url, api_key, "AdminVerify")
        _ensure_agent_has_key(base_url, api_key, agent["agent_id"])

        response = httpx.post(
            f"{base_url}/api/v2/agents/{agent['agent_id']}/verify",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=10.0,
            trust_env=False,
        )
        assert response.status_code == 200, f"verify failed: {response.text}"
        data = response.json()
        assert data["agent_id"] == agent["agent_id"]
        assert data["algorithm"] == "Ed25519"
        assert "key_id" in data
        assert data["public_key_jwk"]["kty"] == "OKP"
        assert data["public_key_jwk"]["crv"] == "Ed25519"

    def test_verify_nonexistent_agent(self, nexus_server_pg):
        """POST /agents/{id}/verify returns 404 for non-existent agent."""
        api_key = nexus_server_pg["api_key"]
        if not api_key:
            pytest.skip("Admin API key not found")

        response = httpx.post(
            f"{nexus_server_pg['base_url']}/api/v2/agents/nonexistent-agent/verify",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=10.0,
            trust_env=False,
        )
        assert response.status_code == 404

    def test_list_agent_keys(self, nexus_server_pg):
        """GET /agents/{id}/keys returns key list (admin)."""
        api_key = nexus_server_pg["api_key"]
        if not api_key:
            pytest.skip("Admin API key not found")

        base_url = nexus_server_pg["base_url"]
        agent = _register_agent(base_url, api_key, "AdminListKeys")
        _ensure_agent_has_key(base_url, api_key, agent["agent_id"])

        response = httpx.get(
            f"{base_url}/api/v2/agents/{agent['agent_id']}/keys",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=10.0,
            trust_env=False,
        )
        assert response.status_code == 200
        data = response.json()
        assert "keys" in data
        assert len(data["keys"]) >= 1
        assert data["keys"][0]["algorithm"] == "Ed25519"

    def test_rotate_key_admin(self, nexus_server_pg):
        """POST /agents/{id}/keys/rotate creates a new key (admin)."""
        api_key = nexus_server_pg["api_key"]
        if not api_key:
            pytest.skip("Admin API key not found")

        base_url = nexus_server_pg["base_url"]
        agent = _register_agent(base_url, api_key, "AdminRotate")

        resp1 = httpx.post(
            f"{base_url}/api/v2/agents/{agent['agent_id']}/keys/rotate",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=10.0,
            trust_env=False,
        )
        assert resp1.status_code == 200
        key1 = resp1.json()["new_key"]

        resp2 = httpx.post(
            f"{base_url}/api/v2/agents/{agent['agent_id']}/keys/rotate",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=10.0,
            trust_env=False,
        )
        assert resp2.status_code == 200
        key2 = resp2.json()["new_key"]
        assert key1["key_id"] != key2["key_id"]

    def test_revoke_key_admin(self, nexus_server_pg):
        """DELETE /agents/{id}/keys/{key_id} revokes the key (admin)."""
        api_key = nexus_server_pg["api_key"]
        if not api_key:
            pytest.skip("Admin API key not found")

        base_url = nexus_server_pg["base_url"]
        agent = _register_agent(base_url, api_key, "AdminRevoke")
        key_id = _ensure_agent_has_key(base_url, api_key, agent["agent_id"])

        revoke_resp = httpx.delete(
            f"{base_url}/api/v2/agents/{agent['agent_id']}/keys/{key_id}",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=10.0,
            trust_env=False,
        )
        assert revoke_resp.status_code == 200
        assert revoke_resp.json()["key_id"] == key_id
        assert "revoked_at" in revoke_resp.json()


# ---------------------------------------------------------------------------
# 3. Identity API with non-admin agent (ownership enforcement)
# ---------------------------------------------------------------------------


class TestIdentityAPIAgentAuth:
    """E2E tests using agent's own API key — non-admin, ownership enforced.

    This is the critical test class: verifies that agents can manage their
    own identity using their non-admin API keys, with proper ownership
    enforcement via NEXUS_ENFORCE_PERMISSIONS=true.
    """

    def test_agent_can_verify_own_identity(self, nexus_server_pg):
        """Agent authenticating with own API key can verify its identity."""
        admin_key = nexus_server_pg["api_key"]
        if not admin_key:
            pytest.skip("Admin API key not found")

        base_url = nexus_server_pg["base_url"]

        # Register agent and get its own API key
        agent = _register_agent(base_url, admin_key, "AgentSelfVerify")
        agent_api_key = agent["api_key"]
        if not agent_api_key:
            pytest.skip("Agent API key not returned")

        # Create a key using admin (agent doesn't have a key yet)
        _ensure_agent_has_key(base_url, admin_key, agent["agent_id"])

        # Agent verifies its own identity with its own API key
        response = httpx.post(
            f"{base_url}/api/v2/agents/{agent['agent_id']}/verify",
            headers={"Authorization": f"Bearer {agent_api_key}"},
            timeout=10.0,
            trust_env=False,
        )
        assert response.status_code == 200, f"verify failed: {response.text}"
        data = response.json()
        assert data["agent_id"] == agent["agent_id"]
        assert data["algorithm"] == "Ed25519"

    def test_agent_can_list_own_keys(self, nexus_server_pg):
        """Agent authenticating with own API key can list its keys."""
        admin_key = nexus_server_pg["api_key"]
        if not admin_key:
            pytest.skip("Admin API key not found")

        base_url = nexus_server_pg["base_url"]

        agent = _register_agent(base_url, admin_key, "AgentListKeys")
        agent_api_key = agent["api_key"]
        if not agent_api_key:
            pytest.skip("Agent API key not returned")

        _ensure_agent_has_key(base_url, admin_key, agent["agent_id"])

        # Agent lists its own keys
        response = httpx.get(
            f"{base_url}/api/v2/agents/{agent['agent_id']}/keys",
            headers={"Authorization": f"Bearer {agent_api_key}"},
            timeout=10.0,
            trust_env=False,
        )
        assert response.status_code == 200, f"list keys failed: {response.text}"
        data = response.json()
        assert len(data["keys"]) >= 1

    def test_agent_can_rotate_own_keys(self, nexus_server_pg):
        """Agent authenticating with own API key can rotate its keys (ownership check)."""
        admin_key = nexus_server_pg["api_key"]
        if not admin_key:
            pytest.skip("Admin API key not found")

        base_url = nexus_server_pg["base_url"]

        agent = _register_agent(base_url, admin_key, "AgentRotate")
        agent_api_key = agent["api_key"]
        if not agent_api_key:
            pytest.skip("Agent API key not returned")

        # Agent rotates its own key — requires ownership check to pass
        response = httpx.post(
            f"{base_url}/api/v2/agents/{agent['agent_id']}/keys/rotate",
            headers={"Authorization": f"Bearer {agent_api_key}"},
            timeout=10.0,
            trust_env=False,
        )
        assert response.status_code == 200, f"rotate failed: {response.text}"
        data = response.json()
        assert "new_key" in data
        assert data["new_key"]["algorithm"] == "Ed25519"

    def test_agent_can_revoke_own_keys(self, nexus_server_pg):
        """Agent authenticating with own API key can revoke its keys (ownership check)."""
        admin_key = nexus_server_pg["api_key"]
        if not admin_key:
            pytest.skip("Admin API key not found")

        base_url = nexus_server_pg["base_url"]

        agent = _register_agent(base_url, admin_key, "AgentRevoke")
        agent_api_key = agent["api_key"]
        if not agent_api_key:
            pytest.skip("Agent API key not returned")

        # Create a key first
        key_id = _ensure_agent_has_key(base_url, admin_key, agent["agent_id"])

        # Agent revokes its own key
        response = httpx.delete(
            f"{base_url}/api/v2/agents/{agent['agent_id']}/keys/{key_id}",
            headers={"Authorization": f"Bearer {agent_api_key}"},
            timeout=10.0,
            trust_env=False,
        )
        assert response.status_code == 200, f"revoke failed: {response.text}"
        assert response.json()["key_id"] == key_id
        assert "revoked_at" in response.json()

    def test_agent_can_verify_another_agent(self, nexus_server_pg):
        """Agent can verify another agent's identity (read, open to all)."""
        admin_key = nexus_server_pg["api_key"]
        if not admin_key:
            pytest.skip("Admin API key not found")

        base_url = nexus_server_pg["base_url"]

        # Register two agents
        agent_a = _register_agent(base_url, admin_key, "AgentCrossVerifyA")
        agent_b = _register_agent(base_url, admin_key, "AgentCrossVerifyB")
        agent_a_key = agent_a["api_key"]
        if not agent_a_key:
            pytest.skip("Agent A API key not returned")

        # Give agent B a key
        _ensure_agent_has_key(base_url, admin_key, agent_b["agent_id"])

        # Agent A verifies Agent B's identity (read = open to all authenticated)
        response = httpx.post(
            f"{base_url}/api/v2/agents/{agent_b['agent_id']}/verify",
            headers={"Authorization": f"Bearer {agent_a_key}"},
            timeout=10.0,
            trust_env=False,
        )
        assert response.status_code == 200, f"cross-verify failed: {response.text}"
        data = response.json()
        assert data["agent_id"] == agent_b["agent_id"]

    def test_agent_full_lifecycle(self, nexus_server_pg):
        """Full key lifecycle with non-admin agent: rotate → verify → revoke → verify fails."""
        admin_key = nexus_server_pg["api_key"]
        if not admin_key:
            pytest.skip("Admin API key not found")

        base_url = nexus_server_pg["base_url"]

        agent = _register_agent(base_url, admin_key, "AgentLifecycle")
        agent_api_key = agent["api_key"]
        if not agent_api_key:
            pytest.skip("Agent API key not returned")

        agent_id = agent["agent_id"]
        headers = {"Authorization": f"Bearer {agent_api_key}"}

        # 1. Rotate (create key) using agent's own API key
        rotate_resp = httpx.post(
            f"{base_url}/api/v2/agents/{agent_id}/keys/rotate",
            headers=headers,
            timeout=10.0,
            trust_env=False,
        )
        assert rotate_resp.status_code == 200
        key_id = rotate_resp.json()["new_key"]["key_id"]

        # 2. Verify — should succeed
        verify_resp = httpx.post(
            f"{base_url}/api/v2/agents/{agent_id}/verify",
            headers=headers,
            timeout=10.0,
            trust_env=False,
        )
        assert verify_resp.status_code == 200
        assert verify_resp.json()["key_id"] == key_id

        # 3. List keys — should show the key
        list_resp = httpx.get(
            f"{base_url}/api/v2/agents/{agent_id}/keys",
            headers=headers,
            timeout=10.0,
            trust_env=False,
        )
        assert list_resp.status_code == 200
        assert len(list_resp.json()["keys"]) >= 1

        # 4. Revoke using agent's own API key
        revoke_resp = httpx.delete(
            f"{base_url}/api/v2/agents/{agent_id}/keys/{key_id}",
            headers=headers,
            timeout=10.0,
            trust_env=False,
        )
        assert revoke_resp.status_code == 200

        # 5. Verify — should fail (no active key)
        verify_resp2 = httpx.post(
            f"{base_url}/api/v2/agents/{agent_id}/verify",
            headers=headers,
            timeout=10.0,
            trust_env=False,
        )
        assert verify_resp2.status_code == 404


# ---------------------------------------------------------------------------
# 4. Unauthenticated / bad auth
# ---------------------------------------------------------------------------


class TestIdentityAPIAuth:
    """E2E tests for authentication enforcement on identity endpoints."""

    def test_unauthenticated_verify_rejected(self, nexus_server_pg):
        """POST /agents/{id}/verify without auth is rejected."""
        base_url = nexus_server_pg["base_url"]

        response = httpx.post(
            f"{base_url}/api/v2/agents/any-agent/verify",
            timeout=10.0,
            trust_env=False,
        )
        # Should get 401 or 403 (depends on auth middleware)
        assert response.status_code in (401, 403), (
            f"Expected 401/403, got {response.status_code}: {response.text}"
        )

    def test_unauthenticated_list_keys_rejected(self, nexus_server_pg):
        """GET /agents/{id}/keys without auth is rejected."""
        base_url = nexus_server_pg["base_url"]

        response = httpx.get(
            f"{base_url}/api/v2/agents/any-agent/keys",
            timeout=10.0,
            trust_env=False,
        )
        assert response.status_code in (401, 403)

    def test_unauthenticated_rotate_rejected(self, nexus_server_pg):
        """POST /agents/{id}/keys/rotate without auth is rejected."""
        base_url = nexus_server_pg["base_url"]

        response = httpx.post(
            f"{base_url}/api/v2/agents/any-agent/keys/rotate",
            timeout=10.0,
            trust_env=False,
        )
        assert response.status_code in (401, 403)

    def test_unauthenticated_revoke_rejected(self, nexus_server_pg):
        """DELETE /agents/{id}/keys/{key_id} without auth is rejected."""
        base_url = nexus_server_pg["base_url"]

        response = httpx.delete(
            f"{base_url}/api/v2/agents/any-agent/keys/fake-key-id",
            timeout=10.0,
            trust_env=False,
        )
        assert response.status_code in (401, 403)

    def test_invalid_bearer_token_rejected(self, nexus_server_pg):
        """Request with invalid bearer token is rejected."""
        base_url = nexus_server_pg["base_url"]

        response = httpx.post(
            f"{base_url}/api/v2/agents/any-agent/verify",
            headers={"Authorization": "Bearer invalid-token-12345"},
            timeout=10.0,
            trust_env=False,
        )
        assert response.status_code in (401, 403)


# ---------------------------------------------------------------------------
# 5. Cascade deletion
# ---------------------------------------------------------------------------


class TestIdentityAPICascade:
    """E2E tests for agent unregistration cascading to key deletion."""

    def test_agent_unregistration_cascades_to_keys(self, nexus_server_pg):
        """Deleting an agent also deletes its keys."""
        api_key = nexus_server_pg["api_key"]
        if not api_key:
            pytest.skip("Admin API key not found")

        base_url = nexus_server_pg["base_url"]
        agent = _register_agent(base_url, api_key, "CascadeAgent")
        headers = {"Authorization": f"Bearer {api_key}"}

        # Create key
        _ensure_agent_has_key(base_url, api_key, agent["agent_id"])

        # Delete agent via RPC
        httpx.post(
            f"{base_url}/api/nfs/delete_agent",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "method": "delete_agent",
                "params": {"agent_id": agent["agent_id"]},
                "id": 1,
            },
            timeout=10.0,
            trust_env=False,
        )

        # Verify should fail (agent gone)
        verify_resp = httpx.post(
            f"{base_url}/api/v2/agents/{agent['agent_id']}/verify",
            headers=headers,
            timeout=10.0,
            trust_env=False,
        )
        assert verify_resp.status_code == 404

        # List keys should fail (agent gone)
        keys_resp = httpx.get(
            f"{base_url}/api/v2/agents/{agent['agent_id']}/keys",
            headers=headers,
            timeout=10.0,
            trust_env=False,
        )
        assert keys_resp.status_code == 404
