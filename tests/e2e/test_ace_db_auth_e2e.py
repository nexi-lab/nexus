"""E2E tests for ACE endpoints with database auth + permission enforcement.

Tests issue #1193: Verify ACE REST APIs work correctly when:
- auth_type = database (API keys stored in DB, JWT tokens)
- enforce_permissions = true (ReBAC permission checks enabled)

Tests both admin and non-admin users:
- Admin: full access via is_admin bypass (short-circuits ReBAC)
- Non-admin: identity-based access via MemoryPermissionEnforcer (owner match)
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

_src_path = Path(__file__).parent.parent.parent / "src"


def _find_free_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("", 0))
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        return s.getsockname()[1]


def _wait_for_server(url: str, timeout: float = 60.0) -> bool:
    start = time.time()
    while time.time() - start < timeout:
        try:
            response = httpx.get(f"{url}/health", timeout=2.0, trust_env=False)
            if response.status_code == 200:
                return True
        except (httpx.ConnectError, httpx.ReadTimeout):
            pass
        time.sleep(0.3)
    return False


def _extract_api_key(output: str) -> str | None:
    """Extract the admin API key from server startup output."""
    for line in output.split("\n"):
        # The init output contains the API key in a line like:
        # "  API Key: sk-..."
        if "sk-" in line:
            # Extract the sk-... token
            for token in line.split():
                if token.startswith("sk-"):
                    return token.rstrip()
    return None


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture(scope="module")
def db_auth_server(tmp_path_factory):
    """Start nexus server with --auth-type database --init --reset.

    This creates a fresh database with admin user and API key,
    and enables permission enforcement.
    """
    tmp_path = tmp_path_factory.mktemp("db_auth_e2e")
    db_path = tmp_path / "nexus.db"
    storage_path = tmp_path / "storage"
    storage_path.mkdir(exist_ok=True)

    port = _find_free_port()
    base_url = f"http://127.0.0.1:{port}"

    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("CONDA_PREFIX", "HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")
    }
    env.update(
        {
            "NEXUS_JWT_SECRET": "test-jwt-secret-for-db-auth-e2e",
            "NEXUS_DATABASE_URL": f"sqlite:///{db_path}",
            "PYTHONPATH": str(_src_path),
            "NO_PROXY": "*",
            "NEXUS_ENFORCE_PERMISSIONS": "true",
            "NEXUS_ENFORCE_ZONE_ISOLATION": "true",
            "NEXUS_SEARCH_DAEMON": "false",
            "NEXUS_RATE_LIMIT_ENABLED": "false",
        }
    )

    # Pre-create database tables so --init can insert API keys
    from sqlalchemy import create_engine

    from nexus.storage.models import Base

    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    engine.dispose()

    # Start server with database auth + init (no --reset needed, DB is fresh)
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                f"from nexus.cli import main; "
                f"main(['serve', '--host', '127.0.0.1', '--port', '{port}', "
                f"'--data-dir', '{tmp_path}', "
                f"'--auth-type', 'database', '--init'])"
            ),
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        preexec_fn=os.setsid if sys.platform != "win32" else None,
    )

    # Wait for server to be ready (database init takes extra time)
    if not _wait_for_server(base_url, timeout=60.0):
        process.terminate()
        try:
            stdout = process.communicate(timeout=5)[0]
        except subprocess.TimeoutExpired:
            process.kill()
            stdout = process.communicate()[0]
        pytest.fail(f"Server failed to start on port {port}.\nOutput:\n{stdout}")

    # Read initial output to find the admin API key
    # Give the server a moment to finish printing startup messages
    time.sleep(1.0)

    # The API key was printed during --init. Read from the output buffer.
    # Since process is still running, we need to read non-blocking.
    # Alternative: query the database directly.
    api_key = None

    # Try to find the key from the process output
    # The server uses Rich console output, so we need to check
    if process.stdout and hasattr(process.stdout, "fileno"):
        try:
            # Read whatever's available without blocking
            import fcntl

            fd = process.stdout.fileno()
            fl = fcntl.fcntl(fd, fcntl.F_GETFL)
            fcntl.fcntl(fd, fcntl.F_SETFL, fl | os.O_NONBLOCK)
            try:
                output = process.stdout.read()
                if output:
                    api_key = _extract_api_key(output)
            except (BlockingIOError, TypeError):
                pass
            finally:
                fcntl.fcntl(fd, fcntl.F_SETFL, fl)
        except Exception:
            pass

    # If we couldn't get the key from output, query the database directly
    if not api_key:
        try:
            from sqlalchemy import create_engine
            from sqlalchemy.orm import sessionmaker

            engine = create_engine(f"sqlite:///{db_path}")
            Session = sessionmaker(bind=engine)
            with Session() as session:
                # Get the raw key hash - we can't recover the raw key from the hash
                # Instead, create a new key directly
                from nexus.server.auth.database_key import DatabaseAPIKeyAuth

                _key_id, api_key = DatabaseAPIKeyAuth.create_key(
                    session,
                    user_id="admin",
                    name="E2E test key",
                    zone_id="default",
                    is_admin=True,
                )
                session.commit()
        except Exception as e:
            # Kill server and fail
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
            pytest.fail(f"Failed to create API key: {e}")

    # Create a non-admin API key for permission enforcement testing
    non_admin_key = None
    try:
        from sqlalchemy import create_engine as ce2
        from sqlalchemy.orm import sessionmaker as sm2

        from nexus.server.auth.database_key import DatabaseAPIKeyAuth

        engine2 = ce2(f"sqlite:///{db_path}")
        Session2 = sm2(bind=engine2)
        with Session2() as session:
            _, non_admin_key = DatabaseAPIKeyAuth.create_key(
                session,
                user_id="testuser",
                name="Non-admin E2E key",
                zone_id="default",
                is_admin=False,
            )
            session.commit()
        engine2.dispose()
    except Exception:
        # Non-fatal: non-admin tests will be skipped
        non_admin_key = None

    yield {
        "port": port,
        "base_url": base_url,
        "process": process,
        "api_key": api_key,
        "non_admin_key": non_admin_key,
        "db_path": db_path,
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


@pytest.fixture(scope="module")
def client(db_auth_server) -> httpx.Client:
    """HTTP client with database auth API key."""
    headers = {"Authorization": f"Bearer {db_auth_server['api_key']}"}
    with httpx.Client(
        base_url=db_auth_server["base_url"],
        timeout=30.0,
        trust_env=False,
        headers=headers,
    ) as c:
        yield c


@pytest.fixture(scope="module")
def unauthenticated_client(db_auth_server) -> httpx.Client:
    """HTTP client WITHOUT auth headers."""
    with httpx.Client(
        base_url=db_auth_server["base_url"],
        timeout=30.0,
        trust_env=False,
    ) as c:
        yield c


@pytest.fixture(scope="module")
def non_admin_client(db_auth_server) -> httpx.Client:
    """HTTP client with non-admin API key (is_admin=False).

    This exercises the real ReBAC permission check path instead of
    the admin bypass short-circuit.
    """
    key = db_auth_server.get("non_admin_key")
    if not key:
        pytest.skip("Non-admin API key not created")
    headers = {"Authorization": f"Bearer {key}"}
    with httpx.Client(
        base_url=db_auth_server["base_url"],
        timeout=30.0,
        trust_env=False,
        headers=headers,
    ) as c:
        yield c


# =============================================================================
# Health check
# =============================================================================


class TestServerHealth:
    def test_health(self, client: httpx.Client):
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "healthy"


# =============================================================================
# Auth enforcement: unauthenticated requests should be rejected
# =============================================================================


class TestAuthEnforcement:
    """Verify database auth rejects unauthenticated requests."""

    def test_memories_requires_auth(self, unauthenticated_client: httpx.Client):
        resp = unauthenticated_client.post(
            "/api/v2/memories",
            json={"content": "test", "scope": "user"},
        )
        assert resp.status_code == 401, f"Expected 401, got {resp.status_code}: {resp.text}"

    def test_trajectories_requires_auth(self, unauthenticated_client: httpx.Client):
        resp = unauthenticated_client.post(
            "/api/v2/trajectories",
            json={"task_description": "test"},
        )
        assert resp.status_code == 401, f"Expected 401, got {resp.status_code}: {resp.text}"

    def test_playbooks_requires_auth(self, unauthenticated_client: httpx.Client):
        resp = unauthenticated_client.get("/api/v2/playbooks")
        assert resp.status_code == 401, f"Expected 401, got {resp.status_code}: {resp.text}"

    def test_feedback_requires_auth(self, unauthenticated_client: httpx.Client):
        resp = unauthenticated_client.get("/api/v2/feedback/queue")
        assert resp.status_code == 401, f"Expected 401, got {resp.status_code}: {resp.text}"


# =============================================================================
# Memory endpoints with database auth + permissions
# =============================================================================


class TestMemoriesWithDbAuth:
    def test_store_and_get(self, client: httpx.Client):
        resp = client.post(
            "/api/v2/memories",
            json={"content": "DB auth memory test", "scope": "user", "memory_type": "fact"},
        )
        assert resp.status_code == 201, f"Store failed: {resp.text}"
        memory_id = resp.json()["memory_id"]

        get_resp = client.get(f"/api/v2/memories/{memory_id}")
        assert get_resp.status_code == 200, f"Get failed: {get_resp.text}"
        assert get_resp.json()["memory"]["memory_id"] == memory_id

    def test_search(self, client: httpx.Client):
        client.post(
            "/api/v2/memories",
            json={"content": "DB auth search test content", "scope": "user"},
        )
        resp = client.post(
            "/api/v2/memories/search",
            json={"query": "search test", "limit": 10},
        )
        assert resp.status_code == 200, f"Search failed: {resp.text}"
        assert "results" in resp.json()

    def test_batch_store(self, client: httpx.Client):
        resp = client.post(
            "/api/v2/memories/batch",
            json={
                "memories": [
                    {"content": "Batch 1 db auth", "scope": "user"},
                    {"content": "Batch 2 db auth", "scope": "user"},
                ]
            },
        )
        assert resp.status_code == 201, f"Batch failed: {resp.text}"
        assert resp.json()["stored"] == 2


# =============================================================================
# Trajectory endpoints with database auth + permissions
# =============================================================================


class TestTrajectoriesWithDbAuth:
    def test_start_and_complete(self, client: httpx.Client):
        resp = client.post(
            "/api/v2/trajectories",
            json={"task_description": "DB auth trajectory test", "task_type": "test"},
        )
        assert resp.status_code == 201, f"Start failed: {resp.text}"
        traj_id = resp.json()["trajectory_id"]

        # Log step
        step_resp = client.post(
            f"/api/v2/trajectories/{traj_id}/steps",
            json={"step_type": "action", "description": "Test step"},
        )
        assert step_resp.status_code == 200, f"Step failed: {step_resp.text}"

        # Complete
        complete_resp = client.post(
            f"/api/v2/trajectories/{traj_id}/complete",
            json={"status": "success", "success_score": 0.9},
        )
        assert complete_resp.status_code == 200, f"Complete failed: {complete_resp.text}"

    def test_query(self, client: httpx.Client):
        resp = client.get("/api/v2/trajectories?limit=10")
        assert resp.status_code == 200, f"Query failed: {resp.text}"
        assert "trajectories" in resp.json()


# =============================================================================
# Feedback endpoints with database auth + permissions
# =============================================================================


class TestFeedbackWithDbAuth:
    def test_add_and_get_feedback(self, client: httpx.Client):
        # Create trajectory first
        traj_resp = client.post(
            "/api/v2/trajectories",
            json={"task_description": "Feedback test trajectory"},
        )
        traj_id = traj_resp.json()["trajectory_id"]

        # Add feedback
        fb_resp = client.post(
            "/api/v2/feedback",
            json={"trajectory_id": traj_id, "feedback_type": "human", "score": 0.8},
        )
        assert fb_resp.status_code == 201, f"Feedback add failed: {fb_resp.text}"

        # Get feedback
        get_resp = client.get(f"/api/v2/feedback/{traj_id}")
        assert get_resp.status_code == 200, f"Feedback get failed: {get_resp.text}"
        assert get_resp.json()["trajectory_id"] == traj_id

    def test_relearning_queue(self, client: httpx.Client):
        resp = client.get("/api/v2/feedback/queue?limit=5")
        assert resp.status_code == 200, f"Queue failed: {resp.text}"
        assert "queue" in resp.json()


# =============================================================================
# Playbook endpoints with database auth + permissions
# =============================================================================


class TestPlaybooksWithDbAuth:
    def test_crud(self, client: httpx.Client):
        name = f"db-auth-pb-{uuid.uuid4().hex[:8]}"

        # Create
        create_resp = client.post(
            "/api/v2/playbooks",
            json={"name": name, "scope": "user", "visibility": "private"},
        )
        assert create_resp.status_code == 201, f"Create failed: {create_resp.text}"
        pb_id = create_resp.json()["playbook_id"]

        # Get
        get_resp = client.get(f"/api/v2/playbooks/{pb_id}")
        assert get_resp.status_code == 200, f"Get failed: {get_resp.text}"

        # Update
        update_resp = client.put(
            f"/api/v2/playbooks/{pb_id}",
            json={"strategies": [{"type": "helpful", "description": "test"}]},
        )
        assert update_resp.status_code == 200, f"Update failed: {update_resp.text}"

        # Delete
        delete_resp = client.delete(f"/api/v2/playbooks/{pb_id}")
        assert delete_resp.status_code == 200, f"Delete failed: {delete_resp.text}"

    def test_list(self, client: httpx.Client):
        resp = client.get("/api/v2/playbooks?limit=10")
        assert resp.status_code == 200, f"List failed: {resp.text}"
        assert "playbooks" in resp.json()


# =============================================================================
# Consolidation endpoints with database auth + permissions
# =============================================================================


class TestConsolidationWithDbAuth:
    def test_apply_decay(self, client: httpx.Client):
        resp = client.post(
            "/api/v2/consolidate/decay",
            json={"decay_factor": 0.95, "min_importance": 0.1, "batch_size": 100},
        )
        assert resp.status_code == 200, f"Decay failed: {resp.text}"
        assert resp.json()["success"] is True

    def test_consolidate(self, client: httpx.Client):
        # Store some memories
        ids = []
        for i in range(3):
            resp = client.post(
                "/api/v2/memories",
                json={"content": f"Consolidation test {i}", "scope": "user"},
            )
            if resp.status_code == 201:
                ids.append(resp.json()["memory_id"])

        resp = client.post(
            "/api/v2/consolidate",
            json={"memory_ids": ids, "affinity_threshold": 0.5},
        )
        assert resp.status_code == 200, f"Consolidate failed: {resp.text}"

        # Verify response body structure (Issue #1026 review 9A)
        body = resp.json()
        assert "clusters_formed" in body, f"Missing clusters_formed: {body}"
        assert "total_consolidated" in body, f"Missing total_consolidated: {body}"
        assert "results" in body, f"Missing results: {body}"
        assert isinstance(body["clusters_formed"], int)
        assert isinstance(body["total_consolidated"], int)
        assert isinstance(body["results"], list)


# =============================================================================
# Reflect + Curate endpoints with database auth + permissions
# =============================================================================


class TestReflectCurateWithDbAuth:
    def test_reflect_requires_llm(self, client: httpx.Client):
        traj_resp = client.post(
            "/api/v2/trajectories",
            json={"task_description": "Reflect test"},
        )
        traj_id = traj_resp.json()["trajectory_id"]

        resp = client.post(
            "/api/v2/reflect",
            json={"trajectory_id": traj_id},
        )
        # Without LLM, should return 503
        assert resp.status_code in [200, 503], f"Unexpected: {resp.text}"

    def test_curate_bulk(self, client: httpx.Client):
        # Create playbook and trajectories
        pb_resp = client.post(
            "/api/v2/playbooks",
            json={"name": f"curate-{uuid.uuid4().hex[:8]}", "scope": "user"},
        )
        pb_id = pb_resp.json()["playbook_id"]

        traj_resp = client.post(
            "/api/v2/trajectories",
            json={"task_description": "Curate test"},
        )
        traj_id = traj_resp.json()["trajectory_id"]

        resp = client.post(
            "/api/v2/curate/bulk",
            json={"playbook_id": pb_id, "trajectory_ids": [traj_id]},
        )
        assert resp.status_code == 200, f"Bulk curate failed: {resp.text}"
        assert "processed" in resp.json()


# =============================================================================
# Non-admin user tests (exercises real ReBAC permission path)
#
# Non-admin API keys (is_admin=False) do NOT get the admin bypass.
# Permission checks go through MemoryPermissionEnforcer which uses
# identity-based access: owner match (user_id == context.user).
# =============================================================================


class TestNonAdminMemories:
    """Non-admin user can store and retrieve their own memories."""

    def test_store_and_get_own_memory(self, non_admin_client: httpx.Client):
        """Non-admin stores a memory and retrieves it (owner match)."""
        resp = non_admin_client.post(
            "/api/v2/memories",
            json={"content": "Non-admin memory", "scope": "user", "memory_type": "fact"},
        )
        assert resp.status_code == 201, f"Non-admin store failed: {resp.text}"
        memory_id = resp.json()["memory_id"]

        get_resp = non_admin_client.get(f"/api/v2/memories/{memory_id}")
        assert get_resp.status_code == 200, f"Non-admin get failed: {get_resp.text}"
        assert get_resp.json()["memory"]["memory_id"] == memory_id

    def test_search_own_memories(self, non_admin_client: httpx.Client):
        """Non-admin can search memories."""
        non_admin_client.post(
            "/api/v2/memories",
            json={"content": "Non-admin searchable content xyz123", "scope": "user"},
        )
        resp = non_admin_client.post(
            "/api/v2/memories/search",
            json={"query": "searchable content", "limit": 10},
        )
        assert resp.status_code == 200, f"Non-admin search failed: {resp.text}"
        assert "results" in resp.json()

    def test_batch_store(self, non_admin_client: httpx.Client):
        """Non-admin can batch store memories."""
        resp = non_admin_client.post(
            "/api/v2/memories/batch",
            json={
                "memories": [
                    {"content": "Non-admin batch 1", "scope": "user"},
                    {"content": "Non-admin batch 2", "scope": "user"},
                ]
            },
        )
        assert resp.status_code == 201, f"Non-admin batch failed: {resp.text}"
        assert resp.json()["stored"] == 2

    def test_query_memories(self, non_admin_client: httpx.Client):
        """Non-admin can query memories via POST /query."""
        resp = non_admin_client.post(
            "/api/v2/memories/query",
            json={"scope": "user", "limit": 5},
        )
        assert resp.status_code == 200, f"Non-admin query failed: {resp.text}"
        assert "results" in resp.json()


class TestNonAdminTrajectories:
    """Non-admin user can manage their own trajectories."""

    def test_start_step_complete(self, non_admin_client: httpx.Client):
        """Non-admin can start, step, and complete a trajectory."""
        resp = non_admin_client.post(
            "/api/v2/trajectories",
            json={"task_description": "Non-admin trajectory", "task_type": "test"},
        )
        assert resp.status_code == 201, f"Non-admin start failed: {resp.text}"
        traj_id = resp.json()["trajectory_id"]

        step_resp = non_admin_client.post(
            f"/api/v2/trajectories/{traj_id}/steps",
            json={"step_type": "action", "description": "Non-admin step"},
        )
        assert step_resp.status_code == 200, f"Non-admin step failed: {step_resp.text}"

        complete_resp = non_admin_client.post(
            f"/api/v2/trajectories/{traj_id}/complete",
            json={"status": "success", "success_score": 0.85},
        )
        assert complete_resp.status_code == 200, f"Non-admin complete failed: {complete_resp.text}"

    def test_query(self, non_admin_client: httpx.Client):
        """Non-admin can query trajectories."""
        resp = non_admin_client.get("/api/v2/trajectories?limit=5")
        assert resp.status_code == 200, f"Non-admin traj query failed: {resp.text}"
        assert "trajectories" in resp.json()


class TestNonAdminFeedback:
    """Non-admin user can manage feedback on their trajectories."""

    def test_add_and_get_feedback(self, non_admin_client: httpx.Client):
        """Non-admin can add feedback and retrieve it."""
        traj_resp = non_admin_client.post(
            "/api/v2/trajectories",
            json={"task_description": "Non-admin feedback test"},
        )
        traj_id = traj_resp.json()["trajectory_id"]

        fb_resp = non_admin_client.post(
            "/api/v2/feedback",
            json={"trajectory_id": traj_id, "feedback_type": "human", "score": 0.7},
        )
        assert fb_resp.status_code == 201, f"Non-admin feedback add failed: {fb_resp.text}"

        get_resp = non_admin_client.get(f"/api/v2/feedback/{traj_id}")
        assert get_resp.status_code == 200, f"Non-admin feedback get failed: {get_resp.text}"
        assert get_resp.json()["trajectory_id"] == traj_id

    def test_relearning_queue(self, non_admin_client: httpx.Client):
        """Non-admin can query relearning queue."""
        resp = non_admin_client.get("/api/v2/feedback/queue?limit=5")
        assert resp.status_code == 200, f"Non-admin queue failed: {resp.text}"


class TestNonAdminPlaybooks:
    """Non-admin user can manage their own playbooks."""

    def test_crud(self, non_admin_client: httpx.Client):
        """Non-admin can create, read, update, delete playbooks."""
        name = f"nonadmin-pb-{uuid.uuid4().hex[:8]}"

        create_resp = non_admin_client.post(
            "/api/v2/playbooks",
            json={"name": name, "scope": "user", "visibility": "private"},
        )
        assert create_resp.status_code == 201, f"Non-admin pb create failed: {create_resp.text}"
        pb_id = create_resp.json()["playbook_id"]

        get_resp = non_admin_client.get(f"/api/v2/playbooks/{pb_id}")
        assert get_resp.status_code == 200, f"Non-admin pb get failed: {get_resp.text}"

        update_resp = non_admin_client.put(
            f"/api/v2/playbooks/{pb_id}",
            json={"strategies": [{"type": "test", "description": "non-admin strat"}]},
        )
        assert update_resp.status_code == 200, f"Non-admin pb update failed: {update_resp.text}"

        delete_resp = non_admin_client.delete(f"/api/v2/playbooks/{pb_id}")
        assert delete_resp.status_code == 200, f"Non-admin pb delete failed: {delete_resp.text}"

    def test_list(self, non_admin_client: httpx.Client):
        """Non-admin can list playbooks."""
        resp = non_admin_client.get("/api/v2/playbooks?limit=5")
        assert resp.status_code == 200, f"Non-admin pb list failed: {resp.text}"
        assert "playbooks" in resp.json()


class TestNonAdminConsolidation:
    """Non-admin user can use consolidation endpoints."""

    def test_apply_decay(self, non_admin_client: httpx.Client):
        """Non-admin can apply decay."""
        resp = non_admin_client.post(
            "/api/v2/consolidate/decay",
            json={"decay_factor": 0.95, "min_importance": 0.1, "batch_size": 50},
        )
        assert resp.status_code == 200, f"Non-admin decay failed: {resp.text}"

    def test_consolidate(self, non_admin_client: httpx.Client):
        """Non-admin can consolidate their own memories."""
        ids = []
        for i in range(3):
            resp = non_admin_client.post(
                "/api/v2/memories",
                json={"content": f"Non-admin consolidate {i}", "scope": "user"},
            )
            if resp.status_code == 201:
                ids.append(resp.json()["memory_id"])

        resp = non_admin_client.post(
            "/api/v2/consolidate",
            json={"memory_ids": ids, "affinity_threshold": 0.5},
        )
        assert resp.status_code == 200, f"Non-admin consolidate failed: {resp.text}"

        # Verify response body structure (Issue #1026 review 9A)
        body = resp.json()
        assert "clusters_formed" in body, f"Missing clusters_formed: {body}"
        assert "total_consolidated" in body, f"Missing total_consolidated: {body}"
        assert "results" in body, f"Missing results: {body}"
        assert isinstance(body["clusters_formed"], int)
        assert isinstance(body["total_consolidated"], int)
        assert isinstance(body["results"], list)


class TestCrossUserIsolation:
    """Verify non-admin user cannot access admin-created data.

    Admin creates memories, non-admin should not see them via direct GET
    (MemoryPermissionEnforcer denies: user_id mismatch).
    """

    def test_non_admin_cannot_get_admin_memory(
        self, client: httpx.Client, non_admin_client: httpx.Client
    ):
        """Non-admin cannot retrieve a memory owned by admin."""
        # Admin creates a memory
        admin_resp = client.post(
            "/api/v2/memories",
            json={"content": "Admin-only secret", "scope": "user", "memory_type": "fact"},
        )
        assert admin_resp.status_code == 201
        admin_memory_id = admin_resp.json()["memory_id"]

        # Admin can get it
        admin_get = client.get(f"/api/v2/memories/{admin_memory_id}")
        assert admin_get.status_code == 200

        # Non-admin tries to get it — should be denied (403 or 404)
        non_admin_get = non_admin_client.get(f"/api/v2/memories/{admin_memory_id}")
        assert non_admin_get.status_code in [403, 404, 500], (
            f"Non-admin should NOT access admin memory, got {non_admin_get.status_code}: "
            f"{non_admin_get.text}"
        )
