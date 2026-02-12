"""E2E test: Agent identity with FastAPI server, permissions enabled, database auth.

Tests the full server-level flow:
1. Create FastAPI app with NexusFS (enforce_permissions=True, database auth)
2. Register agent via RPC → verify identity auto-provisioned
3. GET /api/agents/{id}/identity → returns DID, public key
4. POST /api/agents/{id}/verify → verifies signature round-trip
5. Unauthenticated requests → 401

Run: pytest tests/e2e/test_identity_e2e.py -v
"""

from __future__ import annotations

import base64
import shutil
import tempfile
import uuid
from collections.abc import Sequence
from typing import Any

import pytest

from nexus.core._metadata_generated import FileMetadata, FileMetadataProtocol, PaginatedResult
from nexus.storage.models import Base

# ---------------------------------------------------------------------------
# In-memory metadata store (same pattern as test_memory_paging_postgres.py)
# ---------------------------------------------------------------------------


class InMemoryMetadataStore(FileMetadataProtocol):
    def __init__(self) -> None:
        self._store: dict[str, FileMetadata] = {}

    def get(self, path: str) -> FileMetadata | None:
        return self._store.get(path)

    def put(self, metadata: FileMetadata) -> None:
        self._store[metadata.path] = metadata

    def delete(self, path: str) -> dict[str, Any] | None:
        removed = self._store.pop(path, None)
        return {"path": path} if removed else None

    def exists(self, path: str) -> bool:
        return path in self._store

    def list(
        self, prefix: str = "", recursive: bool = True, **kwargs: Any
    ) -> list[FileMetadata]:
        return [m for p, m in self._store.items() if p.startswith(prefix)]

    def list_paginated(
        self,
        prefix: str = "",
        recursive: bool = True,
        limit: int = 1000,
        cursor: str | None = None,
        zone_id: str | None = None,
    ) -> PaginatedResult:
        items = self.list(prefix, recursive)
        return PaginatedResult(
            items=items[:limit],
            next_cursor=None,
            has_more=len(items) > limit,
            total_count=len(items),
        )

    def get_batch(self, paths: Sequence[str]) -> dict[str, FileMetadata | None]:
        return {p: self._store.get(p) for p in paths}

    def is_implicit_directory(self, path: str) -> bool:
        """Check if path is an implicit dir (children exist without explicit dir metadata)."""
        prefix = path.rstrip("/") + "/"
        return any(p.startswith(prefix) for p in self._store)

    def close(self) -> None:
        self._store.clear()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _set_env(monkeypatch: Any) -> None:
    """Isolate from production env vars."""
    monkeypatch.setenv("NEXUS_JWT_SECRET", "test-secret-key-identity-e2e")
    monkeypatch.delenv("NEXUS_DATABASE_URL", raising=False)
    monkeypatch.delenv("POSTGRES_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)


@pytest.fixture
def db_path(tmp_path: Any) -> Any:
    """SQLite database for the test."""
    return tmp_path / f"identity_e2e_{uuid.uuid4().hex[:8]}.db"


@pytest.fixture
def session_factory(db_path: Any) -> Any:
    """Session factory for database auth key setup."""
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
    """Create admin + normal API keys for the test."""
    from nexus.server.auth.database_key import DatabaseAPIKeyAuth

    with session_factory() as session:
        admin_key_id, admin_raw = DatabaseAPIKeyAuth.create_key(
            session,
            user_id="e2e-admin",
            name="E2E Admin Key",
            zone_id="default",
            is_admin=True,
        )
        normal_key_id, normal_raw = DatabaseAPIKeyAuth.create_key(
            session,
            user_id="e2e-user",
            name="E2E User Key",
            zone_id="default",
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
def app(tmp_path: Any, db_path: Any, session_factory: Any, api_keys: Any) -> Any:
    """FastAPI app with permissions enabled, database auth, identity layer.

    Uses RaftMetadataStore.embedded() for realistic filesystem operations
    (mkdir, write) that the identity layer needs for DID document writing.
    """
    from nexus.backends.local import LocalBackend
    from nexus.core.nexus_fs import NexusFS
    from nexus.server.auth.database_key import DatabaseAPIKeyAuth
    from nexus.server.auth.factory import DiscriminatingAuthProvider
    from nexus.server.fastapi_server import create_app
    from nexus.storage.record_store import SQLAlchemyRecordStore

    tmpdir = tempfile.mkdtemp(prefix="nexus-identity-e2e-")
    backend = LocalBackend(root_path=tmpdir)
    metadata_store = InMemoryMetadataStore()
    record_store = SQLAlchemyRecordStore(db_url=f"sqlite:///{db_path}")

    nx = NexusFS(
        backend=backend,
        metadata_store=metadata_store,
        record_store=record_store,
        enforce_permissions=True,
        auto_parse=False,
    )

    # Wire database auth
    db_key_auth = DatabaseAPIKeyAuth(session_factory=session_factory)
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


def rpc_call(client: Any, method: str, params: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
    """Make an RPC call and return the result."""
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
    assert resp.status_code == 200, f"RPC {method} failed: {resp.text}"
    body = resp.json()
    assert "error" not in body or body.get("error") is None, (
        f"RPC {method} error: {body.get('error')}"
    )
    return dict(body.get("result", {}))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestIdentityE2EServerLevel:
    """Full server-level identity tests with permissions and auth."""

    def test_health_check(self, client: Any, admin_headers: Any) -> None:
        """Server is up and running."""
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] in ("ok", "healthy")

    def test_register_agent_provisions_identity(self, client: Any, admin_headers: Any) -> None:
        """register_agent auto-provisions Ed25519 keypair + DID."""
        agent_id = f"e2e-admin,test-agent-{uuid.uuid4().hex[:8]}"
        result = rpc_call(
            client,
            "register_agent",
            {
                "agent_id": agent_id,
                "name": "E2E Test Agent",
                "description": "Agent for identity e2e test",
                "context": {"user_id": "e2e-admin", "zone_id": "default"},
            },
            admin_headers,
        )

        # Agent should have DID and key_id provisioned
        assert "did" in result, f"No 'did' in register_agent result: {result}"
        assert result["did"].startswith("did:key:z")
        assert "key_id" in result

    def test_get_agent_identity_endpoint(self, client: Any, admin_headers: Any) -> None:
        """GET /api/agents/{id}/identity returns DID and public key."""
        agent_id = f"e2e-admin,identity-agent-{uuid.uuid4().hex[:8]}"
        reg_result = rpc_call(
            client,
            "register_agent",
            {
                "agent_id": agent_id,
                "name": "Identity Query Agent",
                "context": {"user_id": "e2e-admin", "zone_id": "default"},
            },
            admin_headers,
        )

        # Query identity endpoint
        resp = client.get(f"/api/agents/{agent_id}/identity", headers=admin_headers)
        assert resp.status_code == 200, f"Identity query failed: {resp.text}"
        data = resp.json()

        assert data["agent_id"] == agent_id
        assert data["did"] == reg_result["did"]
        assert data["key_id"] == reg_result["key_id"]
        assert data["algorithm"] == "Ed25519"
        assert len(data["public_key_hex"]) == 64  # 32 bytes hex = 64 chars

    def test_verify_signature_round_trip(self, client: Any, admin_headers: Any, app: Any) -> None:
        """POST /api/agents/{id}/verify correctly validates a signature."""
        from nexus.server.fastapi_server import _app_state

        agent_id = f"e2e-admin,verify-agent-{uuid.uuid4().hex[:8]}"
        rpc_call(
            client,
            "register_agent",
            {
                "agent_id": agent_id,
                "name": "Verify Agent",
                "context": {"user_id": "e2e-admin", "zone_id": "default"},
            },
            admin_headers,
        )

        # Sign a message using KeyService directly (simulates agent-side signing)
        key_service = _app_state.key_service
        assert key_service is not None, "KeyService not initialized"

        keys = key_service.get_active_keys(agent_id)
        assert len(keys) > 0, f"No active keys for {agent_id}"

        private_key = key_service.decrypt_private_key(keys[0].key_id)
        message = b"Hello from e2e test"
        signature = key_service._crypto.sign(message, private_key)

        # Verify via REST endpoint
        resp = client.post(
            f"/api/agents/{agent_id}/verify",
            json={
                "message": base64.b64encode(message).decode(),
                "signature": base64.b64encode(signature).decode(),
            },
            headers=admin_headers,
        )
        assert resp.status_code == 200, f"Verify failed: {resp.text}"
        data = resp.json()
        assert data["valid"] is True
        assert data["agent_id"] == agent_id

    def test_verify_tampered_signature_fails(self, client: Any, admin_headers: Any, app: Any) -> None:
        """Tampered signature is rejected."""
        from nexus.server.fastapi_server import _app_state

        agent_id = f"e2e-admin,tamper-agent-{uuid.uuid4().hex[:8]}"
        rpc_call(
            client,
            "register_agent",
            {
                "agent_id": agent_id,
                "name": "Tamper Agent",
                "context": {"user_id": "e2e-admin", "zone_id": "default"},
            },
            admin_headers,
        )

        key_service = _app_state.key_service
        keys = key_service.get_active_keys(agent_id)
        private_key = key_service.decrypt_private_key(keys[0].key_id)
        message = b"Original message"
        signature = key_service._crypto.sign(message, private_key)

        # Tamper with the message
        resp = client.post(
            f"/api/agents/{agent_id}/verify",
            json={
                "message": base64.b64encode(b"Tampered message").decode(),
                "signature": base64.b64encode(signature).decode(),
            },
            headers=admin_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["valid"] is False

    def test_cross_agent_key_id_rejected(self, client: Any, admin_headers: Any, app: Any) -> None:
        """Using agent_a's key_id for agent_b's verify endpoint returns 403."""
        from nexus.server.fastapi_server import _app_state

        agent_a = f"e2e-admin,cross-a-{uuid.uuid4().hex[:8]}"
        agent_b = f"e2e-admin,cross-b-{uuid.uuid4().hex[:8]}"

        rpc_call(client, "register_agent", {
            "agent_id": agent_a, "name": "Agent A",
            "context": {"user_id": "e2e-admin", "zone_id": "default"},
        }, admin_headers)
        rpc_call(client, "register_agent", {
            "agent_id": agent_b, "name": "Agent B",
            "context": {"user_id": "e2e-admin", "zone_id": "default"},
        }, admin_headers)

        key_service = _app_state.key_service
        keys_a = key_service.get_active_keys(agent_a)

        # Try to verify agent_b's endpoint with agent_a's key_id
        resp = client.post(
            f"/api/agents/{agent_b}/verify",
            json={
                "message": base64.b64encode(b"test").decode(),
                "signature": base64.b64encode(b"\x00" * 64).decode(),
                "key_id": keys_a[0].key_id,
            },
            headers=admin_headers,
        )
        assert resp.status_code == 403, f"Expected 403, got {resp.status_code}: {resp.text}"

    def test_unauthenticated_identity_request_rejected(self, client: Any) -> None:
        """Identity endpoint rejects unauthenticated requests."""
        resp = client.get("/api/agents/some-agent/identity")
        assert resp.status_code == 401

    def test_unauthenticated_verify_request_rejected(self, client: Any) -> None:
        """Verify endpoint rejects unauthenticated requests."""
        resp = client.post(
            "/api/agents/some-agent/verify",
            json={"message": "dGVzdA==", "signature": "AAAA"},
        )
        assert resp.status_code == 401

    def test_identity_for_unknown_agent_returns_404(self, client: Any, admin_headers: Any) -> None:
        """Querying identity for non-existent agent returns 404."""
        resp = client.get(
            "/api/agents/nonexistent-agent/identity",
            headers=admin_headers,
        )
        assert resp.status_code == 404

    def test_normal_user_can_query_identity(self, client: Any, normal_headers: Any, admin_headers: Any) -> None:
        """Non-admin authenticated users can query agent identity."""
        agent_id = f"e2e-admin,normal-test-{uuid.uuid4().hex[:8]}"
        rpc_call(
            client,
            "register_agent",
            {
                "agent_id": agent_id,
                "name": "Normal Access Agent",
                "context": {"user_id": "e2e-admin", "zone_id": "default"},
            },
            admin_headers,
        )

        # Normal user can query identity
        resp = client.get(f"/api/agents/{agent_id}/identity", headers=normal_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["did"].startswith("did:key:z")


class TestNormalUserIdentityFlow:
    """Full identity flow for a normal (non-admin) user with permissions enabled."""

    def test_normal_user_registers_own_agent(self, client: Any, normal_headers: Any) -> None:
        """Normal user registers an agent under their own namespace and gets identity."""
        agent_id = f"e2e-user,normal-agent-{uuid.uuid4().hex[:8]}"
        result = rpc_call(
            client,
            "register_agent",
            {
                "agent_id": agent_id,
                "name": "Normal User Agent",
                "description": "Agent owned by normal user",
            },
            normal_headers,
        )

        # Identity should be provisioned even for normal users
        assert "did" in result, f"No 'did' in result: {result}"
        assert result["did"].startswith("did:key:z")
        assert "key_id" in result

        # Verify via identity endpoint
        resp = client.get(f"/api/agents/{agent_id}/identity", headers=normal_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["did"] == result["did"]
        assert data["key_id"] == result["key_id"]
        assert data["algorithm"] == "Ed25519"

    def test_normal_user_sign_and_verify(self, client: Any, normal_headers: Any) -> None:
        """Normal user registers agent, signs message, verifies via REST endpoint."""
        from nexus.server.fastapi_server import _app_state

        agent_id = f"e2e-user,signverify-{uuid.uuid4().hex[:8]}"
        rpc_call(
            client,
            "register_agent",
            {"agent_id": agent_id, "name": "SignVerify Agent"},
            normal_headers,
        )

        # Sign with the agent's key
        key_service = _app_state.key_service
        assert key_service is not None

        keys = key_service.get_active_keys(agent_id)
        assert len(keys) > 0
        private_key = key_service.decrypt_private_key(keys[0].key_id)
        message = b"Normal user signed this message"
        signature = key_service._crypto.sign(message, private_key)

        # Verify via endpoint (normal user auth)
        resp = client.post(
            f"/api/agents/{agent_id}/verify",
            json={
                "message": base64.b64encode(message).decode(),
                "signature": base64.b64encode(signature).decode(),
            },
            headers=normal_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["valid"] is True
        assert data["agent_id"] == agent_id

    def test_normal_user_tampered_signature_rejected(self, client: Any, normal_headers: Any) -> None:
        """Normal user's tampered signature is rejected."""
        from nexus.server.fastapi_server import _app_state

        agent_id = f"e2e-user,tamper-{uuid.uuid4().hex[:8]}"
        rpc_call(
            client,
            "register_agent",
            {"agent_id": agent_id, "name": "Tamper Test Agent"},
            normal_headers,
        )

        key_service = _app_state.key_service
        keys = key_service.get_active_keys(agent_id)
        private_key = key_service.decrypt_private_key(keys[0].key_id)
        signature = key_service._crypto.sign(b"original", private_key)

        # Verify with different message → should fail
        resp = client.post(
            f"/api/agents/{agent_id}/verify",
            json={
                "message": base64.b64encode(b"tampered").decode(),
                "signature": base64.b64encode(signature).decode(),
            },
            headers=normal_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["valid"] is False

    def test_normal_user_cannot_use_other_agents_key(self, client: Any, normal_headers: Any, admin_headers: Any) -> None:
        """Normal user cannot verify with another agent's key_id."""
        from nexus.server.fastapi_server import _app_state

        # Admin creates an agent
        admin_agent = f"e2e-admin,admin-agent-{uuid.uuid4().hex[:8]}"
        rpc_call(
            client, "register_agent",
            {"agent_id": admin_agent, "name": "Admin Agent"},
            admin_headers,
        )
        admin_keys = _app_state.key_service.get_active_keys(admin_agent)

        # Normal user creates their own agent
        user_agent = f"e2e-user,user-agent-{uuid.uuid4().hex[:8]}"
        rpc_call(
            client, "register_agent",
            {"agent_id": user_agent, "name": "User Agent"},
            normal_headers,
        )

        # Normal user tries to verify user_agent endpoint with admin_agent's key_id
        resp = client.post(
            f"/api/agents/{user_agent}/verify",
            json={
                "message": base64.b64encode(b"test").decode(),
                "signature": base64.b64encode(b"\x00" * 64).decode(),
                "key_id": admin_keys[0].key_id,
            },
            headers=normal_headers,
        )
        assert resp.status_code == 403, f"Expected 403, got {resp.status_code}: {resp.text}"

    def test_normal_user_did_resolves_correctly(self, client: Any, normal_headers: Any) -> None:
        """Normal user's agent DID resolves to the correct public key."""
        from nexus.identity.crypto import IdentityCrypto
        from nexus.identity.did import resolve_did_key

        agent_id = f"e2e-user,didresolve-{uuid.uuid4().hex[:8]}"

        result = rpc_call(
            client, "register_agent",
            {"agent_id": agent_id, "name": "DID Resolve Agent"},
            normal_headers,
        )

        did = result.get("did")
        assert did is not None
        assert did.startswith("did:key:z")

        # Resolve the DID to a public key and verify it matches
        resolved_pk = resolve_did_key(did)
        resolved_bytes = IdentityCrypto.public_key_to_bytes(resolved_pk)

        # Verify via identity endpoint
        resp = client.get(f"/api/agents/{agent_id}/identity", headers=normal_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["did"] == did
        assert bytes.fromhex(data["public_key_hex"]) == resolved_bytes
