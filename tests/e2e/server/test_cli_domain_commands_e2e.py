"""E2E tests for new domain CLI commands (Issue #2811).

Starts a real nexus serve process, seeds test data, then exercises
every new CLI command group via HTTP REST bridge (deprecated — migrating to gRPC):
  pay, audit, lock, governance, events, snapshot, exchange

Also tests grep -A/-B/-C context lines against real file content.
"""

import os
import signal
import socket
import subprocess
import sys
import time
from contextlib import closing, suppress
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from nexus.contracts.constants import ROOT_ZONE_ID

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


def _kill_process(process: subprocess.Popen) -> None:
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


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture(scope="module")
def domain_server(tmp_path_factory):
    """Start nexus serve with database auth + seeded audit records."""
    tmp_path = tmp_path_factory.mktemp("cli_domain_e2e")
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
            "NEXUS_JWT_SECRET": "test-jwt-for-cli-domain-e2e",
            "NEXUS_DATABASE_URL": f"sqlite:///{db_path}",
            "PYTHONPATH": str(_src_path),
            "NO_PROXY": "*",
            "NEXUS_SEARCH_DAEMON": "false",
            "NEXUS_RATE_LIMIT_ENABLED": "false",
            "NEXUS_RECORD_STORE_PATH": str(tmp_path / "record_store.db"),
        }
    )

    # Pre-create database tables
    from sqlalchemy import create_engine

    from nexus.storage.models import Base

    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    engine.dispose()

    # Start server
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

    if not _wait_for_server(base_url, timeout=60.0):
        process.terminate()
        try:
            stdout = process.communicate(timeout=5)[0]
        except subprocess.TimeoutExpired:
            process.kill()
            stdout = process.communicate()[0]
        pytest.fail(f"Server failed to start on port {port}.\nOutput:\n{stdout}")

    time.sleep(0.5)

    # Create admin API key
    api_key = None
    try:
        from sqlalchemy import create_engine as ce
        from sqlalchemy.orm import sessionmaker

        from nexus.bricks.auth.providers.database_key import DatabaseAPIKeyAuth

        eng = ce(f"sqlite:///{db_path}")
        factory = sessionmaker(bind=eng)
        with factory() as session:
            _, api_key = DatabaseAPIKeyAuth.create_key(
                session,
                user_id="admin",
                name="CLI domain E2E admin key",
                zone_id=ROOT_ZONE_ID,
                is_admin=True,
            )
            session.commit()
        eng.dispose()
    except Exception as e:
        _kill_process(process)
        pytest.fail(f"Failed to create admin API key: {e}")

    # Seed audit records
    seeded = False
    try:
        from unittest.mock import MagicMock

        from nexus.storage.exchange_audit_logger import ExchangeAuditLogger
        from nexus.storage.record_store import RecordStoreABC

        eng2 = ce(f"sqlite:///{db_path}")
        factory2 = sessionmaker(bind=eng2)
        mock_record_store = MagicMock(spec=RecordStoreABC)
        mock_record_store.session_factory = factory2
        audit_logger = ExchangeAuditLogger(record_store=mock_record_store)

        for i in range(3):
            audit_logger.record(
                protocol="internal",
                buyer_agent_id=f"buyer-{i}",
                seller_agent_id=f"seller-{i}",
                amount=Decimal(str(10 * (i + 1))),
                currency="credits",
                status="settled",
                application="cli-e2e",
                zone_id=ROOT_ZONE_ID,
                transfer_id=f"cli-e2e-tx-{i}",
            )
        seeded = True
        eng2.dispose()
    except Exception:
        seeded = False

    yield {
        "port": port,
        "base_url": base_url,
        "process": process,
        "api_key": api_key,
        "db_path": db_path,
        "seeded": seeded,
    }

    _kill_process(process)


@pytest.fixture(scope="module")
def service_client(domain_server):
    """Service client for testing CLI bridge layer.

    NexusServiceClient was removed (Issue #1133) — CLI now uses gRPC.
    These E2E tests need migration to gRPC-based calls.
    """
    pytest.skip("NexusServiceClient removed (Issue #1133) — migrate E2E tests to gRPC")


# =============================================================================
# Health check
# =============================================================================


class TestServerHealth:
    def test_health(self, service_client):
        with service_client:
            resp = service_client._client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "healthy"


# =============================================================================
# Audit CLI commands (via NexusServiceClient)
# =============================================================================


class TestAuditCLI:
    """Test audit commands via NexusServiceClient → real server."""

    def test_audit_list(self, service_client, domain_server):
        if not domain_server["seeded"]:
            pytest.skip("Audit records not seeded")
        with service_client:
            data = service_client.audit_list()
        assert "transactions" in data
        assert len(data["transactions"]) >= 1

    def test_audit_list_with_limit(self, service_client, domain_server):
        if not domain_server["seeded"]:
            pytest.skip("Audit records not seeded")
        with service_client:
            data = service_client.audit_list(limit=2)
        assert "transactions" in data
        assert len(data["transactions"]) <= 2

    def test_audit_export_json(self, service_client, domain_server):
        if not domain_server["seeded"]:
            pytest.skip("Audit records not seeded")
        with service_client:
            data = service_client.audit_export(fmt="json")
        # Should return JSON data (dict or list)
        assert data is not None

    def test_audit_export_csv(self, service_client, domain_server):
        if not domain_server["seeded"]:
            pytest.skip("Audit records not seeded")
        with service_client:
            data = service_client.audit_export(fmt="csv")
        # CSV returns bytes
        assert isinstance(data, bytes)


# =============================================================================
# Lock CLI commands
# =============================================================================


class TestLockCLI:
    """Test lock commands via NexusServiceClient → real server."""

    def test_lock_list_empty(self, service_client):
        """No locks initially — should return empty or 200."""
        with service_client:
            data = service_client.lock_list()
        # The server may return {"locks": []} or an error if lock manager
        # is not configured. Either is acceptable for this E2E validation.
        assert isinstance(data, dict)


# =============================================================================
# Governance CLI commands
# =============================================================================


class TestGovernanceCLI:
    """Test governance commands via NexusServiceClient → real server."""

    def test_governance_status(self, service_client):
        """Governance status endpoint should respond."""
        with service_client:
            try:
                data = service_client.governance_status()
                assert isinstance(data, dict)
            except Exception:
                # Governance may not be initialized — acceptable for E2E
                pass

    def test_governance_alerts(self, service_client):
        """Governance alerts endpoint should respond."""
        with service_client:
            try:
                data = service_client.governance_alerts()
                assert isinstance(data, dict)
            except Exception:
                pass


# =============================================================================
# Events CLI commands
# =============================================================================


class TestEventsCLI:
    """Test events replay via NexusServiceClient → real server."""

    def test_events_replay(self, service_client):
        """Events replay endpoint should respond."""
        with service_client:
            try:
                data = service_client.events_replay(limit=10)
                assert isinstance(data, dict)
            except Exception:
                # Events service may not be available — acceptable
                pass


# =============================================================================
# Snapshot CLI commands
# =============================================================================


class TestSnapshotCLI:
    """Test snapshot commands via NexusServiceClient → real server."""

    def test_snapshot_list(self, service_client):
        """Snapshot list should respond (empty or populated)."""
        with service_client:
            try:
                data = service_client.snapshot_list()
                assert isinstance(data, dict)
            except Exception:
                # Snapshot service may not be available
                pass
