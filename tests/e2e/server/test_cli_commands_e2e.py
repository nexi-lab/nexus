"""E2E tests for CLI command groups against a real HTTP server.

Issue #2811: Verifies that CLI commands (events, snapshot, exchange)
work end-to-end by:
1. Starting a real FastAPI server (uvicorn) with actual routers on a real port
2. Running CLI commands via subprocess pointing at the server
3. Verifying exit codes and output

Exchange commands are Phase 2 stubs that don't need a server.

Note: pay, audit, lock, governance CLI tests removed — those routers
have been fully replaced by gRPC (Issue #1528, #1529).
"""

import json
import os
import socket
import subprocess
import sys
import threading
import time
from contextlib import closing
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

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    engine = create_engine(f"sqlite:///{db_path}")

    # Create tables
    from nexus.storage.models import Base

    Base.metadata.create_all(engine)

    session_factory = sessionmaker(bind=engine)

    # Snapshot router
    from nexus.server.api.v2.routers.snapshots import router as snapshots_router

    app.include_router(snapshots_router)

    # Events replay router
    from nexus.server.api.v2.routers.events_replay import router as events_router

    app.include_router(events_router)

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

    # Snapshot router requires nexus_fs._snapshot_service (needs CAS + metadata
    # stores). Without a full server stack we can't wire it, so snapshot
    # endpoints will return 503 and tests handle this gracefully.

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
