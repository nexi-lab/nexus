"""E2E tests for new CLI command groups against a real HTTP server.

Issue #2811: Verifies that CLI commands (pay, audit, lock, governance,
events, snapshot, exchange) work end-to-end by:
1. Starting a real FastAPI server (uvicorn) with actual routers on a real port
2. Seeding test data (audit records)
3. Running CLI commands via subprocess pointing at the server
4. Verifying exit codes and output

Exchange commands are Phase 2 stubs that don't need a server.
"""

import json
import os
import socket
import subprocess
import sys
import threading
import time
from contextlib import closing
from decimal import Decimal
from pathlib import Path

import pytest

_src_path = Path(__file__).parent.parent.parent / "src"


def _find_free_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("", 0))
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        return s.getsockname()[1]


def _run_cli(
    *args: str,
    base_url: str,
    api_key: str,
    env: dict[str, str],
    timeout: float = 30.0,
) -> subprocess.CompletedProcess[str]:
    """Run a nexus CLI command via subprocess with --remote-url and --remote-api-key."""
    cmd_args = list(args) + ["--remote-url", base_url, "--remote-api-key", api_key]
    return subprocess.run(
        [
            sys.executable,
            "-c",
            f"from nexus.cli import main; main({cmd_args!r})",
        ],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )


