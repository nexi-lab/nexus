"""E2E integration tests for IPC REST API (Issue #1727, LEGO §8).

Tests the full stack: FastAPI app → IPC router → InMemoryStorageDriver
with mock auth. Validates:
- Full message flow: provision → send → list inbox → count inbox
- Authorization: admin vs non-admin access control
- Input validation: path traversal rejection, invalid agent IDs
- Performance: endpoint response times under budget
- Error handling: missing inbox, invalid message type, self-send
"""

import time
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from nexus.ipc.provisioning import AgentProvisioner
from nexus.server.api.v2.routers.ipc import (
    _get_ipc_provisioner,
    _get_ipc_storage,
    _get_require_auth,
    _get_zone_id,
    router,
)
from tests.unit.ipc.fakes import InMemoryStorageDriver

ZONE = "test-zone"

# Resolve the require_auth dependency once at module load.
# _get_require_auth() lazily imports from fastapi_server and returns the
# same function object thanks to Python module caching.  We store it here
# so the dependency-override key matches the key FastAPI recorded when
# the router decorators were evaluated.
_REQUIRE_AUTH = _get_require_auth()

# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------


def _admin_auth() -> dict[str, Any]:
    """Auth result for an admin user."""
    return {"is_admin": True, "subject_id": "admin"}


def _agent_auth(agent_id: str) -> dict[str, Any]:
    """Auth result for a non-admin agent."""
    return {"is_admin": False, "subject_id": agent_id, "x_agent_id": agent_id}


# ---------------------------------------------------------------------------
# App factory & provision helper
# ---------------------------------------------------------------------------


def _make_client(
    storage: InMemoryStorageDriver,
    provisioner: AgentProvisioner,
    auth_result: dict[str, Any],
) -> TestClient:
    """Create a test FastAPI app with the IPC router and dependency overrides."""
    app = FastAPI()
    app.include_router(router)

    app.dependency_overrides[_REQUIRE_AUTH] = lambda: auth_result
    app.dependency_overrides[_get_ipc_storage] = lambda: storage
    app.dependency_overrides[_get_ipc_provisioner] = lambda: provisioner
    app.dependency_overrides[_get_zone_id] = lambda: ZONE

    return TestClient(app)


def _provision(client: TestClient, agent_id: str) -> None:
    """Provision an agent and assert success."""
    resp = client.post(f"/api/v2/ipc/provision/{agent_id}")
    assert resp.status_code == 200, f"Failed to provision {agent_id}: {resp.json()}"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def storage() -> InMemoryStorageDriver:
    return InMemoryStorageDriver()


@pytest.fixture
def provisioner(storage: InMemoryStorageDriver) -> AgentProvisioner:
    return AgentProvisioner(storage=storage, zone_id=ZONE)


# ---------------------------------------------------------------------------
# E2E: Full message flow (provision → send → list → count)
# ---------------------------------------------------------------------------


