"""E2E tests for Filesystem-as-IPC (#1411).

Tests the full IPC flow through the actual FastAPI server:
1. Start server with auth + permissions enabled
2. Create agent directories via JSON-RPC
3. Send messages by writing to inbox paths
4. Read and verify messages
5. Test permission enforcement (unauthenticated requests rejected)

Uses a custom auth_server fixture that starts nexus serve with --api-key.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
from contextlib import closing, suppress
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

# ── Fixtures: authenticated server ──────────────────────────────────────

_src_path = Path(__file__).parent.parent.parent / "src"


def _find_free_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("", 0))
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        return s.getsockname()[1]


@pytest.fixture(scope="function")
def auth_server(tmp_path):
    """Start nexus serve with database auth + permissions for e2e tests.

    Uses --auth-type database --init to create tables and an admin key,
    then extracts the generated admin API key from .nexus-admin-env.
    """
    import re
    import uuid

    db_path = tmp_path / f"test_ipc_{uuid.uuid4().hex[:8]}.db"
    storage_path = tmp_path / "storage"
    storage_path.mkdir(exist_ok=True)

    port = _find_free_port()
    base_url = f"http://127.0.0.1:{port}"

    env = os.environ.copy()
    env["NEXUS_JWT_SECRET"] = "test-secret-key-for-e2e-12345"
    env["NEXUS_DATABASE_URL"] = f"sqlite:///{db_path}"
    env["NEXUS_ENFORCE_PERMISSIONS"] = "true"
    env["NEXUS_RATE_LIMIT_ENABLED"] = "false"
    env["PYTHONPATH"] = str(_src_path)

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
        stdout, stderr = process.communicate(timeout=10)
        pytest.fail(
            f"Auth server failed to start on port {port}.\n"
            f"stdout: {stdout.decode()[:2000]}\nstderr: {stderr.decode()[:2000]}"
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
                f"output tail: {all_output[-1000:]}"
            )

    yield {
        "port": port,
        "base_url": base_url,
        "process": process,
        "admin_api_key": admin_api_key,
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
    """Authenticated httpx client with admin Bearer token."""
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
        "method": "read",
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


class TestIPCLocal:
    """Lightweight IPC tests using the IPC module directly (no server).

    These always run and test the core IPC logic.
    """

    @pytest.mark.asyncio
    async def test_full_lifecycle_no_server(self) -> None:
        """Full IPC lifecycle using InMemoryVFS."""
        from nexus.ipc.delivery import MessageProcessor, MessageSender
        from nexus.ipc.envelope import MessageEnvelope, MessageType
        from nexus.ipc.provisioning import AgentProvisioner
        from tests.unit.ipc.fakes import InMemoryEventPublisher, InMemoryVFS

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
        from nexus.ipc.conventions import dead_letter_path, inbox_path
        from nexus.ipc.delivery import MessageProcessor, MessageSender
        from nexus.ipc.envelope import MessageEnvelope, MessageType
        from nexus.ipc.provisioning import AgentProvisioner
        from tests.unit.ipc.fakes import InMemoryVFS

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
        assert len(inbox_files) == 0
        assert len(dl_files) == 1

    @pytest.mark.asyncio
    async def test_discovery_flow(self) -> None:
        """End-to-end discovery: provision → ls → get card → find by skill."""
        from nexus.ipc.discovery import AgentDiscovery
        from nexus.ipc.provisioning import AgentProvisioner
        from tests.unit.ipc.fakes import InMemoryVFS

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
