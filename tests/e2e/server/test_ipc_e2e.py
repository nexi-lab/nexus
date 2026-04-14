"""E2E tests for Filesystem-as-IPC (#1411).

Tests the full IPC flow through the actual FastAPI server:
1. Start server with auth + permissions enabled
2. Create agent directories via JSON-RPC
3. Send messages by writing to inbox paths
4. Read and verify messages
5. Test permission enforcement (unauthenticated requests rejected)

Uses the shared e2e server fixture with API-key auth enabled.
"""

import json
import os
import signal
import socket
import subprocess
import sys
import time
from contextlib import closing, suppress
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path

import httpx
import pytest

# ── Fixtures: authenticated server ──────────────────────────────────────

_src_path = Path(__file__).resolve().parents[3] / "src"


@lru_cache(maxsize=1)
def _resolve_daemon_python() -> str:
    """Pick a Python interpreter with the full _nexus_raft extension available."""
    import shutil

    candidates: list[str] = []
    env_python = os.environ.get("NEXUS_E2E_PYTHON")
    if env_python:
        candidates.append(env_python)
    candidates.extend(
        candidate
        for candidate in [
            sys.executable,
            shutil.which("python3"),
            shutil.which("python"),
            "/opt/anaconda3/bin/python",
        ]
        if candidate
    )

    probe = "from _nexus_raft import ZoneManager; print('ok')"
    seen: set[str] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        try:
            result = subprocess.run(
                [candidate, "-c", probe],
                check=False,
                capture_output=True,
                text=True,
                env={**os.environ, "PYTHONPATH": str(_src_path)},
            )
        except OSError:
            continue
        if result.returncode == 0 and "ok" in result.stdout:
            return candidate

    raise RuntimeError("No Python interpreter with _nexus_raft.ZoneManager available for e2e")


def _find_free_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("", 0))
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        return s.getsockname()[1]


