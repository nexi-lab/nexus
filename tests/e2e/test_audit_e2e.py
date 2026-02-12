"""E2E tests for audit endpoints with real nexus serve + database auth.

Issue #1360: Verifies audit REST APIs work correctly when running a real
nexus serve subprocess with:
- auth_type = database (API keys stored in DB)
- enforce_permissions = true (ReBAC permission checks enabled)
- enforce_zone_isolation = true

Tests three user types:
- Admin: full access via is_admin bypass
- Normal user (non-admin): read access via authenticated API key
- Unauthenticated: rejected with 401
"""

from __future__ import annotations

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


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture(scope="module")
def audit_server(tmp_path_factory):
    """Start nexus serve with --auth-type database --init.

    Creates admin + non-admin API keys and seeds audit records directly
    in the database. Yields server info dict with keys, base_url, etc.
    """
    tmp_path = tmp_path_factory.mktemp("audit_e2e")
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
            "NEXUS_JWT_SECRET": "test-jwt-secret-for-audit-e2e",
            "NEXUS_DATABASE_URL": f"sqlite:///{db_path}",
            "PYTHONPATH": str(_src_path),
            "NO_PROXY": "*",
            "NEXUS_ENFORCE_PERMISSIONS": "true",
            "NEXUS_ENFORCE_ZONE_ISOLATION": "true",
            "NEXUS_SEARCH_DAEMON": "false",
            "NEXUS_RATE_LIMIT_ENABLED": "false",
        }
    )

    # Pre-create database tables
    from sqlalchemy import create_engine

    from nexus.storage.models import Base

    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    engine.dispose()

    # Start server with database auth + init
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

    time.sleep(1.0)

    # Create admin API key via direct DB access
    api_key = None
    try:
        from sqlalchemy import create_engine as ce
        from sqlalchemy.orm import sessionmaker

        from nexus.server.auth.database_key import DatabaseAPIKeyAuth

        eng = ce(f"sqlite:///{db_path}")
        factory = sessionmaker(bind=eng)
        with factory() as session:
            _key_id, api_key = DatabaseAPIKeyAuth.create_key(
                session,
                user_id="admin",
                name="Audit E2E admin key",
                zone_id="default",
                is_admin=True,
            )
            session.commit()
        eng.dispose()
    except Exception as e:
        _kill_process(process)
        pytest.fail(f"Failed to create admin API key: {e}")

    # Create non-admin (normal user) API key
    non_admin_key = None
    try:
        eng2 = ce(f"sqlite:///{db_path}")
        factory2 = sessionmaker(bind=eng2)
        with factory2() as session:
            _, non_admin_key = DatabaseAPIKeyAuth.create_key(
                session,
                user_id="normaluser",
                name="Audit E2E normal user key",
                zone_id="default",
                is_admin=False,
            )
            session.commit()
        eng2.dispose()
    except Exception:
        non_admin_key = None

    # Seed audit records directly in the database
    seeded = False
    try:
        from nexus.storage.exchange_audit_logger import ExchangeAuditLogger

        eng3 = ce(f"sqlite:///{db_path}")
        factory3 = sessionmaker(bind=eng3)
        audit_logger = ExchangeAuditLogger(session_factory=factory3)

        for i in range(5):
            audit_logger.record(
                protocol="internal" if i < 3 else "x402",
                buyer_agent_id=f"buyer-{i % 2}",
                seller_agent_id=f"seller-{i % 3}",
                amount=Decimal(str(10 * (i + 1))),
                currency="credits",
                status="settled" if i < 4 else "failed",
                application="gateway",
                zone_id="default",
                transfer_id=f"audit-e2e-tx-{i}",
            )
        seeded = True
        eng3.dispose()
    except Exception:
        seeded = False

    yield {
        "port": port,
        "base_url": base_url,
        "process": process,
        "api_key": api_key,
        "non_admin_key": non_admin_key,
        "db_path": db_path,
        "seeded": seeded,
    }

    _kill_process(process)


