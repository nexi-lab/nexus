"""Unit tests for the audit API router.

Issue #1360 Phase 2: Tests for all audit endpoints including
list, aggregation, export, single lookup, integrity verification,
and authentication enforcement.
Uses a real SQLite-backed ExchangeAuditLogger (no mocks for data layer).
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from nexus.server.api.v2.dependencies import get_exchange_audit_logger
from nexus.server.api.v2.routers.audit import router
from nexus.storage.exchange_audit_logger import ExchangeAuditLogger

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

_test_app = FastAPI()
_test_app.include_router(router)


def _make_test_logger():
    """Create a real ExchangeAuditLogger backed by SQLite.

    Uses StaticPool to ensure all sessions share the same in-memory
    database connection (SQLite :memory: is per-connection).
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from nexus.storage.models._base import Base

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    return ExchangeAuditLogger(session_factory=factory)


# Shared test logger + zone
_audit_logger = _make_test_logger()
_zone_id = "test-zone"


def _override_dep():
    return _audit_logger, _zone_id


_test_app.dependency_overrides[get_exchange_audit_logger] = _override_dep


@pytest.fixture(autouse=True)
def _seed_data():
    """Seed test data before every test module execution.

    We seed once via the module-level logger; tests read from it.
    """
    # Only seed if empty
    try:
        count = _audit_logger.count_transactions(zone_id=_zone_id)
    except Exception:
        count = 0

    if count == 0:
        for i in range(5):
            _audit_logger.record(
                protocol="internal" if i < 3 else "x402",
                buyer_agent_id=f"buyer-{i % 2}",
                seller_agent_id=f"seller-{i % 3}",
                amount=Decimal(str(10 * (i + 1))),
                currency="credits",
                status="settled" if i < 4 else "failed",
                application="gateway",
                zone_id=_zone_id,
                transfer_id=f"seed-tx-{i}",
            )
    yield


@pytest.fixture
def client():
    return TestClient(_test_app)


# ---------------------------------------------------------------------------
# List endpoint
# ---------------------------------------------------------------------------