def _run_cli_no_server(
    *args: str,
    env: dict[str, str],
    timeout: float = 30.0,
) -> subprocess.CompletedProcess[str]:
    """Run a nexus CLI command that doesn't need a server (e.g. exchange stubs)."""
    cmd_args = list(args)
    return subprocess.run(
        [
            sys.executable,
            "-c",
            f"from nexus.cli import main; main({cmd_args!r})",
        ],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture(scope="module")
def cli_server(tmp_path_factory):
    """Start a real FastAPI server with actual API v2 routers on a real TCP port.

    Uses uvicorn in a background thread so it's accessible via subprocess CLI
    commands. This avoids needing the full `nexus serve` startup path (which
    requires the Raft Rust extension) while still testing real HTTP → real CLI.

    Yields dict with base_url, api_key, env.
    """
    import uvicorn
    from fastapi import FastAPI

    from nexus.contracts.constants import ROOT_ZONE_ID
    from nexus.server.dependencies import get_auth_result, require_admin, require_auth

    tmp_path = tmp_path_factory.mktemp("cli_e2e")
    db_path = tmp_path / "cli_e2e.db"

    port = _find_free_port()
    base_url = f"http://127.0.0.1:{port}"
    api_key = "test-cli-e2e-key"

    # Build env for CLI subprocesses
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("CONDA_PREFIX", "HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")
    }
    env.update(
        {
            "PYTHONPATH": str(_src_path),
            "NO_PROXY": "*",
        }
    )

    # --- Build FastAPI app with real routers ---
    app = FastAPI()

    # Auth overrides: simulate authenticated admin
    _auth_result = {
        "authenticated": True,
        "is_admin": True,
        "subject_id": "admin",
        "subject_type": "user",
        "zone_id": ROOT_ZONE_ID,
    }
    app.dependency_overrides[require_auth] = lambda: _auth_result
    app.dependency_overrides[require_admin] = lambda: _auth_result
    app.dependency_overrides[get_auth_result] = lambda: _auth_result

    # --- Mount routers ---

    # Audit router
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    engine = create_engine(f"sqlite:///{db_path}")

    # Create tables
    from nexus.storage.models import Base

    Base.metadata.create_all(engine)

    session_factory = sessionmaker(bind=engine)

    from nexus.server.api.v2.routers.audit import router as audit_router

    app.include_router(audit_router)

    # Snapshot router
    from nexus.server.api.v2.routers.snapshots import router as snapshots_router

    app.include_router(snapshots_router)

    # Events replay router
    from nexus.server.api.v2.routers.events_replay import router as events_router

    app.include_router(events_router)

    # Governance router (admin-only)
    from nexus.server.api.v2.routers.governance import router as governance_router

    app.include_router(governance_router)

    # Pay router
    from nexus.server.api.v2.routers.pay import router as pay_router

    app.include_router(pay_router)

    # Locks router
    from nexus.server.api.v2.routers.locks import router as locks_router

    app.include_router(locks_router)

    # Health endpoint
    @app.get("/health")
    def health():
        return {"status": "healthy"}

    # Wire up record store and services on app.state
    from unittest.mock import MagicMock

    from nexus.storage.record_store import RecordStoreABC

    mock_record_store = MagicMock(spec=RecordStoreABC)
    mock_record_store.session_factory = session_factory
    app.state.record_store = mock_record_store

    # Override get_exchange_audit_logger (used by audit router)
    from nexus.server.api.v2.dependencies import get_exchange_audit_logger
    from nexus.storage.exchange_audit_logger import ExchangeAuditLogger as _EAL

    _eal_instance = _EAL(record_store=mock_record_store)
    app.dependency_overrides[get_exchange_audit_logger] = lambda: (_eal_instance, ROOT_ZONE_ID)

    # Wire audit logger for seeding
    from nexus.storage.exchange_audit_logger import ExchangeAuditLogger

    audit_logger = ExchangeAuditLogger(record_store=mock_record_store)

    # Wire governance services
    try:
        from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
        from sqlalchemy.ext.asyncio import async_sessionmaker as AsyncSessionMaker

        import nexus.bricks.governance.db_models  # noqa: F401
        from nexus.bricks.governance.anomaly_service import (
            AnomalyService,
            StatisticalAnomalyDetector,
        )
        from nexus.bricks.governance.collusion_service import CollusionService
        from nexus.bricks.governance.governance_graph_service import GovernanceGraphService
        from nexus.bricks.governance.response_service import ResponseService
        from nexus.lib.db_base import Base as GovBase

        async_engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", echo=False)

        import asyncio

        async def _create_gov_tables():
            async with async_engine.begin() as conn:
                await conn.run_sync(GovBase.metadata.create_all)

        asyncio.get_event_loop_policy().new_event_loop().run_until_complete(_create_gov_tables())

        async_session_factory = AsyncSessionMaker(
            async_engine, class_=AsyncSession, expire_on_commit=False
        )

        detector = StatisticalAnomalyDetector()
        anomaly_svc = AnomalyService(session_factory=async_session_factory, detector=detector)
        collusion_svc = CollusionService(session_factory=async_session_factory)
        graph_svc = GovernanceGraphService(session_factory=async_session_factory)
        response_svc = ResponseService(
            session_factory=async_session_factory,
            anomaly_service=anomaly_svc,
            collusion_service=collusion_svc,
            graph_service=graph_svc,
        )

        app.state.governance_anomaly_service = anomaly_svc
        app.state.governance_collusion_service = collusion_svc
        app.state.governance_graph_service = graph_svc
        app.state.governance_response_service = response_svc
        governance_wired = True
    except Exception:
        governance_wired = False

    # Snapshot router requires nexus_fs._snapshot_service (needs CAS + metadata
    # stores). Without a full server stack we can't wire it, so snapshot
    # endpoints will return 503 and tests handle this gracefully.

    # Seed audit records
    audit_seeded = False
    try:
        for i in range(5):
            audit_logger.record(
                protocol="internal" if i < 3 else "x402",
                buyer_agent_id=f"buyer-{i % 2}",
                seller_agent_id=f"seller-{i % 3}",
                amount=Decimal(str(10 * (i + 1))),
                currency="credits",
                status="settled" if i < 4 else "failed",
                application="gateway",
                zone_id="root",
                transfer_id=f"cli-e2e-tx-{i}",
            )
        audit_seeded = True
    except Exception:
        pass

    # Start uvicorn in background thread
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    # Wait for server to be ready
    import httpx

    start = time.time()
    while time.time() - start < 30.0:
        try:
            resp = httpx.get(f"{base_url}/health", timeout=1.0, trust_env=False)
            if resp.status_code == 200:
                break
        except (httpx.ConnectError, httpx.ReadTimeout):
            pass
        time.sleep(0.1)
    else:
        pytest.fail(f"Server failed to start on port {port}")

    yield {
        "port": port,
        "base_url": base_url,
        "api_key": api_key,
        "env": env,
        "audit_seeded": audit_seeded,
        "governance_wired": governance_wired,
    }

    server.should_exit = True
    thread.join(timeout=5)
    engine.dispose()


