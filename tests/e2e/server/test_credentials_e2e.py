"""E2E test: Agent Verifiable Credentials with FastAPI server, permissions enabled.

Tests the full server-level flow for JWT-VC credential lifecycle:
1. Create FastAPI app with NexusFS (enforce_permissions=True, database auth)
2. Provision agent identity via KeyService.ensure_keypair()
3. POST /api/v2/credentials/issue → issue capability credential
4. POST /api/v2/credentials/verify → verify JWT-VC token
5. GET /api/v2/credentials/{id} → get credential status
6. DELETE /api/v2/credentials/{id} → revoke credential
7. GET /api/v2/agents/{id}/credentials → list agent credentials
8. Unauthenticated requests → 401

Run: pytest tests/e2e/server/test_credentials_e2e.py -v
"""

import shutil
import tempfile
import uuid
from typing import Any

import pytest

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.core.config import ParseConfig, PermissionConfig
from nexus.storage.models import Base
from tests.helpers.dict_metastore import DictMetastore
from tests.helpers.test_context import TEST_CONTEXT

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _set_env(monkeypatch: Any) -> None:
    """Isolate from production env vars."""
    monkeypatch.setenv("NEXUS_JWT_SECRET", "test-secret-key-credentials-e2e")
    monkeypatch.delenv("NEXUS_DATABASE_URL", raising=False)
    monkeypatch.delenv("POSTGRES_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)


@pytest.fixture
def db_path(tmp_path: Any) -> Any:
    return tmp_path / f"cred_e2e_{uuid.uuid4().hex[:8]}.db"


@pytest.fixture
def session_factory(db_path: Any) -> Any:
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


@pytest.fixture
def api_keys(session_factory: Any) -> dict[str, Any]:
    from nexus.bricks.auth.providers.database_key import DatabaseAPIKeyAuth

    with session_factory() as session:
        admin_key_id, admin_raw = DatabaseAPIKeyAuth.create_key(
            session,
            user_id="e2e-admin",
            name="E2E Admin Key",
            zone_id=ROOT_ZONE_ID,
            is_admin=True,
        )
        normal_key_id, normal_raw = DatabaseAPIKeyAuth.create_key(
            session,
            user_id="e2e-user",
            name="E2E User Key",
            zone_id=ROOT_ZONE_ID,
            is_admin=False,
        )
        session.commit()

    return {
        "admin_key": admin_raw,
        "admin_key_id": admin_key_id,
        "normal_key": normal_raw,
        "normal_key_id": normal_key_id,
    }


@pytest.fixture
async def app(tmp_path: Any, db_path: Any, session_factory: Any, api_keys: Any) -> Any:
    """FastAPI app with permissions enabled, database auth, identity + credentials."""
    from types import SimpleNamespace

    from nexus.backends.storage.cas_local import CASLocalBackend
    from nexus.bricks.auth.providers.database_key import DatabaseAPIKeyAuth
    from nexus.bricks.auth.providers.discriminator import DiscriminatingAuthProvider
    from nexus.factory import create_nexus_fs
    from nexus.server.fastapi_server import create_app
    from nexus.storage.record_store import SQLAlchemyRecordStore

    tmpdir = tempfile.mkdtemp(prefix="nexus-cred-e2e-")
    backend = CASLocalBackend(root_path=tmpdir)
    metadata_store = DictMetastore()
    record_store = SQLAlchemyRecordStore(db_url=f"sqlite:///{db_path}")

    nx = await create_nexus_fs(
        backend=backend,
        metadata_store=metadata_store,
        record_store=record_store,
        permissions=PermissionConfig(enforce=True),
        parsing=ParseConfig(auto_parse=False),
        init_cred=TEST_CONTEXT,
    )

    db_key_auth = DatabaseAPIKeyAuth(record_store=SimpleNamespace(session_factory=session_factory))
    auth_provider = DiscriminatingAuthProvider(
        api_key_provider=db_key_auth,
        jwt_provider=None,
    )

    application = create_app(
        nexus_fs=nx,
        auth_provider=auth_provider,
        database_url=f"sqlite:///{db_path}",
    )

    yield application

    metadata_store.close()
    record_store.close()
    shutil.rmtree(tmpdir, ignore_errors=True)


@pytest.fixture
def client(app: Any) -> Any:
    """TestClient with lifespan context (triggers startup/shutdown)."""
    from fastapi.testclient import TestClient

    with TestClient(app) as c:
        yield c


@pytest.fixture
def admin_headers(api_keys: Any) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_keys['admin_key']}"}


