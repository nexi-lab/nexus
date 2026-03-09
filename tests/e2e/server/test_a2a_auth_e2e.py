"""E2E tests for A2A protocol with authentication ENABLED.

These tests start `nexus serve` with --api-key to verify:
1. A2A endpoints enforce auth when server has auth configured
2. Agent Card remains public (no auth required)
3. Tasks are persisted to disk via VFSTaskStore under agent-scoped paths
4. Full lifecycle works with Bearer token auth
5. On-disk format uses MessageEnvelope (§17.6 convergence)

Separate from test_a2a_e2e.py which tests in open-access mode.
"""

import json
import os
import signal
import subprocess
import sys
import threading
from contextlib import suppress
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.e2e.conftest import find_free_port

_src_path = str(Path(__file__).resolve().parent.parent.parent / "src")

API_KEY = "test-a2a-auth-e2e-key-42"


def _drain_pipe(pipe, lines: list[str], ready: "threading.Event | None" = None):
    """Read lines from a subprocess pipe (daemon thread)."""
    try:
        for raw in iter(pipe.readline, b""):
            decoded = raw.decode(errors="replace")
            lines.append(decoded)
            if ready and "Application startup complete" in decoded:
                ready.set()
    except ValueError:
        pass
    finally:
        pipe.close()