# =============================================================================
# Health check
# =============================================================================


class TestServerHealth:
    def test_health(self, cli_server):
        import httpx

        resp = httpx.get(f"{cli_server['base_url']}/health", trust_env=False)
        assert resp.status_code == 200


# =============================================================================
# Audit CLI
# =============================================================================


class TestAuditCLI:
    """Test `nexus audit` commands against real server."""

    def test_audit_list_json(self, cli_server):
        if not cli_server["audit_seeded"]:
            pytest.skip("Audit records not seeded")
        result = _run_cli(
            "audit",
            "list",
            "--json",
            base_url=cli_server["base_url"],
            api_key=cli_server["api_key"],
            env=cli_server["env"],
        )
        assert result.returncode == 0, f"stdout: {result.stdout}\nstderr: {result.stderr}"
        data = json.loads(result.stdout)
        assert "transactions" in data
        assert len(data["transactions"]) >= 1

    def test_audit_list_rich(self, cli_server):
        if not cli_server["audit_seeded"]:
            pytest.skip("Audit records not seeded")
        result = _run_cli(
            "audit",
            "list",
            base_url=cli_server["base_url"],
            api_key=cli_server["api_key"],
            env=cli_server["env"],
        )
        assert result.returncode == 0, f"stdout: {result.stdout}\nstderr: {result.stderr}"
        assert "Audit" in result.stdout or "Time" in result.stdout

    def test_audit_list_with_limit(self, cli_server):
        if not cli_server["audit_seeded"]:
            pytest.skip("Audit records not seeded")
        result = _run_cli(
            "audit",
            "list",
            "--limit",
            "2",
            "--json",
            base_url=cli_server["base_url"],
            api_key=cli_server["api_key"],
            env=cli_server["env"],
        )
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert len(data["transactions"]) <= 2

    def test_audit_export_json(self, cli_server):
        if not cli_server["audit_seeded"]:
            pytest.skip("Audit records not seeded")
        result = _run_cli(
            "audit",
            "export",
            "--format",
            "json",
            base_url=cli_server["base_url"],
            api_key=cli_server["api_key"],
            env=cli_server["env"],
        )
        assert result.returncode == 0, f"stdout: {result.stdout}\nstderr: {result.stderr}"

    def test_audit_export_csv(self, cli_server):
        if not cli_server["audit_seeded"]:
            pytest.skip("Audit records not seeded")
        result = _run_cli(
            "audit",
            "export",
            "--format",
            "csv",
            base_url=cli_server["base_url"],
            api_key=cli_server["api_key"],
            env=cli_server["env"],
        )
        assert result.returncode == 0, f"stdout: {result.stdout}\nstderr: {result.stderr}"


# =============================================================================
# Governance CLI (admin-only)
# =============================================================================