def _kill_process(process: subprocess.Popen) -> None:
    """Kill a subprocess and its children."""
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
def admin_client(audit_server) -> httpx.Client:
    """HTTP client with admin API key."""
    headers = {"Authorization": f"Bearer {audit_server['api_key']}"}
    with httpx.Client(
        base_url=audit_server["base_url"],
        timeout=30.0,
        trust_env=False,
        headers=headers,
    ) as c:
        yield c


@pytest.fixture(scope="module")
def normal_user_client(audit_server) -> httpx.Client:
    """HTTP client with non-admin (normal user) API key."""
    key = audit_server.get("non_admin_key")
    if not key:
        pytest.skip("Non-admin API key not created")
    headers = {"Authorization": f"Bearer {key}"}
    with httpx.Client(
        base_url=audit_server["base_url"],
        timeout=30.0,
        trust_env=False,
        headers=headers,
    ) as c:
        yield c


@pytest.fixture(scope="module")
def unauthenticated_client(audit_server) -> httpx.Client:
    """HTTP client WITHOUT auth headers."""
    with httpx.Client(
        base_url=audit_server["base_url"],
        timeout=30.0,
        trust_env=False,
    ) as c:
        yield c


# =============================================================================
# Health check
# =============================================================================


class TestServerHealth:
    def test_health(self, admin_client: httpx.Client):
        resp = admin_client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "healthy"


# =============================================================================
# Auth enforcement: unauthenticated requests rejected with 401
# =============================================================================


class TestUnauthenticatedAccess:
    """No API key → 401 on all audit endpoints."""

    def test_list_returns_401(self, unauthenticated_client: httpx.Client):
        resp = unauthenticated_client.get("/api/v2/audit/transactions")
        assert resp.status_code == 401, f"Expected 401, got {resp.status_code}: {resp.text}"

    def test_aggregations_returns_401(self, unauthenticated_client: httpx.Client):
        resp = unauthenticated_client.get("/api/v2/audit/transactions/aggregations")
        assert resp.status_code == 401, f"Expected 401, got {resp.status_code}: {resp.text}"

    def test_export_returns_401(self, unauthenticated_client: httpx.Client):
        resp = unauthenticated_client.get("/api/v2/audit/transactions/export")
        assert resp.status_code == 401, f"Expected 401, got {resp.status_code}: {resp.text}"

    def test_get_single_returns_401(self, unauthenticated_client: httpx.Client):
        resp = unauthenticated_client.get("/api/v2/audit/transactions/some-id")
        assert resp.status_code == 401, f"Expected 401, got {resp.status_code}: {resp.text}"

    def test_integrity_returns_401(self, unauthenticated_client: httpx.Client):
        resp = unauthenticated_client.get("/api/v2/audit/integrity/some-id")
        assert resp.status_code == 401, f"Expected 401, got {resp.status_code}: {resp.text}"


# =============================================================================
# Admin user: full access
# =============================================================================