@pytest.fixture(scope="function")
def auth_server(isolated_db, tmp_path):
    """Start nexus serve WITH --api-key for auth-enabled testing."""
    storage_path = tmp_path / "storage"
    storage_path.mkdir(exist_ok=True)

    port = find_free_port()
    base_url = f"http://127.0.0.1:{port}"

    env = os.environ.copy()
    env["NEXUS_JWT_SECRET"] = "test-secret-key-for-e2e-auth-a2a"
    env["NEXUS_DATABASE_URL"] = os.environ.get("NEXUS_E2E_DATABASE_URL", f"sqlite:///{isolated_db}")
    env["PYTHONPATH"] = _src_path

    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                f"from nexus.daemon.main import main; "
                f"main(['--host', '127.0.0.1', '--port', '{port}', "
                f"'--data-dir', '{tmp_path}', "
                f"'--api-key', '{API_KEY}'])"
            ),
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        preexec_fn=os.setsid if sys.platform != "win32" else None,
    )

    # Event-driven readiness (same pattern as conftest.nexus_server)
    stderr_lines: list[str] = []
    stdout_lines: list[str] = []
    ready = threading.Event()

    t_err = threading.Thread(
        target=_drain_pipe, args=(process.stderr, stderr_lines, ready), daemon=True
    )
    t_out = threading.Thread(target=_drain_pipe, args=(process.stdout, stdout_lines), daemon=True)
    t_err.start()
    t_out.start()

    if not ready.wait(timeout=120.0):
        process.terminate()
        t_err.join(timeout=2)
        t_out.join(timeout=2)
        pytest.fail(
            f"Auth server failed to start on port {port} "
            f"(never saw 'Application startup complete').\n"
            f"stdout: {''.join(stdout_lines)}\n"
            f"stderr: {''.join(stderr_lines)}"
        )

    yield {
        "port": port,
        "base_url": base_url,
        "process": process,
        "data_dir": tmp_path,
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


def _rpc(
    method: str,
    params: dict[str, Any] | None = None,
    request_id: str = "auth-e2e-1",
) -> dict[str, Any]:
    body: dict[str, Any] = {"jsonrpc": "2.0", "method": method, "id": request_id}
    if params is not None:
        body["params"] = params
    return body


# ======================================================================
# Auth Enforcement
# ======================================================================


class TestA2AAuthEnforcement:
    """Verify A2A enforces auth when server has --api-key."""

    def test_agent_card_public_no_auth_needed(self, auth_server):
        """Agent Card is always public per A2A spec."""
        with httpx.Client(
            base_url=auth_server["base_url"], timeout=30.0, trust_env=False
        ) as client:
            resp = client.get("/.well-known/agent.json")
            assert resp.status_code == 200
            data = resp.json()
            assert "name" in data

    def test_a2a_endpoint_rejects_without_auth(self, auth_server):
        """POST /a2a returns 401 without Authorization header."""
        body = _rpc(
            "a2a.tasks.send",
            {"message": {"role": "user", "parts": [{"type": "text", "text": "no auth"}]}},
        )
        with httpx.Client(
            base_url=auth_server["base_url"], timeout=30.0, trust_env=False
        ) as client:
            resp = client.post("/a2a", json=body)
            assert resp.status_code == 401
            assert "WWW-Authenticate" in resp.headers

    def test_a2a_endpoint_works_with_auth(self, auth_server):
        """POST /a2a succeeds with valid Bearer token."""
        body = _rpc(
            "a2a.tasks.send",
            {"message": {"role": "user", "parts": [{"type": "text", "text": "with auth"}]}},
            request_id="auth-ok-1",
        )
        with httpx.Client(
            base_url=auth_server["base_url"], timeout=30.0, trust_env=False
        ) as client:
            resp = client.post(
                "/a2a",
                json=body,
                headers={"Authorization": f"Bearer {API_KEY}"},
            )
            assert resp.status_code == 200
            data = resp.json()
            assert "result" in data
            assert data["result"]["status"]["state"] == "submitted"


# ======================================================================
# Persistence — agent-scoped paths + MessageEnvelope (§17.6)
# ======================================================================


class TestA2APersistenceE2E:
    """Verify tasks are persisted to disk via VFSTaskStore.

    After §17.6 convergence, tasks are stored under agent-scoped paths:
        {data_dir}/agents/{agent_id}/tasks/{ts}_{task_id}.json

    When no agent_id is extracted from auth, the fallback is "_unassigned".
    Files are wrapped in MessageEnvelope format for IPC interoperability.
    """

    def test_task_creates_json_file_on_disk(self, auth_server):
        """Creating a task via A2A produces a .json file under agents/ directory."""
        body = _rpc(
            "a2a.tasks.send",
            {"message": {"role": "user", "parts": [{"type": "text", "text": "persist check"}]}},
            request_id="persist-1",
        )
        with httpx.Client(
            base_url=auth_server["base_url"], timeout=30.0, trust_env=False
        ) as client:
            resp = client.post(
                "/a2a",
                json=body,
                headers={"Authorization": f"Bearer {API_KEY}"},
            )
            assert resp.status_code == 200
            task_id = resp.json()["result"]["id"]

        # Check disk — tasks live under /root/agents/{agent_id}/tasks/
        # (zone-scoped: LocalStorageDriver stores under <root>/<zone_id>/)
        data_dir = auth_server["data_dir"]
        zone_dir = data_dir / "root"
        agents_dir = zone_dir / "agents"
        assert agents_dir.exists(), f"Agents directory not found: {agents_dir}"

        # Find the task JSON file under any agent's tasks/ directory
        all_json = [f for f in agents_dir.rglob(f"*_{task_id}.json") if "tasks" in f.parts]
        assert len(all_json) == 1, f"Expected 1 file for task {task_id}, found {len(all_json)}"

        # Verify the file is inside a tasks/ directory
        task_file = all_json[0]
        assert task_file.parent.name == "tasks", (
            f"Task file should be in a tasks/ directory, got: {task_file.parent}"
        )

        # Verify on-disk format is MessageEnvelope (§17.6 convergence)
        content = json.loads(task_file.read_bytes())
        assert content["type"] == "task", (
            f"Expected MessageEnvelope type='task', got: {content.get('type')}"
        )
        assert "payload" in content, "MessageEnvelope should have 'payload' field"
        assert content["payload"]["id"] == task_id
        assert content["payload"]["status"]["state"] == "submitted"

        # Verify envelope metadata
        assert content["from"] == "a2a_gateway"
        assert content["correlation_id"] == task_id

    def test_full_lifecycle_with_persistence(self, auth_server):
        """Create -> Get -> Cancel with all state changes persisted."""
        headers = {"Authorization": f"Bearer {API_KEY}"}
        with httpx.Client(
            base_url=auth_server["base_url"], timeout=30.0, trust_env=False
        ) as client:
            # Create
            create_resp = client.post(
                "/a2a",
                json=_rpc(
                    "a2a.tasks.send",
                    {"message": {"role": "user", "parts": [{"type": "text", "text": "lifecycle"}]}},
                    request_id="lc-create",
                ),
                headers=headers,
            )
            assert create_resp.status_code == 200
            task_id = create_resp.json()["result"]["id"]

            # Get — should return submitted
            get_resp = client.post(
                "/a2a",
                json=_rpc("a2a.tasks.get", {"taskId": task_id}, request_id="lc-get"),
                headers=headers,
            )
            assert get_resp.json()["result"]["status"]["state"] == "submitted"

            # Cancel
            cancel_resp = client.post(
                "/a2a",
                json=_rpc("a2a.tasks.cancel", {"taskId": task_id}, request_id="lc-cancel"),
                headers=headers,
            )
            assert cancel_resp.json()["result"]["status"]["state"] == "canceled"

        # Verify canceled state is persisted on disk under agent-scoped path
        # (zone-scoped: LocalStorageDriver stores under <root>/<zone_id>/)
        data_dir = auth_server["data_dir"]
        zone_dir = data_dir / "root"
        agents_dir = zone_dir / "agents"
        task_files = list(agents_dir.rglob(f"*_{task_id}.json"))
        assert len(task_files) == 1
        content = json.loads(task_files[0].read_bytes())

        # Verify MessageEnvelope wraps the canceled task
        assert content["type"] == "task"
        assert content["payload"]["status"]["state"] == "canceled"
        assert content["payload"]["id"] == task_id