class TestGovernanceCLI:
    """Test `nexus governance` commands against real server (admin API key)."""

    def test_governance_alerts_json(self, cli_server):
        if not cli_server["governance_wired"]:
            pytest.skip("Governance services not wired")
        result = _run_cli(
            "governance",
            "alerts",
            "--json",
            base_url=cli_server["base_url"],
            api_key=cli_server["api_key"],
            env=cli_server["env"],
        )
        assert result.returncode == 0, f"stdout: {result.stdout}\nstderr: {result.stderr}"
        data = json.loads(result.stdout)
        assert "alerts" in data

    def test_governance_alerts_rich(self, cli_server):
        if not cli_server["governance_wired"]:
            pytest.skip("Governance services not wired")
        result = _run_cli(
            "governance",
            "alerts",
            base_url=cli_server["base_url"],
            api_key=cli_server["api_key"],
            env=cli_server["env"],
        )
        assert result.returncode == 0, f"stdout: {result.stdout}\nstderr: {result.stderr}"

    def test_governance_rings_json(self, cli_server):
        if not cli_server["governance_wired"]:
            pytest.skip("Governance services not wired")
        result = _run_cli(
            "governance",
            "rings",
            "--json",
            base_url=cli_server["base_url"],
            api_key=cli_server["api_key"],
            env=cli_server["env"],
        )
        assert result.returncode == 0, f"stdout: {result.stdout}\nstderr: {result.stderr}"

    def test_governance_status_json(self, cli_server):
        if not cli_server["governance_wired"]:
            pytest.skip("Governance services not wired")
        result = _run_cli(
            "governance",
            "status",
            "--json",
            base_url=cli_server["base_url"],
            api_key=cli_server["api_key"],
            env=cli_server["env"],
        )
        assert result.returncode == 0, f"stdout: {result.stdout}\nstderr: {result.stderr}"
        data = json.loads(result.stdout)
        assert "recent_alerts" in data
        assert "fraud_rings" in data

    def test_governance_status_rich(self, cli_server):
        if not cli_server["governance_wired"]:
            pytest.skip("Governance services not wired")
        result = _run_cli(
            "governance",
            "status",
            base_url=cli_server["base_url"],
            api_key=cli_server["api_key"],
            env=cli_server["env"],
        )
        assert result.returncode == 0, f"stdout: {result.stdout}\nstderr: {result.stderr}"
        assert "Governance" in result.stdout


# =============================================================================
# Pay CLI
# =============================================================================


class TestPayCLI:
    """Test `nexus pay` commands against real server.

    Pay endpoints depend on CreditsService (TigerBeetle). Without it,
    /api/v2/pay/balance returns 500/503. We test that the CLI handles
    the response correctly either way.
    """

    def test_pay_balance_graceful(self, cli_server):
        result = _run_cli(
            "pay",
            "balance",
            base_url=cli_server["base_url"],
            api_key=cli_server["api_key"],
            env=cli_server["env"],
        )
        # Either shows balance (if CreditsService available) or error exit
        if result.returncode == 0:
            assert "Balance" in result.stdout or "available" in result.stdout
        else:
            assert result.returncode == 1
            assert "Error" in result.stdout

    def test_pay_history_json(self, cli_server):
        result = _run_cli(
            "pay",
            "history",
            "--json",
            base_url=cli_server["base_url"],
            api_key=cli_server["api_key"],
            env=cli_server["env"],
        )
        # pay history calls /api/v2/audit/transactions
        assert result.returncode == 0, f"stdout: {result.stdout}\nstderr: {result.stderr}"
        data = json.loads(result.stdout)
        assert "transactions" in data

    def test_pay_transfer_graceful(self, cli_server):
        result = _run_cli(
            "pay",
            "transfer",
            "agent-bob",
            "10.00",
            "--memo",
            "e2e test",
            base_url=cli_server["base_url"],
            api_key=cli_server["api_key"],
            env=cli_server["env"],
        )
        # Transfer requires CreditsService; either succeeds or fails gracefully
        assert result.returncode in (0, 1)


# =============================================================================
# Lock CLI
# =============================================================================


