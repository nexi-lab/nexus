"""E2E tests for POST /api/v2/agents/register (Issue #3130).

Tests the full HTTP → AgentRegistrationService → EntityRegistry + AgentRegistry
+ ReBAC + IPC path using real in-memory SQLite, real IPC provisioner with
InMemoryStorageDriver, and real service wiring. No mocks for core services.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from nexus.bricks.ipc.provisioning import AgentProvisioner
from nexus.bricks.rebac.entity_registry import EntityRegistry
from nexus.bricks.rebac.manager import EnhancedReBACManager
from nexus.core.process_table import AgentRegistry
from nexus.server.api.v2.routers.agent_registration import (
    router as registration_router,
)
from nexus.server.api.v2.routers.ipc import (
    _get_ipc_provisioner,
    _get_ipc_storage,
    _get_require_auth,
    _get_zone_id,
)
from nexus.server.api.v2.routers.ipc import (
    router as ipc_router,
)
from nexus.system_services.agents.agent_registration import AgentRegistrationService
from tests.helpers.in_memory_record_store import InMemoryRecordStore
from tests.unit.bricks.ipc.fakes import InMemoryStorageDriver

ZONE = "root"

_REQUIRE_AUTH = _get_require_auth()


@pytest.fixture()
def record_store():
    store = InMemoryRecordStore()
    yield store
    store.close()


@pytest.fixture()
def entity_registry(record_store):
    return EntityRegistry(record_store)


@pytest.fixture()
def process_table():
    return AgentRegistry()


@pytest.fixture()
def rebac_manager(record_store):
    manager = EnhancedReBACManager(engine=record_store.engine, cache_ttl_seconds=0, max_depth=10)
    yield manager
    manager.close()


@pytest.fixture()
def ipc_storage():
    return InMemoryStorageDriver()


@pytest.fixture()
def ipc_provisioner(ipc_storage):
    return AgentProvisioner(storage=ipc_storage, zone_id=ZONE)


@pytest.fixture()
def registration_service(
    record_store, entity_registry, process_table, rebac_manager, ipc_provisioner
):
    return AgentRegistrationService(
        record_store=record_store,
        entity_registry=entity_registry,
        process_table=process_table,
        rebac_manager=rebac_manager,
        ipc_provisioner=ipc_provisioner,
    )


def _create_test_app(
    registration_service, auth_result, ipc_storage=None, ipc_provisioner_inst=None
):
    app = FastAPI()
    app.state.record_store = None
    app.state._agent_registration_service = registration_service

    from nexus.server.dependencies import require_admin

    async def mock_admin():
        return auth_result

    app.dependency_overrides[require_admin] = mock_admin
    app.include_router(registration_router)

    if ipc_storage is not None and ipc_provisioner_inst is not None:
        app.dependency_overrides[_REQUIRE_AUTH] = lambda: auth_result
        app.dependency_overrides[_get_ipc_storage] = lambda: ipc_storage
        app.dependency_overrides[_get_ipc_provisioner] = lambda: ipc_provisioner_inst
        app.dependency_overrides[_get_zone_id] = lambda: ZONE
        app.include_router(ipc_router)

    return app


@pytest.fixture()
def admin_auth():
    return {
        "authenticated": True,
        "is_admin": True,
        "subject_type": "user",
        "subject_id": "admin-user",
        "user_id": "admin-user",
        "zone_id": ZONE,
    }


@pytest.fixture()
def client(registration_service, admin_auth, entity_registry, ipc_storage, ipc_provisioner):
    entity_registry.register_entity("user", "admin-user")
    app = _create_test_app(
        registration_service,
        admin_auth,
        ipc_storage=ipc_storage,
        ipc_provisioner_inst=ipc_provisioner,
    )
    return TestClient(app)


class TestRegisterEndpoint:
    def test_register_basic_agent(self, client, entity_registry):
        response = client.post(
            "/api/v2/agents/register", json={"agent_id": "basic-agent", "name": "Basic Agent"}
        )
        assert response.status_code == 201
        data = response.json()
        assert data["agent_id"] == "basic-agent"
        assert data["api_key"].startswith("sk-")
        assert data["ipc_provisioned"] is True
        assert entity_registry.get_entity("agent", "basic-agent") is not None

    def test_register_with_grants(self, client):
        response = client.post(
            "/api/v2/agents/register",
            json={
                "agent_id": "grant-agent",
                "name": "Grant Agent",
                "grants": [
                    {"path": "/workspace/main.py", "role": "editor"},
                    {"path": "/docs/readme.md", "role": "viewer"},
                ],
            },
        )
        assert response.status_code == 201
        assert len(response.json()["grants"]) == 2

    def test_register_without_ipc(self, client):
        response = client.post(
            "/api/v2/agents/register",
            json={"agent_id": "no-ipc-agent", "name": "No IPC", "ipc": False},
        )
        assert response.status_code == 201
        assert response.json()["ipc_provisioned"] is False


class TestIPCIntegration:
    def test_register_creates_ipc_directories(self, client, ipc_storage):
        response = client.post(
            "/api/v2/agents/register", json={"agent_id": "ipc-test-agent", "name": "IPC Test Agent"}
        )
        assert response.status_code == 201
        assert response.json()["ipc_provisioned"] is True
        assert ("/agents/ipc-test-agent/inbox", ZONE) in ipc_storage._dirs
        assert ("/agents/ipc-test-agent/outbox", ZONE) in ipc_storage._dirs
        assert ("/agents/ipc-test-agent/processed", ZONE) in ipc_storage._dirs
        assert ("/agents/ipc-test-agent/dead_letter", ZONE) in ipc_storage._dirs

    def test_register_creates_agent_card(self, client, ipc_storage):
        import json

        client.post(
            "/api/v2/agents/register", json={"agent_id": "card-agent", "name": "Card Agent"}
        )
        card = json.loads(ipc_storage._files[("/agents/card-agent/AGENT.json", ZONE)])
        assert card["agent_id"] == "card-agent"
        assert card["name"] == "Card Agent"
        assert card["status"] == "connected"

    def test_registered_agent_can_receive_ipc_messages(self, client):
        client.post("/api/v2/agents/register", json={"agent_id": "sender-a", "name": "Sender"})
        client.post("/api/v2/agents/register", json={"agent_id": "receiver-a", "name": "Receiver"})
        send_resp = client.post(
            "/api/v2/ipc/send",
            json={"sender": "sender-a", "recipient": "receiver-a", "type": "task", "payload": {}},
        )
        assert send_resp.status_code == 200
        assert client.get("/api/v2/ipc/inbox/receiver-a").json()["total"] == 1

    def test_full_lifecycle_register_send_receive(self, client):
        for aid, name in [("lc-a", "Agent A"), ("lc-b", "Agent B")]:
            assert (
                client.post(
                    "/api/v2/agents/register", json={"agent_id": aid, "name": name}
                ).status_code
                == 201
            )
        for i in range(3):
            assert (
                client.post(
                    "/api/v2/ipc/send",
                    json={
                        "sender": "lc-a",
                        "recipient": "lc-b",
                        "type": "task",
                        "payload": {"i": i},
                    },
                ).status_code
                == 200
            )
        assert client.get("/api/v2/ipc/inbox/lc-b").json()["total"] == 3
        assert client.get("/api/v2/ipc/inbox/lc-b/count").json()["count"] == 3

    def test_unregistered_agent_inbox_returns_404(self, client):
        assert client.get("/api/v2/ipc/inbox/ghost-agent").status_code == 404


class TestErrorHandling:
    def test_duplicate_agent_returns_409(self, client):
        assert (
            client.post(
                "/api/v2/agents/register", json={"agent_id": "dup-agent", "name": "First"}
            ).status_code
            == 201
        )
        response2 = client.post(
            "/api/v2/agents/register", json={"agent_id": "dup-agent", "name": "Second"}
        )
        assert response2.status_code == 409
        assert "already exists" in response2.json()["detail"]

    def test_invalid_role_returns_400(self, client):
        response = client.post(
            "/api/v2/agents/register",
            json={
                "agent_id": "bad-role",
                "name": "Bad",
                "grants": [{"path": "/workspace/main.py", "role": "deny"}],
            },
        )
        assert response.status_code == 400
        assert "Invalid role" in response.json()["detail"]

    def test_path_traversal_returns_400(self, client):
        response = client.post(
            "/api/v2/agents/register",
            json={
                "agent_id": "trav",
                "name": "T",
                "grants": [{"path": "/workspace/../etc/passwd", "role": "editor"}],
            },
        )
        assert response.status_code == 400
        assert "traversal" in response.json()["detail"].lower()

    def test_invalid_public_key_hex_returns_400(self, client):
        response = client.post(
            "/api/v2/agents/register",
            json={"agent_id": "bad-key", "name": "BK", "public_key": "not-hex"},
        )
        assert response.status_code == 400
        assert "hex" in response.json()["detail"].lower()

    def test_wrong_length_public_key_returns_400(self, client):
        response = client.post(
            "/api/v2/agents/register",
            json={"agent_id": "short-key", "name": "SK", "public_key": "abcd1234"},
        )
        assert response.status_code == 400
        assert "32 bytes" in response.json()["detail"]

    def test_empty_agent_id_returns_422(self, client):
        assert (
            client.post(
                "/api/v2/agents/register", json={"agent_id": "", "name": "Empty"}
            ).status_code
            == 422
        )


class TestAdminEnforcement:
    def test_non_admin_returns_403(self, registration_service):
        non_admin_auth = {
            "authenticated": True,
            "is_admin": False,
            "subject_type": "user",
            "subject_id": "regular-user",
            "user_id": "regular-user",
            "zone_id": ZONE,
        }
        app = _create_test_app(registration_service, non_admin_auth)
        from nexus.server.dependencies import require_admin

        async def strict_admin():
            from fastapi import HTTPException

            raise HTTPException(status_code=403, detail="Admin privileges required")

        app.dependency_overrides[require_admin] = strict_admin
        assert (
            TestClient(app)
            .post("/api/v2/agents/register", json={"agent_id": "blocked", "name": "B"})
            .status_code
            == 403
        )
