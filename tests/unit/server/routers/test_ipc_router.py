"""Tests for IPC REST router compatibility endpoints."""

import asyncio
import json
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from nexus.bricks.ipc.conventions import inbox_path
from nexus.bricks.ipc.provisioning import AgentProvisioner
from nexus.bricks.ipc.wakeup import CacheStoreEventPublisher
from nexus.cache import InMemoryCacheStore
from nexus.server.api.v2.routers.ipc import (
    SendMessageRequest,
    router,
    send_message,
    stream_inbox,
)
from tests.unit.bricks.ipc.fakes import InMemoryStorageDriver


def _override_auth(app: FastAPI) -> None:
    from nexus.server.api.v2.routers.ipc import _get_require_auth

    app.dependency_overrides[_get_require_auth()] = lambda: {
        "authenticated": True,
        "subject_type": "agent",
        "subject_id": "agent:alice",
        "x_agent_id": "agent:alice",
        "zone_id": "root",
        "is_admin": True,
    }


def _override_auth_zone(app: FastAPI, zone_id: str) -> None:
    from nexus.server.api.v2.routers.ipc import _get_require_auth

    app.dependency_overrides[_get_require_auth()] = lambda: {
        "authenticated": True,
        "subject_type": "agent",
        "subject_id": "agent:alice",
        "x_agent_id": "agent:alice",
        "zone_id": zone_id,
        "is_admin": True,
    }


def test_post_send_enqueues_message() -> None:
    storage = InMemoryStorageDriver()
    provisioner = AgentProvisioner(storage, zone_id="root")
    asyncio.run(provisioner.provision("agent:alice", name="Alice"))
    asyncio.run(provisioner.provision("agent:bob", name="Bob"))

    app = FastAPI()
    app.state.ipc_nexus_fs = storage
    app.state.ipc_event_publisher = None
    app.state.ipc_cache_store = None
    app.state.zone_id = "root"
    app.include_router(router)
    _override_auth(app)

    client = TestClient(app)
    response = client.post(
        "/api/v2/ipc/send",
        json={
            "sender": "agent:alice",
            "recipient": "agent:bob",
            "type": "task",
            "payload": {"body": "hello"},
            "message_id": "msg_router_send",
        },
    )

    assert response.status_code == 200
    data = response.json()
    assert data["message_id"] == "msg_router_send"
    assert data["sender"] == "agent:alice"
    assert data["recipient"] == "agent:bob"

    inbox_entries = storage.list_dir(inbox_path("agent:bob"), "root")
    assert any("msg_router_send" in entry for entry in inbox_entries)

    msg_filename = next(entry for entry in inbox_entries if "msg_router_send" in entry)
    payload = storage.sys_read(f"{inbox_path('agent:bob')}/{msg_filename}", "root").decode("utf-8")
    envelope = json.loads(payload)
    assert envelope["from"] == "agent:alice"
    assert envelope["to"] == "agent:bob"
    assert envelope["payload"]["body"] == "hello"


def test_post_send_generates_message_id_when_omitted() -> None:
    storage = InMemoryStorageDriver()
    provisioner = AgentProvisioner(storage, zone_id="root")
    asyncio.run(provisioner.provision("agent:alice", name="Alice"))
    asyncio.run(provisioner.provision("agent:bob", name="Bob"))

    app = FastAPI()
    app.state.ipc_nexus_fs = storage
    app.state.ipc_event_publisher = None
    app.state.ipc_cache_store = None
    app.state.zone_id = "wrong-zone"
    app.include_router(router)
    _override_auth(app)

    client = TestClient(app)
    response = client.post(
        "/api/v2/ipc/send",
        json={
            "sender": "agent:alice",
            "recipient": "agent:bob",
            "type": "task",
            "payload": {"body": "hello"},
        },
    )

    assert response.status_code == 200
    data = response.json()
    assert isinstance(data["message_id"], str)
    assert data["message_id"]

    inbox_entries = storage.list_dir(inbox_path("agent:bob"), "root")
    assert any(data["message_id"] in entry for entry in inbox_entries)