class TestListTransactions:
    def test_list_returns_200(self, client: TestClient) -> None:
        resp = client.get("/api/v2/audit/transactions")
        assert resp.status_code == 200
        data = resp.json()
        assert "transactions" in data
        assert isinstance(data["transactions"], list)
        assert len(data["transactions"]) >= 1

    def test_list_filter_by_protocol(self, client: TestClient) -> None:
        resp = client.get("/api/v2/audit/transactions", params={"protocol": "x402"})
        assert resp.status_code == 200
        txs = resp.json()["transactions"]
        assert all(tx["protocol"] == "x402" for tx in txs)

    def test_list_filter_by_status(self, client: TestClient) -> None:
        resp = client.get("/api/v2/audit/transactions", params={"status": "failed"})
        assert resp.status_code == 200
        txs = resp.json()["transactions"]
        assert all(tx["status"] == "failed" for tx in txs)

    def test_list_filter_by_buyer(self, client: TestClient) -> None:
        resp = client.get("/api/v2/audit/transactions", params={"buyer_agent_id": "buyer-0"})
        assert resp.status_code == 200
        txs = resp.json()["transactions"]
        assert all(tx["buyer_agent_id"] == "buyer-0" for tx in txs)

    def test_list_with_limit(self, client: TestClient) -> None:
        resp = client.get("/api/v2/audit/transactions", params={"limit": 2})
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["transactions"]) <= 2

    def test_list_with_cursor_pagination(self, client: TestClient) -> None:
        resp1 = client.get("/api/v2/audit/transactions", params={"limit": 2})
        data1 = resp1.json()
        cursor = data1.get("next_cursor")
        if cursor:
            resp2 = client.get("/api/v2/audit/transactions", params={"limit": 2, "cursor": cursor})
            data2 = resp2.json()
            ids1 = {tx["id"] for tx in data1["transactions"]}
            ids2 = {tx["id"] for tx in data2["transactions"]}
            assert ids1.isdisjoint(ids2)

    def test_list_include_total(self, client: TestClient) -> None:
        resp = client.get("/api/v2/audit/transactions", params={"include_total": True})
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] is not None
        assert data["total"] >= 5

    def test_list_empty_with_nonexistent_filter(self, client: TestClient) -> None:
        resp = client.get("/api/v2/audit/transactions", params={"buyer_agent_id": "nobody"})
        assert resp.status_code == 200
        assert resp.json()["transactions"] == []

    def test_transaction_response_fields(self, client: TestClient) -> None:
        resp = client.get("/api/v2/audit/transactions", params={"limit": 1})
        tx = resp.json()["transactions"][0]
        required_fields = {
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
        assert required_fields.issubset(tx.keys())


# ---------------------------------------------------------------------------
# Aggregation endpoint
# ---------------------------------------------------------------------------


class TestAggregations:
    def test_aggregation_returns_200(self, client: TestClient) -> None:
        resp = client.get("/api/v2/audit/transactions/aggregations")
        assert resp.status_code == 200
        data = resp.json()
        assert "total_volume" in data
        assert "tx_count" in data
        assert "top_buyers" in data
        assert "top_sellers" in data

    def test_aggregation_values(self, client: TestClient) -> None:
        resp = client.get("/api/v2/audit/transactions/aggregations")
        data = resp.json()
        assert data["tx_count"] >= 5
        assert float(data["total_volume"]) > 0


# ---------------------------------------------------------------------------
# Export endpoint
# ---------------------------------------------------------------------------


class TestExport:
    def test_json_export(self, client: TestClient) -> None:
        resp = client.get("/api/v2/audit/transactions/export", params={"format": "json"})
        assert resp.status_code == 200
        assert "application/json" in resp.headers["content-type"]
        data = resp.json()
        assert "transactions" in data
        assert len(data["transactions"]) >= 1

    def test_csv_export(self, client: TestClient) -> None:
        resp = client.get("/api/v2/audit/transactions/export", params={"format": "csv"})
        assert resp.status_code == 200
        assert "text/csv" in resp.headers["content-type"]
        lines = resp.text.strip().split("\n")
        assert len(lines) >= 2  # Header + at least one data row

    def test_csv_headers(self, client: TestClient) -> None:
        resp = client.get("/api/v2/audit/transactions/export", params={"format": "csv"})
        header_line = resp.text.strip().split("\n")[0]
        expected_headers = [
            "id",
            "created_at",
            "protocol",
            "buyer_agent_id",
            "seller_agent_id",
            "amount",
            "currency",
            "status",
            "application",
            "zone_id",
            "trace_id",
            "transfer_id",
            "record_hash",
        ]
        for h in expected_headers:
            assert h in header_line

    def test_export_with_filter(self, client: TestClient) -> None:
        resp = client.get(
            "/api/v2/audit/transactions/export", params={"format": "json", "protocol": "x402"}
        )
        data = resp.json()
        assert all(tx["protocol"] == "x402" for tx in data["transactions"])


# ---------------------------------------------------------------------------
# Single transaction endpoint
# ---------------------------------------------------------------------------


class TestGetTransaction:
    def test_get_existing_transaction(self, client: TestClient) -> None:
        # First, get an ID from the list
        list_resp = client.get("/api/v2/audit/transactions", params={"limit": 1})
        tx_id = list_resp.json()["transactions"][0]["id"]

        resp = client.get(f"/api/v2/audit/transactions/{tx_id}")
        assert resp.status_code == 200
        assert resp.json()["id"] == tx_id

    def test_get_nonexistent_transaction(self, client: TestClient) -> None:
        resp = client.get("/api/v2/audit/transactions/nonexistent-id")
        assert resp.status_code == 404

    def test_zone_scoping(self, client: TestClient) -> None:
        """Transaction from a different zone should return 404."""
        # Create a record in a different zone
        _audit_logger.record(
            protocol="internal",
            buyer_agent_id="buyer-x",
            seller_agent_id="seller-x",
            amount=Decimal("1"),
            status="settled",
            application="gateway",
            zone_id="other-zone",
            transfer_id="zone-scoped-tx",
        )
        # List all to find it
        rows, _ = _audit_logger.list_transactions_cursor(filters={"zone_id": "other-zone"}, limit=1)
        if rows:
            other_id = rows[0].id
            resp = client.get(f"/api/v2/audit/transactions/{other_id}")
            # Should be 404 because our test zone is "test-zone"
            assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Integrity endpoint
# ---------------------------------------------------------------------------


class TestIntegrity:
    def test_verify_valid_record(self, client: TestClient) -> None:
        list_resp = client.get("/api/v2/audit/transactions", params={"limit": 1})
        tx_id = list_resp.json()["transactions"][0]["id"]

        resp = client.get(f"/api/v2/audit/integrity/{tx_id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["is_valid"] is True
        assert data["record_id"] == tx_id
        assert len(data["record_hash"]) == 64

    def test_verify_nonexistent_record(self, client: TestClient) -> None:
        resp = client.get("/api/v2/audit/integrity/nonexistent-id")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


class TestErrorHandling:
    def test_invalid_limit_below_min(self, client: TestClient) -> None:
        resp = client.get("/api/v2/audit/transactions", params={"limit": 0})
        assert resp.status_code == 422  # Validation error

    def test_invalid_limit_above_max(self, client: TestClient) -> None:
        resp = client.get("/api/v2/audit/transactions", params={"limit": 9999})
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Auth enforcement
# ---------------------------------------------------------------------------


def _create_test_app(zone_id: str = "test-zone", *, require_auth: bool = True) -> FastAPI:
    """Create a FastAPI test app with configurable auth behavior.

    When ``require_auth=True`` the dependency returns a real logger/zone.
    When ``require_auth=False`` the dependency raises 401 (simulates
    unauthenticated access).
    """
    app = FastAPI()
    app.include_router(router)

    if require_auth:

        async def _mock_dep():
            return _audit_logger, zone_id

        app.dependency_overrides[get_exchange_audit_logger] = _mock_dep
    else:

        async def _mock_dep_unauthed():
            raise HTTPException(status_code=401, detail="Authentication required")

        app.dependency_overrides[get_exchange_audit_logger] = _mock_dep_unauthed

    return app


class TestAuthEnforcement:
    """Verify all audit endpoints return 401 without authentication."""

    def test_list_transactions_auth_required(self) -> None:
        app = _create_test_app(require_auth=False)
        client = TestClient(app)
        resp = client.get("/api/v2/audit/transactions")
        assert resp.status_code == 401

    def test_aggregations_auth_required(self) -> None:
        app = _create_test_app(require_auth=False)
        client = TestClient(app)
        resp = client.get("/api/v2/audit/transactions/aggregations")
        assert resp.status_code == 401

    def test_export_auth_required(self) -> None:
        app = _create_test_app(require_auth=False)
        client = TestClient(app)
        resp = client.get("/api/v2/audit/transactions/export")
        assert resp.status_code == 401

    def test_get_transaction_auth_required(self) -> None:
        app = _create_test_app(require_auth=False)
        client = TestClient(app)
        resp = client.get("/api/v2/audit/transactions/some-id")
        assert resp.status_code == 401

    def test_integrity_auth_required(self) -> None:
        app = _create_test_app(require_auth=False)
        client = TestClient(app)
        resp = client.get("/api/v2/audit/integrity/some-id")
        assert resp.status_code == 401


class TestZoneIsolation:
    """Verify zone-scoped access returns correct results."""

    def test_different_zone_sees_no_data(self) -> None:
        """A user in 'empty-zone' sees zero transactions."""
        app = _create_test_app(zone_id="empty-zone")
        client = TestClient(app)
        resp = client.get("/api/v2/audit/transactions", params={"include_total": True})
        assert resp.status_code == 200
        data = resp.json()
        assert data["transactions"] == []
        assert data["total"] == 0

    def test_zone_aggregations_isolated(self) -> None:
        """Aggregations for an empty zone return zero volume."""
        app = _create_test_app(zone_id="empty-zone")
        client = TestClient(app)
        resp = client.get("/api/v2/audit/transactions/aggregations")
        assert resp.status_code == 200
        data = resp.json()
        assert data["tx_count"] == 0
        assert float(data["total_volume"]) == 0

    def test_zone_export_isolated(self) -> None:
        """Export for an empty zone returns zero rows."""
        app = _create_test_app(zone_id="empty-zone")
        client = TestClient(app)
        resp = client.get("/api/v2/audit/transactions/export", params={"format": "json"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["transactions"] == []