class TestIPCE2EFullFlow:
    """End-to-end IPC message flow via REST API."""

    @pytest.mark.asyncio
    async def test_full_flow_provision_send_list_count(
        self,
        storage: InMemoryStorageDriver,
        provisioner: AgentProvisioner,
    ) -> None:
        """Full flow: provision both agents → send message → list inbox → count."""
        client = _make_client(storage, provisioner, _admin_auth())

        # 1. Provision sender and recipient
        _provision(client, "agent:analyst")
        _provision(client, "agent:reviewer")

        # 2. Send a message
        resp = client.post(
            "/api/v2/ipc/send",
            json={
                "sender": "agent:analyst",
                "recipient": "agent:reviewer",
                "type": "task",
                "payload": {"action": "review_document", "doc_id": "doc_42"},
                "ttl_seconds": 3600,
                "correlation_id": "task_42",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "sent"
        assert data["message_id"].startswith("msg_")

        # 3. List inbox — should show 1 message
        resp = client.get("/api/v2/ipc/inbox/agent:reviewer")
        assert resp.status_code == 200
        data = resp.json()
        assert data["agent_id"] == "agent:reviewer"
        assert data["total"] == 1
        assert len(data["messages"]) == 1
        assert data["messages"][0]["filename"].endswith(".json")

        # 4. Count inbox — should be 1
        resp = client.get("/api/v2/ipc/inbox/agent:reviewer/count")
        assert resp.status_code == 200
        assert resp.json()["count"] == 1

    @pytest.mark.asyncio
    async def test_multiple_messages_ordering(
        self,
        storage: InMemoryStorageDriver,
        provisioner: AgentProvisioner,
    ) -> None:
        """Multiple messages appear in inbox and count is accurate."""
        client = _make_client(storage, provisioner, _admin_auth())
        _provision(client, "agent:sender")
        _provision(client, "agent:receiver")

        # Send 3 messages
        for i in range(3):
            resp = client.post(
                "/api/v2/ipc/send",
                json={
                    "sender": "agent:sender",
                    "recipient": "agent:receiver",
                    "type": "event",
                    "payload": {"index": i},
                },
            )
            assert resp.status_code == 200

        # Verify count
        resp = client.get("/api/v2/ipc/inbox/agent:receiver/count")
        assert resp.json()["count"] == 3

        # Verify list
        resp = client.get("/api/v2/ipc/inbox/agent:receiver")
        assert resp.json()["total"] == 3

    @pytest.mark.asyncio
    async def test_send_creates_outbox_copy(
        self,
        storage: InMemoryStorageDriver,
        provisioner: AgentProvisioner,
    ) -> None:
        """Sending a message also writes an outbox copy for the sender."""
        client = _make_client(storage, provisioner, _admin_auth())
        _provision(client, "agent:alice")
        _provision(client, "agent:bob")

        resp = client.post(
            "/api/v2/ipc/send",
            json={
                "sender": "agent:alice",
                "recipient": "agent:bob",
                "type": "task",
                "payload": {"greet": "hello"},
            },
        )
        assert resp.status_code == 200

        # Verify outbox has a copy via storage directly
        outbox_entries = await storage.list_dir("/agents/agent:alice/outbox", ZONE)
        assert len(outbox_entries) == 1
        assert outbox_entries[0].endswith(".json")


# ---------------------------------------------------------------------------
# E2E: Authorization
# ---------------------------------------------------------------------------


class TestIPCE2EAuthorization:
    """Test authorization controls on IPC endpoints."""

    def test_non_admin_cannot_provision(
        self,
        storage: InMemoryStorageDriver,
        provisioner: AgentProvisioner,
    ) -> None:
        """Non-admin users cannot provision agents."""
        client = _make_client(storage, provisioner, _agent_auth("agent:analyst"))

        resp = client.post("/api/v2/ipc/provision/agent:analyst")
        assert resp.status_code == 403
        assert "admin" in resp.json()["detail"].lower()

    def test_non_admin_cannot_access_other_inbox(
        self,
        storage: InMemoryStorageDriver,
        provisioner: AgentProvisioner,
    ) -> None:
        """Non-admin agent cannot list another agent's inbox."""
        # Provision as admin first
        admin_client = _make_client(storage, provisioner, _admin_auth())
        _provision(admin_client, "agent:analyst")
        _provision(admin_client, "agent:reviewer")

        # Try to access reviewer's inbox as analyst
        agent_client = _make_client(storage, provisioner, _agent_auth("agent:analyst"))
        resp = agent_client.get("/api/v2/ipc/inbox/agent:reviewer")
        assert resp.status_code == 403
        assert "Access denied" in resp.json()["detail"]

    def test_non_admin_cannot_count_other_inbox(
        self,
        storage: InMemoryStorageDriver,
        provisioner: AgentProvisioner,
    ) -> None:
        """Non-admin agent cannot count another agent's inbox."""
        admin_client = _make_client(storage, provisioner, _admin_auth())
        _provision(admin_client, "agent:analyst")
        _provision(admin_client, "agent:reviewer")

        agent_client = _make_client(storage, provisioner, _agent_auth("agent:analyst"))
        resp = agent_client.get("/api/v2/ipc/inbox/agent:reviewer/count")
        assert resp.status_code == 403

    def test_non_admin_can_access_own_inbox(
        self,
        storage: InMemoryStorageDriver,
        provisioner: AgentProvisioner,
    ) -> None:
        """Non-admin agent CAN access their own inbox."""
        admin_client = _make_client(storage, provisioner, _admin_auth())
        _provision(admin_client, "agent:analyst")

        agent_client = _make_client(storage, provisioner, _agent_auth("agent:analyst"))
        resp = agent_client.get("/api/v2/ipc/inbox/agent:analyst")
        assert resp.status_code == 200
        assert resp.json()["agent_id"] == "agent:analyst"

    def test_non_admin_cannot_send_as_other_agent(
        self,
        storage: InMemoryStorageDriver,
        provisioner: AgentProvisioner,
    ) -> None:
        """Non-admin agent cannot send messages as another agent."""
        admin_client = _make_client(storage, provisioner, _admin_auth())
        _provision(admin_client, "agent:analyst")
        _provision(admin_client, "agent:reviewer")

        agent_client = _make_client(storage, provisioner, _agent_auth("agent:analyst"))
        resp = agent_client.post(
            "/api/v2/ipc/send",
            json={
                "sender": "agent:reviewer",  # Impersonation attempt
                "recipient": "agent:analyst",
                "type": "task",
                "payload": {},
            },
        )
        assert resp.status_code == 403

    def test_non_admin_can_send_as_self(
        self,
        storage: InMemoryStorageDriver,
        provisioner: AgentProvisioner,
    ) -> None:
        """Non-admin agent CAN send messages as themselves."""
        admin_client = _make_client(storage, provisioner, _admin_auth())
        _provision(admin_client, "agent:analyst")
        _provision(admin_client, "agent:reviewer")

        agent_client = _make_client(storage, provisioner, _agent_auth("agent:analyst"))
        resp = agent_client.post(
            "/api/v2/ipc/send",
            json={
                "sender": "agent:analyst",
                "recipient": "agent:reviewer",
                "type": "task",
                "payload": {"my_task": True},
            },
        )
        assert resp.status_code == 200

    def test_admin_can_access_any_inbox(
        self,
        storage: InMemoryStorageDriver,
        provisioner: AgentProvisioner,
    ) -> None:
        """Admin users can access any agent's inbox."""
        client = _make_client(storage, provisioner, _admin_auth())
        _provision(client, "agent:reviewer")

        resp = client.get("/api/v2/ipc/inbox/agent:reviewer")
        assert resp.status_code == 200

    def test_admin_can_send_as_any_agent(
        self,
        storage: InMemoryStorageDriver,
        provisioner: AgentProvisioner,
    ) -> None:
        """Admin users can send messages as any agent."""
        client = _make_client(storage, provisioner, _admin_auth())
        _provision(client, "agent:analyst")
        _provision(client, "agent:reviewer")

        resp = client.post(
            "/api/v2/ipc/send",
            json={
                "sender": "agent:analyst",
                "recipient": "agent:reviewer",
                "type": "task",
                "payload": {"admin_task": True},
            },
        )
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# E2E: Input validation
# ---------------------------------------------------------------------------


class TestIPCE2EInputValidation:
    """Test input validation at the REST boundary."""

    def test_path_traversal_in_send_body_rejected(
        self,
        storage: InMemoryStorageDriver,
        provisioner: AgentProvisioner,
    ) -> None:
        """Agent IDs with path traversal characters are rejected in request body."""
        client = _make_client(storage, provisioner, _admin_auth())

        resp = client.post(
            "/api/v2/ipc/send",
            json={
                "sender": "agent:valid",
                "recipient": "../traversal",
                "type": "task",
                "payload": {},
            },
        )
        assert resp.status_code == 400
        assert "Invalid agent_id" in resp.json()["detail"]

    def test_backslash_in_agent_id_rejected(
        self,
        storage: InMemoryStorageDriver,
        provisioner: AgentProvisioner,
    ) -> None:
        """Agent IDs with backslashes are rejected."""
        client = _make_client(storage, provisioner, _admin_auth())

        resp = client.post(
            "/api/v2/ipc/send",
            json={
                "sender": "agent:valid",
                "recipient": "agent\\traversal",
                "type": "task",
                "payload": {},
            },
        )
        assert resp.status_code == 400

    def test_invalid_message_type_rejected(
        self,
        storage: InMemoryStorageDriver,
        provisioner: AgentProvisioner,
    ) -> None:
        """Invalid message type returns 400."""
        client = _make_client(storage, provisioner, _admin_auth())
        _provision(client, "agent:a")
        _provision(client, "agent:b")

        resp = client.post(
            "/api/v2/ipc/send",
            json={
                "sender": "agent:a",
                "recipient": "agent:b",
                "type": "invalid_type",
                "payload": {},
            },
        )
        assert resp.status_code == 400
        assert "Invalid message type" in resp.json()["detail"]

    def test_valid_agent_id_patterns(
        self,
        storage: InMemoryStorageDriver,
        provisioner: AgentProvisioner,
    ) -> None:
        """Valid agent ID patterns are accepted."""
        client = _make_client(storage, provisioner, _admin_auth())

        for agent_id in ["agent:analyst", "my-agent_1", "AGENT.test", "simple"]:
            resp = client.post(f"/api/v2/ipc/provision/{agent_id}")
            assert resp.status_code == 200, f"Failed for agent_id={agent_id!r}"


# ---------------------------------------------------------------------------
# E2E: Error handling
# ---------------------------------------------------------------------------


class TestIPCE2EErrorHandling:
    """Test error handling and edge cases."""

    def test_send_to_unprovisioned_agent_fails(
        self,
        storage: InMemoryStorageDriver,
        provisioner: AgentProvisioner,
    ) -> None:
        """Sending to an agent without a provisioned inbox fails."""
        client = _make_client(storage, provisioner, _admin_auth())
        _provision(client, "agent:sender")

        resp = client.post(
            "/api/v2/ipc/send",
            json={
                "sender": "agent:sender",
                "recipient": "agent:ghost",
                "type": "task",
                "payload": {},
            },
        )
        # InboxNotFoundError is caught by generic except → 500.
        # TODO(#1727): Should be 404 — catch InboxNotFoundError in router.
        assert resp.status_code == 500

    def test_self_send_rejected(
        self,
        storage: InMemoryStorageDriver,
        provisioner: AgentProvisioner,
    ) -> None:
        """Sending a message to yourself is rejected."""
        client = _make_client(storage, provisioner, _admin_auth())
        _provision(client, "agent:self")

        resp = client.post(
            "/api/v2/ipc/send",
            json={
                "sender": "agent:self",
                "recipient": "agent:self",
                "type": "task",
                "payload": {},
            },
        )
        # EnvelopeValidationError caught by generic except → 500.
        # TODO(#1727): Should be 400 — catch EnvelopeValidationError in router.
        assert resp.status_code == 500

    def test_list_unprovisioned_inbox_returns_404(
        self,
        storage: InMemoryStorageDriver,
        provisioner: AgentProvisioner,
    ) -> None:
        """Listing inbox for an unprovisioned agent returns 404."""
        client = _make_client(storage, provisioner, _admin_auth())

        resp = client.get("/api/v2/ipc/inbox/agent:ghost")
        assert resp.status_code == 404
        assert "not found" in resp.json()["detail"].lower()

    def test_count_unprovisioned_inbox_returns_404(
        self,
        storage: InMemoryStorageDriver,
        provisioner: AgentProvisioner,
    ) -> None:
        """Counting inbox for an unprovisioned agent returns 404."""
        client = _make_client(storage, provisioner, _admin_auth())

        resp = client.get("/api/v2/ipc/inbox/agent:ghost/count")
        assert resp.status_code == 404

    def test_provision_is_idempotent(
        self,
        storage: InMemoryStorageDriver,
        provisioner: AgentProvisioner,
    ) -> None:
        """Provisioning the same agent twice succeeds (idempotent)."""
        client = _make_client(storage, provisioner, _admin_auth())
        _provision(client, "agent:analyst")
        _provision(client, "agent:analyst")  # second call also succeeds

    def test_empty_inbox_returns_zero(
        self,
        storage: InMemoryStorageDriver,
        provisioner: AgentProvisioner,
    ) -> None:
        """A provisioned but empty inbox returns total=0 and count=0."""
        client = _make_client(storage, provisioner, _admin_auth())
        _provision(client, "agent:empty")

        resp = client.get("/api/v2/ipc/inbox/agent:empty")
        assert resp.status_code == 200
        assert resp.json()["total"] == 0

        resp = client.get("/api/v2/ipc/inbox/agent:empty/count")
        assert resp.status_code == 200
        assert resp.json()["count"] == 0

    def test_all_message_types_accepted(
        self,
        storage: InMemoryStorageDriver,
        provisioner: AgentProvisioner,
    ) -> None:
        """All valid message types can be sent."""
        client = _make_client(storage, provisioner, _admin_auth())
        _provision(client, "agent:a")
        _provision(client, "agent:b")

        for msg_type in ["task", "response", "event", "cancel"]:
            payload: dict[str, Any] = {}
            correlation_id = None
            if msg_type in ("response", "cancel"):
                correlation_id = "corr_1"

            resp = client.post(
                "/api/v2/ipc/send",
                json={
                    "sender": "agent:a",
                    "recipient": "agent:b",
                    "type": msg_type,
                    "payload": payload,
                    "correlation_id": correlation_id,
                },
            )
            assert resp.status_code == 200, f"Failed for type={msg_type!r}: {resp.json()}"


# ---------------------------------------------------------------------------
# E2E: Performance
# ---------------------------------------------------------------------------


class TestIPCE2EPerformance:
    """Verify endpoint response times are within budget.

    Budgets are generous to avoid CI flakiness. In-memory storage
    eliminates I/O jitter, but TestClient has HTTP serialization overhead.
    """

    def test_provision_latency_under_200ms(
        self,
        storage: InMemoryStorageDriver,
        provisioner: AgentProvisioner,
    ) -> None:
        """Provisioning should complete well under 200ms (in-memory)."""
        client = _make_client(storage, provisioner, _admin_auth())

        start = time.monotonic()
        resp = client.post("/api/v2/ipc/provision/agent:fast")
        elapsed = time.monotonic() - start

        assert resp.status_code == 200
        assert elapsed < 0.2, f"Provision took {elapsed * 1000:.1f}ms (limit: 200ms)"

    def test_send_latency_under_200ms(
        self,
        storage: InMemoryStorageDriver,
        provisioner: AgentProvisioner,
    ) -> None:
        """Sending a message should complete well under 200ms (in-memory)."""
        client = _make_client(storage, provisioner, _admin_auth())
        _provision(client, "agent:fast-sender")
        _provision(client, "agent:fast-receiver")

        start = time.monotonic()
        resp = client.post(
            "/api/v2/ipc/send",
            json={
                "sender": "agent:fast-sender",
                "recipient": "agent:fast-receiver",
                "type": "task",
                "payload": {"quick": True},
            },
        )
        elapsed = time.monotonic() - start

        assert resp.status_code == 200
        assert elapsed < 0.2, f"Send took {elapsed * 1000:.1f}ms (limit: 200ms)"

    def test_list_inbox_latency_under_200ms(
        self,
        storage: InMemoryStorageDriver,
        provisioner: AgentProvisioner,
    ) -> None:
        """Listing inbox should complete well under 200ms (in-memory)."""
        client = _make_client(storage, provisioner, _admin_auth())
        _provision(client, "agent:fast-reader")
        _provision(client, "agent:fast-writer")

        # Add 10 messages
        for _ in range(10):
            client.post(
                "/api/v2/ipc/send",
                json={
                    "sender": "agent:fast-writer",
                    "recipient": "agent:fast-reader",
                    "type": "event",
                    "payload": {},
                },
            )

        start = time.monotonic()
        resp = client.get("/api/v2/ipc/inbox/agent:fast-reader")
        elapsed = time.monotonic() - start

        assert resp.status_code == 200
        assert resp.json()["total"] == 10
        assert elapsed < 0.2, f"List took {elapsed * 1000:.1f}ms (limit: 200ms)"

    def test_bulk_send_throughput(
        self,
        storage: InMemoryStorageDriver,
        provisioner: AgentProvisioner,
    ) -> None:
        """50 messages should be sent within 5 seconds (in-memory)."""
        client = _make_client(storage, provisioner, _admin_auth())
        _provision(client, "agent:bulk-sender")
        _provision(client, "agent:bulk-receiver")

        start = time.monotonic()
        for i in range(50):
            resp = client.post(
                "/api/v2/ipc/send",
                json={
                    "sender": "agent:bulk-sender",
                    "recipient": "agent:bulk-receiver",
                    "type": "task",
                    "payload": {"index": i},
                },
            )
            assert resp.status_code == 200
        elapsed = time.monotonic() - start

        # Verify all delivered
        resp = client.get("/api/v2/ipc/inbox/agent:bulk-receiver/count")
        assert resp.json()["count"] == 50

        assert elapsed < 5.0, f"50 sends took {elapsed:.2f}s (limit: 5.0s)"
