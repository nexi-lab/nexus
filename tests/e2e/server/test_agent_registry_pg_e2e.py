"""E2E tests for AgentRegistry with PostgreSQL and FastAPI server (Issue #1240).

Tests the full Agent OS Phase 1 stack:
1. AgentRegistry lifecycle against real PostgreSQL (not SQLite)
2. Optimistic locking with real concurrent PostgreSQL transactions
3. Heartbeat batch flush with PostgreSQL
4. Server startup with database auth and agent registration via RPC
5. Namespace visibility with agent permissions end-to-end

Requirements:
    - PostgreSQL running at postgresql://scorpio@localhost:5432/nexus_e2e_test
    - Start with: docker start scorpio-postgres

Run with:
    pytest tests/e2e/test_agent_registry_e2e.py -v --override-ini="addopts="
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import threading
import time
from contextlib import closing, suppress
from pathlib import Path

import httpx
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from nexus.core.agent_record import AgentState
from nexus.core.agent_registry import (
    AgentRegistry,
    InvalidTransitionError,
    StaleAgentError,
)
from nexus.storage.models import Base

# PostgreSQL connection for E2E tests
POSTGRES_URL = os.getenv(
    "NEXUS_E2E_DATABASE_URL",
    "postgresql://scorpio@localhost:5432/nexus_e2e_test",
)

_src_path = Path(__file__).parent.parent.parent / "src"
if str(_src_path) not in sys.path:
    sys.path.insert(0, str(_src_path))


def find_free_port() -> int:
    """Find a free port on localhost."""
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("", 0))
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        return s.getsockname()[1]


def wait_for_server(url: str, timeout: float = 30.0) -> bool:
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


# ---------------------------------------------------------------------------
# PostgreSQL fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def pg_engine():
    """Create PostgreSQL engine for E2E testing.

    Skips tests if PostgreSQL is not available.
    Creates all tables including the new agent_records table.
    """
    try:
        engine = create_engine(POSTGRES_URL, echo=False, pool_pre_ping=True)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as e:
        pytest.skip(f"PostgreSQL not available at {POSTGRES_URL}: {e}")

    # Create all tables (including AgentRecordModel)
    Base.metadata.create_all(engine)

    yield engine

    engine.dispose()


@pytest.fixture
def pg_session_factory(pg_engine):
    """Create a session factory for PostgreSQL."""
    return sessionmaker(bind=pg_engine, expire_on_commit=False)


@pytest.fixture
def pg_registry(pg_session_factory):
    """Create an AgentRegistry backed by PostgreSQL."""
    registry = AgentRegistry(session_factory=pg_session_factory, flush_interval=1)
    yield registry

    # Cleanup: remove test agents
    with pg_session_factory() as session:
        session.execute(text("DELETE FROM agent_records WHERE agent_id LIKE 'e2e-%'"))
        session.commit()


# ---------------------------------------------------------------------------
# 1. PostgreSQL table creation and schema validation
# ---------------------------------------------------------------------------


class TestPostgreSQLSchema:
    """Verify agent_records table is properly created in PostgreSQL."""

    def test_agent_records_table_exists(self, pg_engine):
        """agent_records table exists in PostgreSQL."""
        with pg_engine.connect() as conn:
            result = conn.execute(
                text(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_name = 'agent_records'"
                )
            )
            tables = [row[0] for row in result]
            assert "agent_records" in tables

    def test_indexes_exist(self, pg_engine):
        """All 3 composite indexes exist."""
        with pg_engine.connect() as conn:
            result = conn.execute(
                text("SELECT indexname FROM pg_indexes WHERE tablename = 'agent_records'")
            )
            indexes = {row[0] for row in result}

        assert "idx_agent_records_zone_state" in indexes
        assert "idx_agent_records_state_heartbeat" in indexes
        assert "idx_agent_records_owner" in indexes

    def test_columns_exist(self, pg_engine):
        """All expected columns exist with correct types."""
        with pg_engine.connect() as conn:
            result = conn.execute(
                text(
                    "SELECT column_name, data_type, is_nullable "
                    "FROM information_schema.columns "
                    "WHERE table_name = 'agent_records' "
                    "ORDER BY ordinal_position"
                )
            )
            columns = {row[0]: (row[1], row[2]) for row in result}

        assert "agent_id" in columns
        assert "owner_id" in columns
        assert "state" in columns
        assert "generation" in columns
        assert "last_heartbeat" in columns


# ---------------------------------------------------------------------------
# 2. AgentRegistry lifecycle with PostgreSQL
# ---------------------------------------------------------------------------


class TestAgentRegistryPostgreSQL:
    """Test AgentRegistry operations against real PostgreSQL."""

    def test_register_and_get(self, pg_registry):
        """Register an agent and retrieve it from PostgreSQL."""
        record = pg_registry.register(
            "e2e-agent-1", "alice", zone_id="default", name="E2E Test Agent"
        )
        assert record.agent_id == "e2e-agent-1"
        assert record.owner_id == "alice"
        assert record.state is AgentState.UNKNOWN
        assert record.generation == 0

        # Get should return the same record
        fetched = pg_registry.get("e2e-agent-1")
        assert fetched is not None
        assert fetched.agent_id == "e2e-agent-1"

    def test_full_lifecycle_postgres(self, pg_registry):
        """Full state machine lifecycle against PostgreSQL."""
        # Register
        r = pg_registry.register("e2e-lifecycle-1", "alice", zone_id="default")
        assert r.state is AgentState.UNKNOWN
        assert r.generation == 0

        # UNKNOWN -> CONNECTED (gen 0 -> 1)
        r = pg_registry.transition("e2e-lifecycle-1", AgentState.CONNECTED, expected_generation=0)
        assert r.state is AgentState.CONNECTED
        assert r.generation == 1

        # CONNECTED -> IDLE (gen stays 1)
        r = pg_registry.transition("e2e-lifecycle-1", AgentState.IDLE, expected_generation=1)
        assert r.state is AgentState.IDLE
        assert r.generation == 1

        # IDLE -> CONNECTED (gen 1 -> 2, new session)
        r = pg_registry.transition("e2e-lifecycle-1", AgentState.CONNECTED, expected_generation=1)
        assert r.state is AgentState.CONNECTED
        assert r.generation == 2

        # CONNECTED -> SUSPENDED (gen stays 2)
        r = pg_registry.transition("e2e-lifecycle-1", AgentState.SUSPENDED, expected_generation=2)
        assert r.state is AgentState.SUSPENDED
        assert r.generation == 2

        # SUSPENDED -> CONNECTED (gen 2 -> 3, reactivation)
        r = pg_registry.transition("e2e-lifecycle-1", AgentState.CONNECTED, expected_generation=2)
        assert r.state is AgentState.CONNECTED
        assert r.generation == 3

    def test_invalid_transition_postgres(self, pg_registry):
        """Invalid transitions raise InvalidTransitionError with PostgreSQL."""
        pg_registry.register("e2e-invalid-1", "alice")

        with pytest.raises(InvalidTransitionError):
            pg_registry.transition("e2e-invalid-1", AgentState.IDLE, expected_generation=0)

    def test_list_by_zone_postgres(self, pg_registry):
        """list_by_zone works with PostgreSQL indexes."""
        pg_registry.register("e2e-zone-a1", "alice", zone_id="e2e-zone")
        pg_registry.register("e2e-zone-a2", "bob", zone_id="e2e-zone")
        pg_registry.register("e2e-zone-a3", "charlie", zone_id="e2e-other-zone")

        agents = pg_registry.list_by_zone("e2e-zone")
        agent_ids = {a.agent_id for a in agents}
        assert "e2e-zone-a1" in agent_ids
        assert "e2e-zone-a2" in agent_ids
        assert "e2e-zone-a3" not in agent_ids

    def test_list_by_zone_with_state_filter_postgres(self, pg_registry):
        """list_by_zone with state filter uses the composite index."""
        pg_registry.register("e2e-state-a1", "alice", zone_id="e2e-filter-zone")
        pg_registry.register("e2e-state-a2", "bob", zone_id="e2e-filter-zone")
        pg_registry.transition("e2e-state-a1", AgentState.CONNECTED, expected_generation=0)

        connected = pg_registry.list_by_zone("e2e-filter-zone", state=AgentState.CONNECTED)
        assert len(connected) == 1
        assert connected[0].agent_id == "e2e-state-a1"

    def test_unregister_postgres(self, pg_registry):
        """Unregister removes the agent from PostgreSQL."""
        pg_registry.register("e2e-unreg-1", "alice")
        assert pg_registry.unregister("e2e-unreg-1") is True
        assert pg_registry.get("e2e-unreg-1") is None


# ---------------------------------------------------------------------------
# 3. Optimistic locking with real PostgreSQL concurrency
# ---------------------------------------------------------------------------


class TestOptimisticLockingPostgreSQL:
    """Test optimistic locking with real PostgreSQL transactions."""

    def test_stale_generation_raises_postgres(self, pg_registry):
        """Stale generation raises StaleAgentError with PostgreSQL."""
        pg_registry.register("e2e-stale-1", "alice")
        pg_registry.transition("e2e-stale-1", AgentState.CONNECTED, expected_generation=0)

        # Advance to gen 2
        pg_registry.transition("e2e-stale-1", AgentState.IDLE, expected_generation=1)
        pg_registry.transition("e2e-stale-1", AgentState.CONNECTED, expected_generation=1)

        # Try with stale gen=1 (actual is 2)
        with pytest.raises(StaleAgentError):
            pg_registry.transition("e2e-stale-1", AgentState.IDLE, expected_generation=1)

    def test_concurrent_transitions_postgres(self, pg_registry):
        """Concurrent state transitions with PostgreSQL row-level locking."""
        pg_registry.register("e2e-concurrent-1", "alice")
        pg_registry.transition("e2e-concurrent-1", AgentState.CONNECTED, expected_generation=0)
        pg_registry.transition("e2e-concurrent-1", AgentState.IDLE, expected_generation=1)

        # Two threads try to transition IDLE -> CONNECTED simultaneously
        results: list[str] = []
        errors: list[Exception] = []

        def transition_worker(name: str):
            try:
                pg_registry.transition(
                    "e2e-concurrent-1", AgentState.CONNECTED, expected_generation=1
                )
                results.append(f"{name}:success")
            except (StaleAgentError, InvalidTransitionError):
                # StaleAgentError: optimistic lock detected stale generation
                # InvalidTransitionError: state already changed (CONNECTED→CONNECTED is invalid)
                results.append(f"{name}:conflict")
            except Exception as e:
                errors.append(e)

        t1 = threading.Thread(target=transition_worker, args=("T1",))
        t2 = threading.Thread(target=transition_worker, args=("T2",))
        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)

        assert len(errors) == 0, f"Unexpected errors: {errors}"
        assert len(results) == 2

        # Exactly one should succeed, one should get a conflict error
        successes = [r for r in results if r.endswith(":success")]
        conflicts = [r for r in results if r.endswith(":conflict")]
        assert len(successes) == 1, f"Expected exactly 1 success, got: {results}"
        assert len(conflicts) == 1, f"Expected exactly 1 conflict, got: {results}"

        # Final state should be CONNECTED with gen=2
        final = pg_registry.get("e2e-concurrent-1")
        assert final is not None
        assert final.state is AgentState.CONNECTED
        assert final.generation == 2


# ---------------------------------------------------------------------------
# 4. Heartbeat batch flush with PostgreSQL
# ---------------------------------------------------------------------------


class TestHeartbeatPostgreSQL:
    """Test heartbeat buffer and batch flush with PostgreSQL."""

    def test_heartbeat_and_flush_postgres(self, pg_registry):
        """Heartbeat writes to buffer, flush persists to PostgreSQL."""
        pg_registry.register("e2e-heartbeat-1", "alice")
        pg_registry.transition("e2e-heartbeat-1", AgentState.CONNECTED, expected_generation=0)

        # Heartbeat should write to buffer
        pg_registry.heartbeat("e2e-heartbeat-1")
        assert "e2e-heartbeat-1" in pg_registry._heartbeat_buffer

        # Flush should persist to PostgreSQL
        flushed = pg_registry.flush_heartbeats()
        assert flushed >= 1
        assert len(pg_registry._heartbeat_buffer) == 0

        # Verify persisted in PostgreSQL
        record = pg_registry.get("e2e-heartbeat-1")
        assert record is not None
        assert record.last_heartbeat is not None

    def test_concurrent_heartbeats_postgres(self, pg_registry):
        """10 threads x 50 heartbeats with PostgreSQL — no data corruption."""
        pg_registry.register("e2e-concurrent-hb-1", "alice")
        pg_registry.transition("e2e-concurrent-hb-1", AgentState.CONNECTED, expected_generation=0)

        errors: list[Exception] = []

        def heartbeat_worker():
            for _ in range(50):
                try:
                    pg_registry.heartbeat("e2e-concurrent-hb-1")
                except Exception as e:
                    errors.append(e)

        threads = [threading.Thread(target=heartbeat_worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert len(errors) == 0, f"Heartbeat errors: {errors[:5]}"

        flushed = pg_registry.flush_heartbeats()
        assert flushed >= 1

        record = pg_registry.get("e2e-concurrent-hb-1")
        assert record is not None
        assert record.last_heartbeat is not None

    def test_detect_stale_agents_postgres(self, pg_registry):
        """Stale detection query uses PostgreSQL composite index."""
        pg_registry.register("e2e-stale-detect-1", "alice")
        pg_registry.transition("e2e-stale-detect-1", AgentState.CONNECTED, expected_generation=0)
        pg_registry.heartbeat("e2e-stale-detect-1")
        pg_registry.flush_heartbeats()

        # With threshold=0, agent should be stale
        stale = pg_registry.detect_stale(threshold_seconds=0)
        stale_ids = {a.agent_id for a in stale}
        assert "e2e-stale-detect-1" in stale_ids


# ---------------------------------------------------------------------------
# 5. Server E2E: database auth + agent registration + namespace visibility
# ---------------------------------------------------------------------------


class TestServerE2E:
    """E2E test with real FastAPI server, PostgreSQL, and database auth."""

    @pytest.fixture
    def nexus_server_pg(self, tmp_path, pg_engine):
        """Start nexus server with PostgreSQL and database auth."""
        storage_path = tmp_path / "storage"
        storage_path.mkdir(exist_ok=True)

        port = find_free_port()
        base_url = f"http://127.0.0.1:{port}"

        env = os.environ.copy()
        env["NEXUS_JWT_SECRET"] = "test-secret-key-for-e2e-12345"
        env["NEXUS_DATABASE_URL"] = POSTGRES_URL
        env["PYTHONPATH"] = str(_src_path)
        env["NEXUS_ENFORCE_PERMISSIONS"] = "true"

        # Start server with --init to create admin user + API key
        # Set cwd=tmp_path so .nexus-admin-env is saved there (not project root)
        process = subprocess.Popen(
            [
                sys.executable,
                "-c",
                (
                    "from nexus.cli import main; "
                    f"main(['serve', '--host', '127.0.0.1', '--port', '{port}', "
                    f"'--data-dir', '{tmp_path}', '--auth-type', 'database', "
                    f"'--init', '--reset', '--admin-user', 'e2e-admin'])"
                ),
            ],
            env=env,
            cwd=str(tmp_path),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            preexec_fn=os.setsid if sys.platform != "win32" else None,
        )

        if not wait_for_server(base_url, timeout=30.0):
            process.terminate()
            stdout, stderr = process.communicate(timeout=5)
            pytest.fail(
                f"Server failed to start on port {port}.\n"
                f"stdout: {stdout.decode()[:2000]}\n"
                f"stderr: {stderr.decode()[:2000]}"
            )

        # Read the admin API key from the .nexus-admin-env file
        # The file uses format: export NEXUS_API_KEY='sk-...'
        admin_env_file = tmp_path / ".nexus-admin-env"
        api_key = None
        if admin_env_file.exists():
            for line in admin_env_file.read_text().splitlines():
                if "NEXUS_API_KEY=" in line:
                    # Handle both NEXUS_API_KEY=xxx and export NEXUS_API_KEY='xxx'
                    value = line.split("NEXUS_API_KEY=", 1)[1].strip()
                    api_key = value.strip("'\"")
                    break

        yield {
            "port": port,
            "base_url": base_url,
            "process": process,
            "api_key": api_key,
        }

        # Cleanup: kill server
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

    def test_server_health(self, nexus_server_pg):
        """Server starts and responds to health check."""
        response = httpx.get(
            f"{nexus_server_pg['base_url']}/health",
            timeout=5.0,
            trust_env=False,
        )
        assert response.status_code == 200

    def test_admin_auth_works(self, nexus_server_pg):
        """Admin API key authenticates successfully."""
        api_key = nexus_server_pg["api_key"]
        if not api_key:
            pytest.skip("Admin API key not found in .nexus-admin-env")

        response = httpx.get(
            f"{nexus_server_pg['base_url']}/api/auth/whoami",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=5.0,
            trust_env=False,
        )
        assert response.status_code == 200
        data = response.json()
        assert data.get("authenticated") is True

    def test_register_agent_via_rpc(self, nexus_server_pg):
        """Register an agent via the /api/nfs/register_agent RPC endpoint."""
        api_key = nexus_server_pg["api_key"]
        if not api_key:
            pytest.skip("Admin API key not found")

        base_url = nexus_server_pg["base_url"]

        response = httpx.post(
            f"{base_url}/api/nfs/register_agent",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "jsonrpc": "2.0",
                "method": "register_agent",
                "params": {
                    "agent_id": "e2e-admin,E2ETestAgent",
                    "name": "E2E Test Agent",
                    "description": "Agent for E2E testing",
                    "generate_api_key": True,
                },
                "id": 1,
            },
            timeout=10.0,
            trust_env=False,
        )
        assert response.status_code == 200, f"register_agent failed: {response.text}"
        data = response.json()
        assert "error" not in data or data.get("error") is None, f"RPC error: {data.get('error')}"
        result = data.get("result", {})
        assert result.get("agent_id") == "e2e-admin,E2ETestAgent"

    def test_agent_api_key_auth(self, nexus_server_pg):
        """Agent registered with API key can authenticate."""
        api_key = nexus_server_pg["api_key"]
        if not api_key:
            pytest.skip("Admin API key not found")

        base_url = nexus_server_pg["base_url"]

        # Register agent with API key
        reg_response = httpx.post(
            f"{base_url}/api/nfs/register_agent",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "jsonrpc": "2.0",
                "method": "register_agent",
                "params": {
                    "agent_id": "e2e-admin,AuthTestAgent",
                    "name": "Auth Test Agent",
                    "generate_api_key": True,
                },
                "id": 1,
            },
            timeout=10.0,
            trust_env=False,
        )
        assert reg_response.status_code == 200
        reg_data = reg_response.json()
        agent_api_key = reg_data.get("result", {}).get("api_key")

        if not agent_api_key:
            pytest.skip("Agent API key not returned")

        # Authenticate with agent API key
        whoami_response = httpx.get(
            f"{base_url}/api/auth/whoami",
            headers={"Authorization": f"Bearer {agent_api_key}"},
            timeout=5.0,
            trust_env=False,
        )
        assert whoami_response.status_code == 200
        whoami = whoami_response.json()
        assert whoami.get("authenticated") is True
        assert whoami.get("subject_type") == "agent"

    def test_unauthenticated_request_rejected(self, nexus_server_pg):
        """Request without API key is rejected or returns unauthenticated."""
        base_url = nexus_server_pg["base_url"]

        # Try a protected RPC endpoint without auth
        response = httpx.post(
            f"{base_url}/api/nfs/list",
            json={
                "jsonrpc": "2.0",
                "method": "list",
                "params": {"path": "/workspace"},
                "id": 1,
            },
            timeout=5.0,
            trust_env=False,
        )
        # Should either get 401/403 or an RPC error indicating auth required
        if response.status_code == 200:
            data = response.json()
            assert data.get("error") is not None, "Expected auth error from protected endpoint"
        else:
            assert response.status_code in (401, 403)


# ---------------------------------------------------------------------------
# 6. Namespace visibility E2E with PostgreSQL
# ---------------------------------------------------------------------------


class TestNamespaceE2E:
    """Test namespace visibility with agent permissions against PostgreSQL."""

    def test_namespace_manager_with_postgres(self, pg_engine):
        """NamespaceManager works with PostgreSQL-backed ReBAC."""
        from nexus.rebac.namespace_manager import NamespaceManager
        from nexus.rebac.manager import EnhancedReBACManager

        rebac = EnhancedReBACManager(engine=pg_engine, cache_ttl_seconds=5, max_depth=10)
        tuple_ids: list[str] = []

        try:
            ns = NamespaceManager(
                rebac_manager=rebac,
                cache_maxsize=100,
                cache_ttl=60,
                revision_window=10,
            )

            # Grant a path to a user
            result1 = rebac.rebac_write(
                subject=("user", "e2e-ns-user"),
                relation="direct_viewer",
                object=("file", "/workspace/e2e-project/data.csv"),
                zone_id="default",
            )
            tuple_ids.append(result1.tuple_id)

            # Check visibility
            assert ns.is_visible(
                ("user", "e2e-ns-user"),
                "/workspace/e2e-project/data.csv",
            )
            assert not ns.is_visible(
                ("user", "e2e-ns-user"),
                "/workspace/other-project/secret.txt",
            )

            # Check grants_hash
            ns.get_mount_table(("user", "e2e-ns-user"))
            grants_hash = ns.get_grants_hash(("user", "e2e-ns-user"))
            assert grants_hash is not None
            assert len(grants_hash) == 16

            # Add another grant and verify hash changes
            result2 = rebac.rebac_write(
                subject=("user", "e2e-ns-user"),
                relation="direct_viewer",
                object=("file", "/workspace/e2e-project-2/readme.md"),
                zone_id="default",
            )
            tuple_ids.append(result2.tuple_id)
            ns.invalidate(("user", "e2e-ns-user"))
            ns.get_mount_table(("user", "e2e-ns-user"))
            new_hash = ns.get_grants_hash(("user", "e2e-ns-user"))
            assert new_hash != grants_hash

        finally:
            # Cleanup ReBAC tuples by tuple_id
            for tid in tuple_ids:
                with suppress(Exception):
                    rebac.rebac_delete(tid)
            rebac.close()


# ---------------------------------------------------------------------------
# 7. Alembic Migration E2E
# ---------------------------------------------------------------------------


class TestAlembicMigration:
    """Verify Alembic migration generates correct SQL for agent_records.

    Uses offline mode (--sql) to avoid lock conflicts with module-scoped pg_engine.
    Table creation is already verified by TestPostgreSQLSchema.
    """

    def test_alembic_upgrade_sql_contains_create_table(self):
        """alembic upgrade --sql generates CREATE TABLE agent_records."""
        alembic_dir = Path(__file__).parent.parent.parent / "alembic"
        env = os.environ.copy()
        env["NEXUS_DATABASE_URL"] = POSTGRES_URL
        env["PYTHONPATH"] = str(_src_path)

        # Generate SQL for the agent_records migration only
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "alembic",
                "upgrade",
                "add_memory_version_tracking:add_agent_records_table",
                "--sql",
            ],
            cwd=str(alembic_dir),
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, (
            f"alembic upgrade --sql failed:\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )

        sql = result.stdout
        assert "CREATE TABLE agent_records" in sql
        assert "agent_id" in sql
        assert "generation" in sql
        assert "last_heartbeat" in sql
        assert "idx_agent_records_zone_state" in sql
        assert "idx_agent_records_state_heartbeat" in sql
        assert "idx_agent_records_owner" in sql

    def test_alembic_downgrade_sql_contains_drop_table(self):
        """alembic downgrade --sql generates DROP TABLE agent_records."""
        alembic_dir = Path(__file__).parent.parent.parent / "alembic"
        env = os.environ.copy()
        env["NEXUS_DATABASE_URL"] = POSTGRES_URL
        env["PYTHONPATH"] = str(_src_path)

        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "alembic",
                "downgrade",
                "add_agent_records_table:add_memory_version_tracking",
                "--sql",
            ],
            cwd=str(alembic_dir),
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, (
            f"alembic downgrade --sql failed:\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )

        sql = result.stdout
        assert "DROP TABLE agent_records" in sql
        assert "DROP INDEX idx_agent_records_owner" in sql


# ---------------------------------------------------------------------------
# 8. RPC Endpoints E2E (Server with database auth)
# ---------------------------------------------------------------------------


class TestRPCEndpoints:
    """Test new agent RPC endpoints via HTTP against real server."""

    @pytest.fixture
    def nexus_server_pg(self, tmp_path, pg_engine):
        """Start nexus server with PostgreSQL and database auth."""
        storage_path = tmp_path / "storage"
        storage_path.mkdir(exist_ok=True)

        port = find_free_port()
        base_url = f"http://127.0.0.1:{port}"

        env = os.environ.copy()
        env["NEXUS_JWT_SECRET"] = "test-secret-key-for-e2e-12345"
        env["NEXUS_DATABASE_URL"] = POSTGRES_URL
        env["PYTHONPATH"] = str(_src_path)
        env["NEXUS_ENFORCE_PERMISSIONS"] = "true"

        process = subprocess.Popen(
            [
                sys.executable,
                "-c",
                (
                    "from nexus.cli import main; "
                    f"main(['serve', '--host', '127.0.0.1', '--port', '{port}', "
                    f"'--data-dir', '{tmp_path}', '--auth-type', 'database', "
                    f"'--init', '--reset', '--admin-user', 'e2e-rpc-admin'])"
                ),
            ],
            env=env,
            cwd=str(tmp_path),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            preexec_fn=os.setsid if sys.platform != "win32" else None,
        )

        if not wait_for_server(base_url, timeout=30.0):
            process.terminate()
            stdout, stderr = process.communicate(timeout=5)
            pytest.fail(
                f"Server failed to start on port {port}.\n"
                f"stdout: {stdout.decode()[:2000]}\n"
                f"stderr: {stderr.decode()[:2000]}"
            )

        admin_env_file = tmp_path / ".nexus-admin-env"
        api_key = None
        if admin_env_file.exists():
            for line in admin_env_file.read_text().splitlines():
                if "NEXUS_API_KEY=" in line:
                    value = line.split("NEXUS_API_KEY=", 1)[1].strip()
                    api_key = value.strip("'\"")
                    break

        yield {
            "port": port,
            "base_url": base_url,
            "process": process,
            "api_key": api_key,
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

    def _rpc_call(self, base_url: str, api_key: str, method: str, params: dict) -> dict:
        """Make an RPC call and return the response JSON."""
        response = httpx.post(
            f"{base_url}/api/nfs/{method}",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "jsonrpc": "2.0",
                "method": method,
                "params": params,
                "id": 1,
            },
            timeout=10.0,
            trust_env=False,
        )
        return response.json()

    def test_agent_transition_rpc(self, nexus_server_pg):
        """agent_transition RPC changes state and bumps generation."""
        api_key = nexus_server_pg["api_key"]
        if not api_key:
            pytest.skip("Admin API key not found")

        base_url = nexus_server_pg["base_url"]

        # Register agent
        reg = self._rpc_call(
            base_url,
            api_key,
            "register_agent",
            {
                "agent_id": "e2e-rpc-admin,TransitionAgent",
                "name": "Transition Test Agent",
                "generate_api_key": True,
            },
        )
        assert reg.get("error") is None, f"register_agent error: {reg.get('error')}"

        # Transition UNKNOWN -> CONNECTED (gen 0 -> 1)
        result = self._rpc_call(
            base_url,
            api_key,
            "agent_transition",
            {
                "agent_id": "e2e-rpc-admin,TransitionAgent",
                "target_state": "CONNECTED",
                "expected_generation": 0,
            },
        )
        assert result.get("error") is None, f"transition error: {result.get('error')}"
        data = result["result"]
        assert data["state"] == "CONNECTED"
        assert data["generation"] == 1

        # Transition CONNECTED -> IDLE (gen stays 1)
        result = self._rpc_call(
            base_url,
            api_key,
            "agent_transition",
            {
                "agent_id": "e2e-rpc-admin,TransitionAgent",
                "target_state": "IDLE",
                "expected_generation": 1,
            },
        )
        assert result.get("error") is None
        assert result["result"]["state"] == "IDLE"
        assert result["result"]["generation"] == 1

    def test_agent_heartbeat_rpc(self, nexus_server_pg):
        """agent_heartbeat RPC returns ok."""
        api_key = nexus_server_pg["api_key"]
        if not api_key:
            pytest.skip("Admin API key not found")

        base_url = nexus_server_pg["base_url"]

        # Register and connect
        self._rpc_call(
            base_url,
            api_key,
            "register_agent",
            {
                "agent_id": "e2e-rpc-admin,HeartbeatAgent",
                "name": "Heartbeat Test Agent",
            },
        )
        self._rpc_call(
            base_url,
            api_key,
            "agent_transition",
            {
                "agent_id": "e2e-rpc-admin,HeartbeatAgent",
                "target_state": "CONNECTED",
                "expected_generation": 0,
            },
        )

        # Heartbeat
        result = self._rpc_call(
            base_url,
            api_key,
            "agent_heartbeat",
            {
                "agent_id": "e2e-rpc-admin,HeartbeatAgent",
            },
        )
        assert result.get("error") is None, f"heartbeat error: {result.get('error')}"
        assert result["result"]["ok"] is True

    def test_agent_list_by_zone_rpc(self, nexus_server_pg):
        """agent_list_by_zone RPC returns agents in zone."""
        api_key = nexus_server_pg["api_key"]
        if not api_key:
            pytest.skip("Admin API key not found")

        base_url = nexus_server_pg["base_url"]

        # Register 2 agents (they go to "default" zone)
        self._rpc_call(
            base_url,
            api_key,
            "register_agent",
            {
                "agent_id": "e2e-rpc-admin,ZoneAgent1",
                "name": "Zone Agent 1",
            },
        )
        self._rpc_call(
            base_url,
            api_key,
            "register_agent",
            {
                "agent_id": "e2e-rpc-admin,ZoneAgent2",
                "name": "Zone Agent 2",
            },
        )

        # List by zone
        result = self._rpc_call(
            base_url,
            api_key,
            "agent_list_by_zone",
            {
                "zone_id": "default",
            },
        )
        assert result.get("error") is None, f"list error: {result.get('error')}"
        agents = result["result"]
        agent_ids = {a["agent_id"] for a in agents}
        assert "e2e-rpc-admin,ZoneAgent1" in agent_ids
        assert "e2e-rpc-admin,ZoneAgent2" in agent_ids

    def test_invalid_transition_returns_error(self, nexus_server_pg):
        """Invalid transition (UNKNOWN -> IDLE) returns RPC error."""
        api_key = nexus_server_pg["api_key"]
        if not api_key:
            pytest.skip("Admin API key not found")

        base_url = nexus_server_pg["base_url"]

        # Register agent (starts as UNKNOWN)
        self._rpc_call(
            base_url,
            api_key,
            "register_agent",
            {
                "agent_id": "e2e-rpc-admin,InvalidAgent",
                "name": "Invalid Transition Agent",
            },
        )

        # Try UNKNOWN -> IDLE (invalid)
        result = self._rpc_call(
            base_url,
            api_key,
            "agent_transition",
            {
                "agent_id": "e2e-rpc-admin,InvalidAgent",
                "target_state": "IDLE",
                "expected_generation": 0,
            },
        )
        # Should return an RPC error
        assert result.get("error") is not None, (
            f"Expected error for invalid transition, got: {result}"
        )

    def test_stale_generation_returns_error(self, nexus_server_pg):
        """Stale generation in transition returns RPC error."""
        api_key = nexus_server_pg["api_key"]
        if not api_key:
            pytest.skip("Admin API key not found")

        base_url = nexus_server_pg["base_url"]

        # Register and advance generation
        self._rpc_call(
            base_url,
            api_key,
            "register_agent",
            {
                "agent_id": "e2e-rpc-admin,StaleAgent",
                "name": "Stale Gen Agent",
            },
        )
        self._rpc_call(
            base_url,
            api_key,
            "agent_transition",
            {
                "agent_id": "e2e-rpc-admin,StaleAgent",
                "target_state": "CONNECTED",
                "expected_generation": 0,
            },
        )
        # Now at gen 1. Try with stale gen=0
        result = self._rpc_call(
            base_url,
            api_key,
            "agent_transition",
            {
                "agent_id": "e2e-rpc-admin,StaleAgent",
                "target_state": "IDLE",
                "expected_generation": 0,
            },
        )
        assert result.get("error") is not None, f"Expected stale generation error, got: {result}"


# ---------------------------------------------------------------------------
# 9. Dual-Write Bridge E2E
# ---------------------------------------------------------------------------


class TestDualWriteBridge:
    """Test that register_agent writes to both EntityRegistry and AgentRegistry."""

    def test_register_creates_in_both_registries(self, nexus_server_pg):
        """register_agent via RPC creates in both EntityRegistry and AgentRegistry."""
        api_key = nexus_server_pg["api_key"]
        if not api_key:
            pytest.skip("Admin API key not found")

        base_url = nexus_server_pg["base_url"]

        # Register agent
        reg = httpx.post(
            f"{base_url}/api/nfs/register_agent",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "jsonrpc": "2.0",
                "method": "register_agent",
                "params": {
                    "agent_id": "e2e-rpc-admin,DualWriteAgent",
                    "name": "Dual Write Agent",
                    "generate_api_key": True,
                },
                "id": 1,
            },
            timeout=10.0,
            trust_env=False,
        )
        assert reg.status_code == 200
        reg_data = reg.json()
        assert reg_data.get("error") is None, f"register error: {reg_data.get('error')}"
        assert reg_data["result"]["agent_id"] == "e2e-rpc-admin,DualWriteAgent"

        # Verify in AgentRegistry via agent_list_by_zone
        list_result = httpx.post(
            f"{base_url}/api/nfs/agent_list_by_zone",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "jsonrpc": "2.0",
                "method": "agent_list_by_zone",
                "params": {"zone_id": "default"},
                "id": 2,
            },
            timeout=10.0,
            trust_env=False,
        ).json()
        assert list_result.get("error") is None
        agent_ids = {a["agent_id"] for a in list_result["result"]}
        assert "e2e-rpc-admin,DualWriteAgent" in agent_ids

    def test_delete_removes_from_both_registries(self, nexus_server_pg):
        """delete_agent via RPC removes from both EntityRegistry and AgentRegistry."""
        api_key = nexus_server_pg["api_key"]
        if not api_key:
            pytest.skip("Admin API key not found")

        base_url = nexus_server_pg["base_url"]

        # Register first
        httpx.post(
            f"{base_url}/api/nfs/register_agent",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "jsonrpc": "2.0",
                "method": "register_agent",
                "params": {
                    "agent_id": "e2e-rpc-admin,DeleteAgent",
                    "name": "Delete Test Agent",
                },
                "id": 1,
            },
            timeout=10.0,
            trust_env=False,
        )

        # Delete
        del_result = httpx.post(
            f"{base_url}/api/nfs/delete_agent",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "jsonrpc": "2.0",
                "method": "delete_agent",
                "params": {
                    "agent_id": "e2e-rpc-admin,DeleteAgent",
                },
                "id": 2,
            },
            timeout=10.0,
            trust_env=False,
        )
        assert del_result.status_code == 200

        # Verify removed from AgentRegistry
        list_result = httpx.post(
            f"{base_url}/api/nfs/agent_list_by_zone",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "jsonrpc": "2.0",
                "method": "agent_list_by_zone",
                "params": {"zone_id": "default"},
                "id": 3,
            },
            timeout=10.0,
            trust_env=False,
        ).json()
        assert list_result.get("error") is None
        agent_ids = {a["agent_id"] for a in list_result["result"]}
        assert "e2e-rpc-admin,DeleteAgent" not in agent_ids

    # Reuse server fixture from TestRPCEndpoints
    @pytest.fixture
    def nexus_server_pg(self, tmp_path, pg_engine):
        """Start nexus server for dual-write bridge tests."""
        storage_path = tmp_path / "storage"
        storage_path.mkdir(exist_ok=True)

        port = find_free_port()
        base_url = f"http://127.0.0.1:{port}"

        env = os.environ.copy()
        env["NEXUS_JWT_SECRET"] = "test-secret-key-for-e2e-12345"
        env["NEXUS_DATABASE_URL"] = POSTGRES_URL
        env["PYTHONPATH"] = str(_src_path)
        env["NEXUS_ENFORCE_PERMISSIONS"] = "true"

        process = subprocess.Popen(
            [
                sys.executable,
                "-c",
                (
                    "from nexus.cli import main; "
                    f"main(['serve', '--host', '127.0.0.1', '--port', '{port}', "
                    f"'--data-dir', '{tmp_path}', '--auth-type', 'database', "
                    f"'--init', '--reset', '--admin-user', 'e2e-dual-admin'])"
                ),
            ],
            env=env,
            cwd=str(tmp_path),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            preexec_fn=os.setsid if sys.platform != "win32" else None,
        )

        if not wait_for_server(base_url, timeout=30.0):
            process.terminate()
            stdout, stderr = process.communicate(timeout=5)
            pytest.fail(
                f"Server failed to start on port {port}.\n"
                f"stdout: {stdout.decode()[:2000]}\n"
                f"stderr: {stderr.decode()[:2000]}"
            )

        admin_env_file = tmp_path / ".nexus-admin-env"
        api_key = None
        if admin_env_file.exists():
            for line in admin_env_file.read_text().splitlines():
                if "NEXUS_API_KEY=" in line:
                    value = line.split("NEXUS_API_KEY=", 1)[1].strip()
                    api_key = value.strip("'\"")
                    break

        yield {
            "port": port,
            "base_url": base_url,
            "process": process,
            "api_key": api_key,
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
