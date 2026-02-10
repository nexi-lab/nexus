"""E2E tests for task queue with authentication.

Tests the full task queue lifecycle through the actual HTTP API
with --auth-type database enabled.
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
import uuid
from contextlib import closing, suppress
from pathlib import Path

import httpx
import pytest

try:
    import _nexus_tasks  # noqa: F401

    HAS_NEXUS_TASKS = True
except ImportError:
    HAS_NEXUS_TASKS = False

pytestmark = [
    pytest.mark.skipif(
        not HAS_NEXUS_TASKS,
        reason="nexus_tasks Rust extension not available",
    ),
]

# Source path for PYTHONPATH
_src_path = Path(__file__).parent.parent.parent / "src"


def find_free_port() -> int:
    """Find a free port on localhost."""
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("", 0))
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        return s.getsockname()[1]


def wait_for_server(url: str, timeout: float = 60.0) -> bool:
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


def _rpc_call(
    client: httpx.Client, method: str, params: dict, token: str | None = None
) -> httpx.Response:
    """Make an RPC call to the server."""
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    response = client.post(
        f"/api/nfs/{method}",
        json={
            "jsonrpc": "2.0",
            "id": str(uuid.uuid4()),
            "params": params,
        },
        headers=headers,
    )
    return response


@pytest.fixture(scope="function")
def auth_server(tmp_path):
    """Start nexus serve with --auth-type database for auth testing.

    This fixture:
    1. Creates storage directory and database
    2. Finds a free port
    3. Starts `nexus serve --auth-type database` as subprocess
    4. Waits for server to be ready
    5. Creates an API key for testing
    6. Yields server info including API key
    7. Kills server process on cleanup
    """
    storage_path = tmp_path / "storage"
    storage_path.mkdir(exist_ok=True)

    db_path = tmp_path / f"test_db_{uuid.uuid4().hex[:8]}.db"
    port = find_free_port()
    base_url = f"http://127.0.0.1:{port}"

    env = os.environ.copy()
    env["NEXUS_JWT_SECRET"] = "test-secret-key-for-e2e-12345"
    env["NEXUS_DATABASE_URL"] = f"sqlite:///{db_path}"
    env["PYTHONPATH"] = str(_src_path)
    # Prevent CONDA_PREFIX conflict with VIRTUAL_ENV
    env.pop("CONDA_PREFIX", None)

    # Start nexus serve with auth
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                f"from nexus.cli import main; main(['serve', "
                f"'--host', '127.0.0.1', '--port', '{port}', "
                f"'--data-dir', '{tmp_path}', "
                f"'--auth-type', 'database'])"
            ),
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        preexec_fn=os.setsid if sys.platform != "win32" else None,
    )

    if not wait_for_server(base_url, timeout=60.0):
        process.terminate()
        stdout, stderr = process.communicate(timeout=5)
        pytest.fail(
            f"Server failed to start on port {port}.\n"
            f"stdout: {stdout.decode()}\n"
            f"stderr: {stderr.decode()}"
        )

    # Create an API key for testing
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from nexus.server.auth.database_key import DatabaseAPIKeyAuth
    from nexus.storage.models import Base

    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)

    with session_factory() as session:
        _key_id, raw_key = DatabaseAPIKeyAuth.create_key(
            session,
            user_id="test-user",
            name="E2E Test Key",
        )
        session.commit()

    yield {
        "port": port,
        "base_url": base_url,
        "process": process,
        "db_path": db_path,
        "storage_path": storage_path,
        "api_key": raw_key,
    }

    # Cleanup
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
def auth_client(auth_server):
    """Create httpx client for auth server."""
    with httpx.Client(
        base_url=auth_server["base_url"],
        timeout=30.0,
        trust_env=False,
    ) as client:
        yield {"client": client, "api_key": auth_server["api_key"]}


class TestTaskQueueAuth:
    """Test task queue operations require authentication."""

    def test_submit_task_unauthenticated_returns_401(self, auth_client):
        """Unauthenticated request should be rejected."""
        client = auth_client["client"]
        response = _rpc_call(client, "submit_task", {"task_type": "test.echo"})
        assert response.status_code == 401

    def test_submit_and_get_task_authenticated(self, auth_client):
        """Authenticated submit and get should work."""
        client = auth_client["client"]
        token = auth_client["api_key"]

        # Submit task
        response = _rpc_call(
            client,
            "submit_task",
            {"task_type": "test.echo", "params_json": '{"msg": "hello"}'},
            token=token,
        )
        assert response.status_code == 200
        data = response.json()
        assert "result" in data
        result = data["result"]
        assert "task_id" in result
        assert result["status"] == "pending"
        task_id = result["task_id"]

        # Get task
        response = _rpc_call(client, "get_task", {"task_id": task_id}, token=token)
        assert response.status_code == 200
        task = response.json()["result"]
        assert task["task_id"] == task_id
        assert task["task_type"] == "test.echo"
        assert task["status_name"] == "pending"

    def test_list_queue_tasks_authenticated(self, auth_client):
        """List tasks with authentication."""
        client = auth_client["client"]
        token = auth_client["api_key"]

        # Submit multiple tasks
        for i in range(3):
            _rpc_call(
                client,
                "submit_task",
                {"task_type": f"test.type_{i}", "params_json": "{}"},
                token=token,
            )

        # List all
        response = _rpc_call(client, "list_queue_tasks", {}, token=token)
        assert response.status_code == 200
        tasks = response.json()["result"]
        assert len(tasks) >= 3

    def test_cancel_task_authenticated(self, auth_client):
        """Cancel a task with authentication."""
        client = auth_client["client"]
        token = auth_client["api_key"]

        # Submit
        response = _rpc_call(
            client,
            "submit_task",
            {"task_type": "test.cancel_me"},
            token=token,
        )
        task_id = response.json()["result"]["task_id"]

        # Cancel
        response = _rpc_call(client, "cancel_task", {"task_id": task_id}, token=token)
        assert response.status_code == 200
        result = response.json()["result"]
        assert result["success"] is True

        # Verify cancelled
        response = _rpc_call(client, "get_task", {"task_id": task_id}, token=token)
        task = response.json()["result"]
        assert task["status"] == 5  # CANCELLED

    def test_get_task_stats_authenticated(self, auth_client):
        """Get task queue stats with authentication."""
        client = auth_client["client"]
        token = auth_client["api_key"]

        # Submit some tasks
        _rpc_call(client, "submit_task", {"task_type": "test.a"}, token=token)
        _rpc_call(client, "submit_task", {"task_type": "test.b"}, token=token)

        # Get stats
        response = _rpc_call(client, "get_task_stats", {}, token=token)
        assert response.status_code == 200
        stats = response.json()["result"]
        assert stats["pending"] >= 2
        assert "running" in stats
        assert "completed" in stats

    def test_get_nonexistent_task(self, auth_client):
        """Getting a nonexistent task returns null result."""
        client = auth_client["client"]
        token = auth_client["api_key"]

        response = _rpc_call(client, "get_task", {"task_id": 999999}, token=token)
        assert response.status_code == 200
        assert response.json()["result"] is None