@pytest.fixture(scope="function")
def auth_server(tmp_path):
    """Start a Nexus server with static API-key auth for IPC e2e tests."""
    import uuid

    db_path = tmp_path / f"test_ipc_{uuid.uuid4().hex[:8]}.db"
    storage_path = tmp_path / "storage"
    storage_path.mkdir(exist_ok=True)

    port = _find_free_port()
    base_url = f"http://127.0.0.1:{port}"

    env = os.environ.copy()
    env["NEXUS_JWT_SECRET"] = "test-secret-key-for-e2e-12345"
    env["NEXUS_DATABASE_URL"] = f"sqlite:///{db_path}"
    env["NEXUS_API_KEY"] = "test-e2e-api-key-12345"
    env["PYTHONPATH"] = str(_src_path)
    env["HOME"] = str(tmp_path)

    process = subprocess.Popen(
        [
            _resolve_daemon_python(),
            "-c",
            (
                "from nexus.daemon.main import main; "
                f"main(['--host', '127.0.0.1', '--port', '{port}', "
                f"'--data-dir', '{tmp_path}'])"
            ),
        ],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        cwd=str(tmp_path),
        preexec_fn=os.setsid if sys.platform != "win32" else None,
    )

    # Wait for server
    start = time.time()
    while time.time() - start < 45.0:
        try:
            r = httpx.get(f"{base_url}/health", timeout=1.0, trust_env=False)
            if r.status_code == 200:
                break
        except (httpx.ConnectError, httpx.ReadTimeout):
            pass
        time.sleep(0.1)
    else:
        process.terminate()
        process.wait(timeout=10)
        pytest.fail(f"Auth server failed to start on port {port}.")

    yield {
        "port": port,
        "base_url": base_url,
        "process": process,
        "admin_api_key": env["NEXUS_API_KEY"],
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


@pytest.fixture(scope="function")
def auth_client(auth_server) -> httpx.Client:
    """Authenticated httpx client with the test Bearer token."""
    with httpx.Client(
        base_url=auth_server["base_url"],
        timeout=30.0,
        trust_env=False,
        headers={"Authorization": f"Bearer {auth_server['admin_api_key']}"},
    ) as client:
        yield client


@pytest.fixture(scope="function")
def unauth_client(auth_server) -> httpx.Client:
    """Unauthenticated httpx client (no Bearer token)."""
    with httpx.Client(
        base_url=auth_server["base_url"],
        timeout=30.0,
        trust_env=False,
    ) as client:
        yield client


def _rpc_call(
    client: httpx.Client,
    method: str,
    params: dict,
) -> dict:
    """Make an RPC call to the Nexus server.

    The API accepts POST to /api/nfs/{method} with a JSON-RPC body:
    ``{"jsonrpc": "2.0", "method": "<method>", "params": {...}, "id": "1"}``

    Auth is handled by the API key in the server's environment.
    """
    body = {
        "jsonrpc": "2.0",
        "method": method,
        "params": params,
        "id": "1",
    }
    response = client.post(f"/api/nfs/{method}", json=body)
    if response.status_code != 200:
        raise RuntimeError(f"RPC {method} failed ({response.status_code}): {response.text}")
    data = response.json()
    if isinstance(data, dict) and "error" in data:
        raise RuntimeError(f"RPC error: {data['error']}")
    result = data.get("result", data) if isinstance(data, dict) else data
    return result if isinstance(result, dict) else {"raw": result}


def _write_file(client: httpx.Client, path: str, content: str | bytes) -> dict:
    """Write a file via RPC."""
    if isinstance(content, bytes):
        content = content.decode("utf-8")
    return _rpc_call(client, "write", {"path": path, "content": content})


def _read_file(client: httpx.Client, path: str) -> str:
    """Read a file via RPC. Returns content as string."""
    import base64

    body = {
        "jsonrpc": "2.0",
        "method": "sys_read",
        "params": {"path": path},
        "id": "1",
    }
    response = client.post("/api/nfs/read", json=body)
    if response.status_code != 200:
        raise RuntimeError(f"Read failed ({response.status_code}): {response.text}")
    data = response.json()
    if isinstance(data, dict) and "error" in data:
        raise RuntimeError(f"Read error: {data['error']}")
    result = data.get("result", data) if isinstance(data, dict) else data
    # Result may be base64-encoded bytes dict
    if isinstance(result, dict):
        if "__type__" in result and result["__type__"] == "bytes":
            return base64.b64decode(result["data"]).decode("utf-8")
        content = result.get("content", "")
        if isinstance(content, dict) and "__type__" in content:
            return base64.b64decode(content["data"]).decode("utf-8")
        return str(content)
    return str(result)


def _list_dir(client: httpx.Client, path: str) -> list:
    """List directory via RPC. Returns list of file/dir entries."""
    result = _rpc_call(client, "list", {"path": path})
    return result.get("files", [])


def _mkdir(client: httpx.Client, path: str) -> dict:
    """Create directory via RPC."""
    return _rpc_call(client, "mkdir", {"path": path, "exist_ok": True})


def _provision_ipc_agent(client: httpx.Client, agent_id: str) -> None:
    """Create the standard IPC directory layout for one agent."""
    _mkdir(client, "/agents")
    _mkdir(client, f"/agents/{agent_id}")
    _mkdir(client, f"/agents/{agent_id}/inbox")
    _mkdir(client, f"/agents/{agent_id}/outbox")
    _mkdir(client, f"/agents/{agent_id}/processed")
    _mkdir(client, f"/agents/{agent_id}/dead_letter")
    _mkdir(client, f"/agents/{agent_id}/tasks")


def _make_envelope(
    sender: str,
    recipient: str,
    msg_id: str = "msg_e2e_001",
    msg_type: str = "task",
    payload: dict | None = None,
) -> dict:
    """Create a message envelope dict."""
    return {
        "nexus_message": "1.0",
        "id": msg_id,
        "from": sender,
        "to": recipient,
        "type": msg_type,
        "correlation_id": None,
        "timestamp": datetime.now(UTC).isoformat(),
        "ttl_seconds": None,
        "payload": payload or {"action": "test"},
    }


class TestIPCViaServer:
    """E2E tests for IPC through the actual Nexus server with auth enabled."""

    def test_unauthenticated_request_rejected(
        self,
        unauth_client: httpx.Client,
    ) -> None:
        """Verify unauthenticated requests are rejected (401)."""
        body = {
            "jsonrpc": "2.0",
            "method": "mkdir",
            "params": {"path": "/agents", "exist_ok": True},
            "id": "1",
        }
        response = unauth_client.post("/api/nfs/mkdir", json=body)
        assert response.status_code == 401

    def test_agent_directory_creation(self, auth_client: httpx.Client) -> None:
        """Create agent IPC directories via JSON-RPC."""
        # Create agent directory structure
        _mkdir(auth_client, "/agents")
        _mkdir(auth_client, "/agents/analyst")
        _mkdir(auth_client, "/agents/analyst/inbox")
        _mkdir(auth_client, "/agents/analyst/outbox")
        _mkdir(auth_client, "/agents/analyst/processed")
        _mkdir(auth_client, "/agents/analyst/dead_letter")

        # Write agent card
        card = {
            "name": "Analyst",
            "agent_id": "analyst",
            "skills": ["research", "data_analysis"],
            "status": "connected",
            "inbox": "/agents/analyst/inbox",
        }
        _write_file(auth_client, "/agents/analyst/AGENT.json", json.dumps(card))

        # Verify: list /agents/ shows the analyst
        entries = _list_dir(auth_client, "/agents")
        names = [e.get("name", e) if isinstance(e, dict) else str(e) for e in entries]
        assert any("analyst" in n for n in names)

        # Verify: read AGENT.json
        content = _read_file(auth_client, "/agents/analyst/AGENT.json")
        restored = json.loads(content)
        assert restored["name"] == "Analyst"
        assert restored["skills"] == ["research", "data_analysis"]

    def test_message_send_and_read(self, auth_client: httpx.Client) -> None:
        """Send a message to an agent's inbox and read it back."""
        _mkdir(auth_client, "/agents")
        for agent_id in ["sender_agent", "receiver_agent"]:
            _mkdir(auth_client, f"/agents/{agent_id}")
            _mkdir(auth_client, f"/agents/{agent_id}/inbox")
            _mkdir(auth_client, f"/agents/{agent_id}/outbox")

        envelope = _make_envelope(
            sender="sender_agent",
            recipient="receiver_agent",
            msg_id="msg_e2e_test_001",
            payload={"action": "review", "file": "/workspace/draft.py"},
        )
        filename = "20260213T000000_msg_e2e_test_001.json"
        msg_path = f"/agents/receiver_agent/inbox/{filename}"
        _write_file(auth_client, msg_path, json.dumps(envelope))

        # Verify: list inbox shows the message
        inbox_entries = _list_dir(auth_client, "/agents/receiver_agent/inbox")
        filenames = [e.get("name", e) if isinstance(e, dict) else str(e) for e in inbox_entries]
        assert any(filename in f for f in filenames)

        # Verify: read the message and validate envelope
        content = _read_file(auth_client, msg_path)
        restored = json.loads(content)
        assert restored["id"] == "msg_e2e_test_001"
        assert restored["from"] == "sender_agent"
        assert restored["to"] == "receiver_agent"
        assert restored["payload"]["action"] == "review"

    def test_discovery_via_ls(self, auth_client: httpx.Client) -> None:
        """Discover agents via ls /agents/ + read AGENT.json."""
        _mkdir(auth_client, "/agents")
        for agent_id, skills in [
            ("search_agent", ["semantic_search", "indexing"]),
            ("chat_agent", ["conversation", "summarization"]),
        ]:
            _mkdir(auth_client, f"/agents/{agent_id}")
            _mkdir(auth_client, f"/agents/{agent_id}/inbox")
            card = {
                "name": agent_id,
                "skills": skills,
                "status": "connected",
            }
            _write_file(
                auth_client,
                f"/agents/{agent_id}/AGENT.json",
                json.dumps(card),
            )

        # Discovery: list /agents/
        entries = _list_dir(auth_client, "/agents")
        agent_names = [e.get("name", e) if isinstance(e, dict) else str(e) for e in entries]
        assert any("search_agent" in n for n in agent_names)
        assert any("chat_agent" in n for n in agent_names)

        # Discovery: read AGENT.json for capabilities
        search_card = json.loads(_read_file(auth_client, "/agents/search_agent/AGENT.json"))
        assert "semantic_search" in search_card["skills"]

    def test_rest_compat_send_inbox_count(self, auth_client: httpx.Client) -> None:
        """Compatibility REST endpoints work through the real daemon."""
        _provision_ipc_agent(auth_client, "agent:alice")
        _provision_ipc_agent(auth_client, "agent:bob")

        send_response = auth_client.post(
            "/api/v2/ipc/send",
            json={
                "sender": "agent:alice",
                "recipient": "agent:bob",
                "type": "task",
                "payload": {"body": "hello from daemon"},
                "message_id": "msg_daemon_rest_compat",
            },
        )
        assert send_response.status_code == 200, send_response.text
        send_data = send_response.json()
        assert send_data["message_id"] == "msg_daemon_rest_compat"
        assert send_data["sender"] == "agent:alice"
        assert send_data["recipient"] == "agent:bob"

        inbox_response = auth_client.get("/api/v2/ipc/inbox/agent:bob")
        assert inbox_response.status_code == 200, inbox_response.text
        inbox_data = inbox_response.json()
        assert inbox_data["agent_id"] == "agent:bob"
        assert inbox_data["count"] == 1
        assert len(inbox_data["messages"]) == 1
        assert "msg_daemon_rest_compat" in inbox_data["messages"][0]["filename"]

        count_response = auth_client.get("/api/v2/ipc/inbox/agent:bob/count")
        assert count_response.status_code == 200, count_response.text
        assert count_response.json() == {"agent_id": "agent:bob", "count": 1}

    def test_sse_endpoint_returns_event_stream_headers(self, auth_client: httpx.Client) -> None:
        """SSE endpoint is exposed through the live daemon."""
        with auth_client.stream("GET", "/api/v2/ipc/stream/agent:alice") as response:
            assert response.status_code == 200
            assert "text/event-stream" in response.headers.get("content-type", "")
            assert response.headers.get("x-accel-buffering") == "no"
            assert response.headers.get("cache-control") == "no-cache"


class TestIPCLocal:
    """Lightweight IPC tests using the IPC module directly (no server).

    These always run and test the core IPC logic.
    """

    @pytest.mark.asyncio
    async def test_full_lifecycle_no_server(self) -> None:
        """Full IPC lifecycle using InMemoryVFS."""
        from nexus.bricks.ipc.delivery import MessageProcessor, MessageSender
        from nexus.bricks.ipc.envelope import MessageEnvelope, MessageType
        from nexus.bricks.ipc.provisioning import AgentProvisioner
        from tests.unit.bricks.ipc.fakes import InMemoryEventPublisher, InMemoryVFS

        vfs = InMemoryVFS()
        publisher = InMemoryEventPublisher()
        zone = "e2e-local"

        # Provision agents
        prov = AgentProvisioner(vfs, zone_id=zone)
        await prov.provision("alice", skills=["coding"])
        await prov.provision("bob", skills=["reviewing"])

        # Alice sends to Bob
        sender = MessageSender(vfs, publisher, zone_id=zone)
        env = MessageEnvelope(
            sender="alice",
            recipient="bob",
            type=MessageType.TASK,
            id="msg_lifecycle",
            payload={"review_this": "/workspace/main.py"},
        )
        await sender.send(env)

        # Bob processes
        received: list[MessageEnvelope] = []

        async def handler(msg: MessageEnvelope) -> None:
            received.append(msg)

        processor = MessageProcessor(vfs, "bob", handler, zone_id=zone)
        await processor.process_inbox()

        assert len(received) == 1
        assert received[0].payload["review_this"] == "/workspace/main.py"
        assert len(publisher.published) == 1

    @pytest.mark.asyncio
    async def test_dead_letter_flow(self) -> None:
        """Message goes to dead_letter when handler fails."""
        from nexus.bricks.ipc.conventions import dead_letter_path, inbox_path
        from nexus.bricks.ipc.delivery import MessageProcessor, MessageSender
        from nexus.bricks.ipc.envelope import MessageEnvelope, MessageType
        from nexus.bricks.ipc.provisioning import AgentProvisioner
        from tests.unit.bricks.ipc.fakes import InMemoryVFS

        vfs = InMemoryVFS()
        zone = "e2e-local"

        prov = AgentProvisioner(vfs, zone_id=zone)
        await prov.provision("alice", skills=["sending"])
        await prov.provision("bob", skills=["failing"])

        sender = MessageSender(vfs, zone_id=zone)
        env = MessageEnvelope(
            sender="alice",
            recipient="bob",
            type=MessageType.TASK,
            id="msg_deadletter",
            payload={"action": "fail_task"},
        )
        await sender.send(env)

        async def failing(msg: MessageEnvelope) -> None:
            raise ValueError("I can't process this")

        proc = MessageProcessor(vfs, "bob", failing, zone_id=zone)
        await proc.process_inbox()

        inbox_files = await vfs.list_dir(inbox_path("bob"), zone)
        dl_files = await vfs.list_dir(dead_letter_path("bob"), zone)
        dl_msgs = [f for f in dl_files if not f.endswith(".reason.json")]
        assert len(inbox_files) == 0
        assert len(dl_msgs) == 1

    @pytest.mark.asyncio
    async def test_discovery_flow(self) -> None:
        """End-to-end discovery: provision → ls → get card → find by skill."""
        from nexus.bricks.ipc.discovery import AgentDiscovery
        from nexus.bricks.ipc.provisioning import AgentProvisioner
        from tests.unit.bricks.ipc.fakes import InMemoryVFS

        vfs = InMemoryVFS()
        zone = "e2e-local"

        prov = AgentProvisioner(vfs, zone_id=zone)
        await prov.provision("coder", skills=["python", "rust"])
        await prov.provision("tester", skills=["python", "testing"])
        await prov.provision("writer", skills=["docs", "markdown"])

        discovery = AgentDiscovery(vfs, zone_id=zone)

        # Discover all
        all_agents = await discovery.discover_all()
        assert len(all_agents) == 3

        # Find Python-skilled agents
        python_agents = await discovery.find_by_skill("python")
        assert len(python_agents) == 2
        ids = {a.agent_id for a in python_agents}
        assert ids == {"coder", "tester"}

    # NOTE: test_cross_zone_ipc_roundtrip removed — CrossZoneStorageDriver
    # deleted in #1178 (IPC routed through kernel VFS). Cross-zone routing
    # will be handled by kernel federation mechanisms.