def test_inbox_and_count_return_compatibility_shapes() -> None:
    storage = InMemoryStorageDriver()
    provisioner = AgentProvisioner(storage, zone_id="root")
    asyncio.run(provisioner.provision("agent:alice", name="Alice"))
    asyncio.run(provisioner.provision("agent:bob", name="Bob"))

    app = FastAPI()
    app.state.ipc_nexus_fs = storage
    app.state.ipc_event_publisher = None
    app.state.ipc_cache_store = None
    app.state.zone_id = "root"
    app.include_router(router)
    _override_auth(app)

    client = TestClient(app)
    send_response = client.post(
        "/api/v2/ipc/send",
        json={
            "sender": "agent:alice",
            "recipient": "agent:bob",
            "type": "task",
            "payload": {"body": "hello"},
            "message_id": "msg_router_list",
        },
    )
    assert send_response.status_code == 200

    inbox_response = client.get("/api/v2/ipc/inbox/agent:bob")
    assert inbox_response.status_code == 200
    inbox_data = inbox_response.json()
    assert inbox_data["agent_id"] == "agent:bob"
    assert inbox_data["count"] == 1
    assert len(inbox_data["messages"]) == 1
    assert "msg_router_list" in inbox_data["messages"][0]["filename"]

    count_response = client.get("/api/v2/ipc/inbox/agent:bob/count")
    assert count_response.status_code == 200
    count_data = count_response.json()
    assert count_data == {"agent_id": "agent:bob", "count": 1}


def test_rest_endpoints_use_authenticated_zone_instead_of_app_state() -> None:
    storage = InMemoryStorageDriver()
    provisioner = AgentProvisioner(storage, zone_id="tenant-a")
    asyncio.run(provisioner.provision("agent:alice", name="Alice"))
    asyncio.run(provisioner.provision("agent:bob", name="Bob"))

    app = FastAPI()
    app.state.ipc_nexus_fs = storage
    app.state.ipc_event_publisher = None
    app.state.ipc_cache_store = None
    app.state.zone_id = "root"
    app.include_router(router)
    _override_auth_zone(app, "tenant-a")

    client = TestClient(app)
    send_response = client.post(
        "/api/v2/ipc/send",
        json={
            "sender": "agent:alice",
            "recipient": "agent:bob",
            "type": "task",
            "payload": {"body": "hello"},
            "message_id": "msg_router_zone",
        },
    )
    assert send_response.status_code == 200

    tenant_entries = storage.list_dir(inbox_path("agent:bob"), "tenant-a")
    assert any("msg_router_zone" in entry for entry in tenant_entries)

    root_entries_error: FileNotFoundError | None = None
    try:
        storage.list_dir(inbox_path("agent:bob"), "root")
    except FileNotFoundError as exc:
        root_entries_error = exc
    assert root_entries_error is not None

    inbox_response = client.get("/api/v2/ipc/inbox/agent:bob")
    assert inbox_response.status_code == 200
    assert inbox_response.json()["count"] == 1

    count_response = client.get("/api/v2/ipc/inbox/agent:bob/count")
    assert count_response.status_code == 200
    assert count_response.json() == {"agent_id": "agent:bob", "count": 1}


def test_sse_stream_emits_connected_and_delivery_event() -> None:
    storage = InMemoryStorageDriver()
    provisioner = AgentProvisioner(storage, zone_id="root")
    asyncio.run(provisioner.provision("agent:alice", name="Alice"))
    asyncio.run(provisioner.provision("agent:bob", name="Bob"))

    cache_store = InMemoryCacheStore()
    event_publisher = CacheStoreEventPublisher(cache_store)
    app = FastAPI()
    app.state.ipc_cache_store = cache_store

    async def _run() -> tuple[str, str]:
        request = SimpleNamespace(
            app=app,
            is_disconnected=lambda: asyncio.sleep(0, result=False),
        )
        auth = {
            "authenticated": True,
            "subject_type": "agent",
            "subject_id": "agent:alice",
            "x_agent_id": "agent:alice",
            "zone_id": "root",
            "is_admin": True,
        }

        response = await stream_inbox("agent:bob", request, auth, cache_store)
        connected_event = await anext(response.body_iterator)
        delivery_task = asyncio.create_task(anext(response.body_iterator))
        send_task = asyncio.create_task(
            send_message(
                SendMessageRequest(
                    sender="agent:alice",
                    recipient="agent:bob",
                    type="task",
                    payload={"body": "hello"},
                    message_id="msg_router_sse",
                ),
                request,
                auth,
                storage,
                event_publisher,
                cache_store,
            )
        )
        delivery_event = await asyncio.wait_for(delivery_task, timeout=1.0)
        send_result = await send_task
        assert send_result["message_id"] == "msg_router_sse"
        return connected_event, delivery_event

    connected_event, delivery_event = asyncio.run(_run())
    assert "event: connected" in connected_event
    assert '"agent_id": "agent:bob"' in connected_event
    assert "event: message_delivered" in delivery_event
    assert "msg_router_sse" in delivery_event
