"""E2E tests for A2A protocol with authentication ENABLED.

These tests start `nexus serve` with --api-key to verify:
1. A2A endpoints enforce auth when server has auth configured
2. Agent Card remains public (no auth required)
3. Tasks are persisted to disk via VFSTaskStore
4. Full lifecycle works with Bearer token auth

Separate from test_a2a_e2e.py which tests in open-access mode.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from contextlib import suppress
from pathlib import Path
from typing import Any

import httpx
import pytest

# Reuse port finder from conftest
from tests.e2e.conftest import find_free_port, wait_for_server

_src_path = str(Path(__file__).resolve().parent.parent.parent / "src")

API_KEY = "test-a2a-auth-e2e-key-42"


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
                f"from nexus.cli import main; "
                f"main(['serve', '--host', '127.0.0.1', '--port', '{port}', "
                f"'--data-dir', '{tmp_path}', "
                f"'--api-key', '{API_KEY}'])"
            ),
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        preexec_fn=os.setsid if sys.platform != "win32" else None,
    )

    if not wait_for_server(base_url, timeout=30.0):
        process.terminate()
        stdout, stderr = process.communicate(timeout=5)
        pytest.fail(
            f"Auth server failed to start on port {port}.\n"
            f"stdout: {stdout.decode()}\n"
            f"stderr: {stderr.decode()}"
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
# Persistence (data_dir wiring)
# ======================================================================


class TestA2APersistenceE2E:
    """Verify tasks are persisted to disk via VFSTaskStore."""

    def test_task_creates_json_file_on_disk(self, auth_server):
        """Creating a task via A2A produces a .json file in data_dir."""
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

        # Check disk for the task file
        data_dir = auth_server["data_dir"]
        a2a_tasks_dir = data_dir / "a2a" / "tasks"
        assert a2a_tasks_dir.exists(), f"A2A tasks directory not found: {a2a_tasks_dir}"

        # Find the task JSON file
        all_json = list(a2a_tasks_dir.rglob(f"*_{task_id}.json"))
        assert len(all_json) == 1, f"Expected 1 file for task {task_id}, found {len(all_json)}"

        # Verify file content
        content = json.loads(all_json[0].read_bytes())
        assert content["task"]["id"] == task_id
        assert content["task"]["status"]["state"] == "submitted"

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

        # Verify canceled state is persisted on disk
        data_dir = auth_server["data_dir"]
        task_files = list((data_dir / "a2a" / "tasks").rglob(f"*_{task_id}.json"))
        assert len(task_files) == 1
        content = json.loads(task_files[0].read_bytes())
        assert content["task"]["status"]["state"] == "canceled"