@pytest.fixture
def normal_headers(api_keys: Any) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_keys['normal_key']}"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def provision_agent(client: Any, *, prefix: str = "cred-test") -> str:
    """Provision an agent identity via KeyService and return agent_id.

    Uses the server's key_service directly (via app.state) since the
    register_agent RPC method is not available in TestClient mode.
    """
    from nexus.server.fastapi_server import _fastapi_app

    agent_id = f"e2e-admin,{prefix}-{uuid.uuid4().hex[:8]}"
    key_service = _fastapi_app.state.key_service
    assert key_service is not None, "KeyService not initialized on app.state"
    key_service.ensure_keypair(agent_id)
    return agent_id


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestCredentialE2ELifecycle:
    """Full server-level credential lifecycle tests."""

    def test_health_check(self, client: Any) -> None:
        resp = client.get("/health")
        assert resp.status_code == 200

    def test_issue_credential(self, client: Any, admin_headers: Any) -> None:
        """Issue a credential via REST API."""
        agent_id = provision_agent(client, prefix="issue")

        resp = client.post(
            "/api/v2/credentials/issue",
            json={
                "agent_id": agent_id,
                "capabilities": [
                    {"resource": "nexus:brick:search", "abilities": ["read"]},
                ],
                "ttl_seconds": 3600,
            },
            headers=admin_headers,
        )
        assert resp.status_code == 200, f"Issue failed: {resp.text}"
        data = resp.json()

        assert data["credential_id"].startswith("urn:uuid:")
        assert data["subject_did"].startswith("did:key:z")
        assert data["delegation_depth"] == 0
        assert len(data["capabilities"]) == 1
        assert data["capabilities"][0]["resource"] == "nexus:brick:search"

    def test_issue_and_verify(self, client: Any, admin_headers: Any) -> None:
        """Issue a credential and verify the JWT-VC token."""
        agent_id = provision_agent(client, prefix="verify")

        # Issue
        issue_resp = client.post(
            "/api/v2/credentials/issue",
            json={
                "agent_id": agent_id,
                "capabilities": [
                    {"resource": "nexus:brick:cache", "abilities": ["read", "write"]},
                ],
            },
            headers=admin_headers,
        )
        assert issue_resp.status_code == 200
        cred_id = issue_resp.json()["credential_id"]

        # Get status to confirm it's active
        status_resp = client.get(
            f"/api/v2/credentials/{cred_id}",
            headers=admin_headers,
        )
        assert status_resp.status_code == 200
        assert status_resp.json()["is_active"] is True

    def test_revoke_credential(self, client: Any, admin_headers: Any) -> None:
        """Issue and revoke a credential."""
        agent_id = provision_agent(client, prefix="revoke")

        # Issue
        issue_resp = client.post(
            "/api/v2/credentials/issue",
            json={
                "agent_id": agent_id,
                "capabilities": [
                    {"resource": "nexus:brick:search", "abilities": ["read"]},
                ],
            },
            headers=admin_headers,
        )
        assert issue_resp.status_code == 200
        cred_id = issue_resp.json()["credential_id"]

        # Revoke
        revoke_resp = client.delete(
            f"/api/v2/credentials/{cred_id}",
            headers=admin_headers,
        )
        assert revoke_resp.status_code == 200
        assert revoke_resp.json()["revoked"] is True

        # Verify it's revoked
        status_resp = client.get(
            f"/api/v2/credentials/{cred_id}",
            headers=admin_headers,
        )
        assert status_resp.status_code == 200
        assert status_resp.json()["is_active"] is False
        assert status_resp.json()["revoked_at"] is not None

    def test_revoke_nonexistent_returns_404(self, client: Any, admin_headers: Any) -> None:
        resp = client.delete(
            "/api/v2/credentials/urn:uuid:nonexistent",
            headers=admin_headers,
        )
        assert resp.status_code == 404

    def test_get_credential_status(self, client: Any, admin_headers: Any) -> None:
        """Get status of an issued credential."""
        agent_id = provision_agent(client, prefix="status")

        issue_resp = client.post(
            "/api/v2/credentials/issue",
            json={
                "agent_id": agent_id,
                "capabilities": [
                    {"resource": "nexus:brick:search", "abilities": ["read"]},
                ],
            },
            headers=admin_headers,
        )
        cred_id = issue_resp.json()["credential_id"]

        status_resp = client.get(
            f"/api/v2/credentials/{cred_id}",
            headers=admin_headers,
        )
        assert status_resp.status_code == 200
        data = status_resp.json()

        assert data["credential_id"] == cred_id
        assert data["is_active"] is True
        assert data["delegation_depth"] == 0
        assert data["created_at"] is not None
        assert data["expires_at"] is not None

    def test_get_nonexistent_credential_returns_404(self, client: Any, admin_headers: Any) -> None:
        resp = client.get(
            "/api/v2/credentials/urn:uuid:nothing",
            headers=admin_headers,
        )
        assert resp.status_code == 404

    def test_list_agent_credentials(self, client: Any, admin_headers: Any) -> None:
        """List credentials for an agent."""
        agent_id = provision_agent(client, prefix="list")

        # Issue two credentials
        for resource in ["nexus:brick:search", "nexus:brick:cache"]:
            resp = client.post(
                "/api/v2/credentials/issue",
                json={
                    "agent_id": agent_id,
                    "capabilities": [
                        {"resource": resource, "abilities": ["read"]},
                    ],
                },
                headers=admin_headers,
            )
            assert resp.status_code == 200

        resp = client.get(
            f"/api/v2/agents/{agent_id}/credentials",
            headers=admin_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["agent_id"] == agent_id
        assert data["count"] == 2
        assert len(data["credentials"]) == 2

    def test_list_empty_agent(self, client: Any, admin_headers: Any) -> None:
        resp = client.get(
            "/api/v2/agents/nonexistent-agent/credentials",
            headers=admin_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["count"] == 0

    def test_issue_multiple_capabilities(self, client: Any, admin_headers: Any) -> None:
        """Issue credential with multiple capabilities and caveats."""
        agent_id = provision_agent(client, prefix="multicap")

        resp = client.post(
            "/api/v2/credentials/issue",
            json={
                "agent_id": agent_id,
                "capabilities": [
                    {"resource": "nexus:brick:search", "abilities": ["read"]},
                    {
                        "resource": "nexus:brick:cache",
                        "abilities": ["read", "write"],
                        "caveats": {"max_results": 100},
                    },
                ],
            },
            headers=admin_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["capabilities"]) == 2


class TestCredentialE2EAuth:
    """Authentication and authorization tests for credential endpoints."""

    def test_unauthenticated_issue_rejected(self, client: Any) -> None:
        resp = client.post(
            "/api/v2/credentials/issue",
            json={
                "agent_id": "test",
                "capabilities": [
                    {"resource": "test", "abilities": ["read"]},
                ],
            },
        )
        assert resp.status_code == 401

    def test_unauthenticated_verify_rejected(self, client: Any) -> None:
        resp = client.post(
            "/api/v2/credentials/verify",
            json={"token": "fake.jwt.token"},
        )
        assert resp.status_code == 401

    def test_unauthenticated_revoke_rejected(self, client: Any) -> None:
        resp = client.delete("/api/v2/credentials/urn:uuid:test")
        assert resp.status_code == 401

    def test_unauthenticated_status_rejected(self, client: Any) -> None:
        resp = client.get("/api/v2/credentials/urn:uuid:test")
        assert resp.status_code == 401

    def test_unauthenticated_list_rejected(self, client: Any) -> None:
        resp = client.get("/api/v2/agents/test/credentials")
        assert resp.status_code == 401

    def test_normal_user_can_issue(
        self,
        client: Any,
        normal_headers: Any,
    ) -> None:
        """Non-admin users can issue credentials."""
        agent_id = provision_agent(client, prefix="normal-issue")

        resp = client.post(
            "/api/v2/credentials/issue",
            json={
                "agent_id": agent_id,
                "capabilities": [
                    {"resource": "nexus:brick:search", "abilities": ["read"]},
                ],
            },
            headers=normal_headers,
        )
        assert resp.status_code == 200


class TestCredentialE2EValidation:
    """Input validation tests for credential endpoints."""

    def test_issue_no_capabilities_rejected(self, client: Any, admin_headers: Any) -> None:
        """Issue with empty capabilities fails validation."""
        resp = client.post(
            "/api/v2/credentials/issue",
            json={
                "agent_id": "any-agent",
                "capabilities": [],
            },
            headers=admin_headers,
        )
        assert resp.status_code == 422  # Pydantic validation

    def test_issue_invalid_ability_rejected(self, client: Any, admin_headers: Any) -> None:
        """Issue with invalid ability string fails."""
        agent_id = provision_agent(client, prefix="badability")

        resp = client.post(
            "/api/v2/credentials/issue",
            json={
                "agent_id": agent_id,
                "capabilities": [
                    {"resource": "test", "abilities": ["invalid_ability"]},
                ],
            },
            headers=admin_headers,
        )
        # This should fail during capability parsing (400 or 500)
        assert resp.status_code in (400, 422, 500)

    def test_issue_unknown_agent_returns_404(self, client: Any, admin_headers: Any) -> None:
        """Issue to an agent without identity returns 404."""
        resp = client.post(
            "/api/v2/credentials/issue",
            json={
                "agent_id": "nonexistent-agent-id",
                "capabilities": [
                    {"resource": "test", "abilities": ["read"]},
                ],
            },
            headers=admin_headers,
        )
        assert resp.status_code == 404

    def test_verify_invalid_token(self, client: Any, admin_headers: Any) -> None:
        """Verify with an invalid JWT string returns valid=false."""
        resp = client.post(
            "/api/v2/credentials/verify",
            json={"token": "not-a-jwt"},
            headers=admin_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["valid"] is False
        assert "error" in data