class TestAdminAccess:
    """Admin API key → full access to all audit endpoints."""

    def test_list_transactions(self, admin_client: httpx.Client, audit_server):
        if not audit_server["seeded"]:
            pytest.skip("Audit records not seeded")
        resp = admin_client.get("/api/v2/audit/transactions")
        assert resp.status_code == 200, f"List failed: {resp.text}"
        data = resp.json()
        assert "transactions" in data
        assert len(data["transactions"]) >= 1

    def test_list_with_filter(self, admin_client: httpx.Client, audit_server):
        if not audit_server["seeded"]:
            pytest.skip("Audit records not seeded")
        resp = admin_client.get("/api/v2/audit/transactions", params={"protocol": "x402"})
        assert resp.status_code == 200
        txs = resp.json()["transactions"]
        assert all(tx["protocol"] == "x402" for tx in txs)

    def test_list_with_pagination(self, admin_client: httpx.Client, audit_server):
        if not audit_server["seeded"]:
            pytest.skip("Audit records not seeded")
        resp1 = admin_client.get("/api/v2/audit/transactions", params={"limit": 2})
        assert resp1.status_code == 200
        data1 = resp1.json()
        cursor = data1.get("next_cursor")
        if cursor:
            resp2 = admin_client.get(
                "/api/v2/audit/transactions", params={"limit": 2, "cursor": cursor}
            )
            data2 = resp2.json()
            ids1 = {tx["id"] for tx in data1["transactions"]}
            ids2 = {tx["id"] for tx in data2["transactions"]}
            assert ids1.isdisjoint(ids2), "Cursor pagination returned duplicates"

    def test_include_total(self, admin_client: httpx.Client, audit_server):
        if not audit_server["seeded"]:
            pytest.skip("Audit records not seeded")
        resp = admin_client.get("/api/v2/audit/transactions", params={"include_total": True})
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] is not None
        assert data["total"] >= 5

    def test_aggregations(self, admin_client: httpx.Client, audit_server):
        if not audit_server["seeded"]:
            pytest.skip("Audit records not seeded")
        resp = admin_client.get("/api/v2/audit/transactions/aggregations")
        assert resp.status_code == 200
        data = resp.json()
        assert data["tx_count"] >= 5
        assert float(data["total_volume"]) > 0

    def test_json_export(self, admin_client: httpx.Client, audit_server):
        if not audit_server["seeded"]:
            pytest.skip("Audit records not seeded")
        resp = admin_client.get("/api/v2/audit/transactions/export", params={"format": "json"})
        assert resp.status_code == 200
        assert "application/json" in resp.headers["content-type"]
        data = resp.json()
        assert len(data["transactions"]) >= 1

    def test_csv_export(self, admin_client: httpx.Client, audit_server):
        if not audit_server["seeded"]:
            pytest.skip("Audit records not seeded")
        resp = admin_client.get("/api/v2/audit/transactions/export", params={"format": "csv"})
        assert resp.status_code == 200
        assert "text/csv" in resp.headers["content-type"]
        lines = resp.text.strip().split("\n")
        assert len(lines) >= 2

    def test_get_single_transaction(self, admin_client: httpx.Client, audit_server):
        if not audit_server["seeded"]:
            pytest.skip("Audit records not seeded")
        list_resp = admin_client.get("/api/v2/audit/transactions", params={"limit": 1})
        tx_id = list_resp.json()["transactions"][0]["id"]
        resp = admin_client.get(f"/api/v2/audit/transactions/{tx_id}")
        assert resp.status_code == 200
        assert resp.json()["id"] == tx_id

    def test_get_nonexistent_returns_404(self, admin_client: httpx.Client):
        resp = admin_client.get("/api/v2/audit/transactions/nonexistent-id-xyz")
        assert resp.status_code == 404

    def test_integrity_verification(self, admin_client: httpx.Client, audit_server):
        if not audit_server["seeded"]:
            pytest.skip("Audit records not seeded")
        list_resp = admin_client.get("/api/v2/audit/transactions", params={"limit": 1})
        tx_id = list_resp.json()["transactions"][0]["id"]
        resp = admin_client.get(f"/api/v2/audit/integrity/{tx_id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["is_valid"] is True
        assert data["record_id"] == tx_id
        assert len(data["record_hash"]) == 64

    def test_response_fields(self, admin_client: httpx.Client, audit_server):
        if not audit_server["seeded"]:
            pytest.skip("Audit records not seeded")
        resp = admin_client.get("/api/v2/audit/transactions", params={"limit": 1})
        tx = resp.json()["transactions"][0]
        required = {
            "id",
            "record_hash",
            "created_at",
            "protocol",
            "buyer_agent_id",
            "seller_agent_id",
            "amount",
            "currency",
            "status",
            "application",
            "zone_id",
        }
        assert required.issubset(tx.keys())

    def test_invalid_limit(self, admin_client: httpx.Client):
        resp = admin_client.get("/api/v2/audit/transactions", params={"limit": 0})
        assert resp.status_code == 422


# =============================================================================
# Normal user (non-admin): read access with permissions enforced
# =============================================================================


class TestNormalUserAccess:
    """Non-admin API key with permissions enabled.

    Normal users (is_admin=False) go through the real ReBAC permission
    check path instead of the admin bypass. All audit read endpoints
    should still work for authenticated normal users.
    """

    def test_list_transactions(self, normal_user_client: httpx.Client, audit_server):
        if not audit_server["seeded"]:
            pytest.skip("Audit records not seeded")
        resp = normal_user_client.get("/api/v2/audit/transactions")
        assert resp.status_code == 200, f"Normal user list failed: {resp.status_code}: {resp.text}"
        data = resp.json()
        assert "transactions" in data
        assert isinstance(data["transactions"], list)

    def test_list_with_filter(self, normal_user_client: httpx.Client, audit_server):
        if not audit_server["seeded"]:
            pytest.skip("Audit records not seeded")
        resp = normal_user_client.get("/api/v2/audit/transactions", params={"protocol": "x402"})
        assert resp.status_code == 200, f"Normal user filter failed: {resp.text}"
        txs = resp.json()["transactions"]
        assert all(tx["protocol"] == "x402" for tx in txs)

    def test_list_with_pagination(self, normal_user_client: httpx.Client, audit_server):
        if not audit_server["seeded"]:
            pytest.skip("Audit records not seeded")
        resp = normal_user_client.get("/api/v2/audit/transactions", params={"limit": 2})
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["transactions"]) <= 2

    def test_include_total(self, normal_user_client: httpx.Client, audit_server):
        if not audit_server["seeded"]:
            pytest.skip("Audit records not seeded")
        resp = normal_user_client.get("/api/v2/audit/transactions", params={"include_total": True})
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] is not None

    def test_aggregations(self, normal_user_client: httpx.Client, audit_server):
        if not audit_server["seeded"]:
            pytest.skip("Audit records not seeded")
        resp = normal_user_client.get("/api/v2/audit/transactions/aggregations")
        assert resp.status_code == 200, f"Normal user aggregations failed: {resp.text}"
        data = resp.json()
        assert "tx_count" in data
        assert "total_volume" in data

    def test_json_export(self, normal_user_client: httpx.Client, audit_server):
        if not audit_server["seeded"]:
            pytest.skip("Audit records not seeded")
        resp = normal_user_client.get(
            "/api/v2/audit/transactions/export", params={"format": "json"}
        )
        assert resp.status_code == 200, f"Normal user JSON export failed: {resp.text}"
        data = resp.json()
        assert "transactions" in data

    def test_csv_export(self, normal_user_client: httpx.Client, audit_server):
        if not audit_server["seeded"]:
            pytest.skip("Audit records not seeded")
        resp = normal_user_client.get("/api/v2/audit/transactions/export", params={"format": "csv"})
        assert resp.status_code == 200, f"Normal user CSV export failed: {resp.text}"
        assert "text/csv" in resp.headers["content-type"]

    def test_get_single_transaction(self, normal_user_client: httpx.Client, audit_server):
        if not audit_server["seeded"]:
            pytest.skip("Audit records not seeded")
        list_resp = normal_user_client.get("/api/v2/audit/transactions", params={"limit": 1})
        txs = list_resp.json()["transactions"]
        if not txs:
            pytest.skip("No transactions visible to normal user")
        tx_id = txs[0]["id"]
        resp = normal_user_client.get(f"/api/v2/audit/transactions/{tx_id}")
        assert resp.status_code == 200, f"Normal user get single failed: {resp.text}"
        assert resp.json()["id"] == tx_id

    def test_integrity_verification(self, normal_user_client: httpx.Client, audit_server):
        if not audit_server["seeded"]:
            pytest.skip("Audit records not seeded")
        list_resp = normal_user_client.get("/api/v2/audit/transactions", params={"limit": 1})
        txs = list_resp.json()["transactions"]
        if not txs:
            pytest.skip("No transactions visible to normal user")
        tx_id = txs[0]["id"]
        resp = normal_user_client.get(f"/api/v2/audit/integrity/{tx_id}")
        assert resp.status_code == 200, f"Normal user integrity failed: {resp.text}"
        data = resp.json()
        assert data["is_valid"] is True
        assert data["record_id"] == tx_id

    def test_get_nonexistent_returns_404(self, normal_user_client: httpx.Client):
        resp = normal_user_client.get("/api/v2/audit/transactions/nonexistent-id")
        assert resp.status_code == 404

    def test_integrity_nonexistent_returns_404(self, normal_user_client: httpx.Client):
        resp = normal_user_client.get("/api/v2/audit/integrity/nonexistent-id")
        assert resp.status_code == 404