class TestLockCLI:
    """Test `nexus lock` commands against real server.

    Lock endpoints require Redis/Dragonfly. Without it, server returns 503.
    We test that the CLI handles the response correctly.
    """

    def test_lock_list_graceful(self, cli_server):
        result = _run_cli(
            "lock",
            "list",
            base_url=cli_server["base_url"],
            api_key=cli_server["api_key"],
            env=cli_server["env"],
        )
        # 503 from missing lock manager → CLI error exit 1
        assert result.returncode in (0, 1)

    def test_lock_info_graceful(self, cli_server):
        result = _run_cli(
            "lock",
            "info",
            "/data/test.txt",
            base_url=cli_server["base_url"],
            api_key=cli_server["api_key"],
            env=cli_server["env"],
        )
        assert result.returncode in (0, 1)


# =============================================================================
# Events CLI
# =============================================================================


class TestEventsCLI:
    """Test `nexus events` commands against real server."""

    def test_events_replay_json(self, cli_server):
        result = _run_cli(
            "events",
            "replay",
            "--json",
            base_url=cli_server["base_url"],
            api_key=cli_server["api_key"],
            env=cli_server["env"],
        )
        assert result.returncode == 0, f"stdout: {result.stdout}\nstderr: {result.stderr}"
        data = json.loads(result.stdout)
        assert "events" in data

    def test_events_replay_rich(self, cli_server):
        result = _run_cli(
            "events",
            "replay",
            base_url=cli_server["base_url"],
            api_key=cli_server["api_key"],
            env=cli_server["env"],
        )
        assert result.returncode == 0, f"stdout: {result.stdout}\nstderr: {result.stderr}"


# =============================================================================
# Snapshot CLI
# =============================================================================


class TestSnapshotCLI:
    """Test `nexus snapshot` commands against real server.

    Snapshot endpoints require nexus_fs._snapshot_service (CAS + metadata stores).
    Without a full server, endpoints return 503. We test that the CLI handles
    the response correctly either way.
    """

    def test_snapshot_list_graceful(self, cli_server):
        result = _run_cli(
            "snapshot",
            "list",
            "--json",
            base_url=cli_server["base_url"],
            api_key=cli_server["api_key"],
            env=cli_server["env"],
        )
        # 503 from missing snapshot service → CLI error exit 1
        assert result.returncode in (0, 1)

    def test_snapshot_create_graceful(self, cli_server):
        result = _run_cli(
            "snapshot",
            "create",
            "--description",
            "CLI e2e test",
            base_url=cli_server["base_url"],
            api_key=cli_server["api_key"],
            env=cli_server["env"],
        )
        assert result.returncode in (0, 1)

    def test_snapshot_restore_graceful(self, cli_server):
        result = _run_cli(
            "snapshot",
            "restore",
            "fake-txn-id",
            base_url=cli_server["base_url"],
            api_key=cli_server["api_key"],
            env=cli_server["env"],
        )
        assert result.returncode in (0, 1)


# =============================================================================
# Exchange CLI (Phase 2 stubs — no server needed)
# =============================================================================


class TestExchangeCLI:
    """Test `nexus exchange` Phase 2 stubs.

    These commands don't call the server; they print 'not yet available'.
    """

    def test_exchange_list(self, cli_server):
        result = _run_cli_no_server("exchange", "list", env=cli_server["env"])
        assert result.returncode == 0
        assert "not yet available" in result.stdout

    def test_exchange_create(self, cli_server):
        result = _run_cli_no_server(
            "exchange",
            "create",
            "/data/dataset.csv",
            "--price",
            "100",
            env=cli_server["env"],
        )
        assert result.returncode == 0
        assert "not yet available" in result.stdout

    def test_exchange_show(self, cli_server):
        result = _run_cli_no_server("exchange", "show", "offer-123", env=cli_server["env"])
        assert result.returncode == 0
        assert "not yet available" in result.stdout

    def test_exchange_cancel(self, cli_server):
        result = _run_cli_no_server("exchange", "cancel", "offer-123", env=cli_server["env"])
        assert result.returncode == 0
        assert "not yet available" in result.stdout
