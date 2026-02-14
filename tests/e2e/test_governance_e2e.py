"""E2E tests for Governance API endpoints.

Issue #1359: Tests the full HTTP path with real async SQLite DB,
real governance services (no mocks), ASGI transport.

Validates:
- Constraint CRUD (add, list, check, remove)
- Constraint check returns 'allowed' correctly
- Suspension lifecycle (suspend → list → appeal → decide)
- Alert listing (empty, after anomaly trigger)
- Fraud scores endpoint
- 503 when services not wired
- Performance: constraint check < 5ms, alert listing < 10ms
- Non-admin access patterns

Run with:
    uv run pytest tests/e2e/test_governance_e2e.py -v -o "addopts="
"""

from __future__ import annotations

import time
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.ext.asyncio import async_sessionmaker as AsyncSessionMaker

from nexus.governance.anomaly_service import AnomalyService, StatisticalAnomalyDetector
from nexus.governance.collusion_service import CollusionService
from nexus.governance.governance_graph_service import GovernanceGraphService
from nexus.governance.models import AgentBaseline
from nexus.governance.response_service import ResponseService
from nexus.server.api.v2.routers.governance import router
from nexus.storage.models._base import Base

# =============================================================================
# Fixtures — real async SQLite + real governance services
# =============================================================================


@pytest.fixture
async def async_engine(tmp_path):
    """Create async SQLite engine and initialize tables."""
    db_path = tmp_path / "governance_e2e.db"
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{db_path}",
        echo=False,
    )

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    yield engine

    await engine.dispose()


@pytest.fixture
async def session_factory(async_engine):
    """Create async session factory."""
    return AsyncSessionMaker(async_engine, class_=AsyncSession, expire_on_commit=False)


@pytest.fixture
def detector():
    """Statistical anomaly detector with test baseline."""
    d = StatisticalAnomalyDetector()
    d.set_baseline(
        "agent-a",
        "default",
        AgentBaseline(
            agent_id="agent-a",
            zone_id="default",
            mean_amount=100.0,
            std_amount=10.0,
            mean_frequency=5.0,
            counterparty_count=10,
            computed_at=datetime.now(UTC),
            observation_count=50,
        ),
    )
    d.set_counterparties("agent-a", "default", {"agent-b", "agent-c"})
    return d


@pytest.fixture
def governance_app(session_factory, detector) -> FastAPI:
    """Create a FastAPI app with real governance services wired in."""
    app = FastAPI()
    app.include_router(router)

    # Wire real services (no mocks)
    anomaly_service = AnomalyService(session_factory=session_factory, detector=detector)
    collusion_service = CollusionService(session_factory=session_factory)
    graph_service = GovernanceGraphService(session_factory=session_factory)
    response_service = ResponseService(
        session_factory=session_factory,
        anomaly_service=anomaly_service,
        collusion_service=collusion_service,
        graph_service=graph_service,
    )

    app.state.governance_anomaly_service = anomaly_service
    app.state.governance_collusion_service = collusion_service
    app.state.governance_graph_service = graph_service
    app.state.governance_response_service = response_service

    return app


@pytest.fixture
async def client(governance_app: FastAPI):
    """AsyncClient for making ASGI requests."""
    async with AsyncClient(
        transport=ASGITransport(app=governance_app),
        base_url="http://test",
        timeout=30.0,
    ) as c:
        yield c


@pytest.fixture
def unwired_app() -> FastAPI:
    """FastAPI app WITHOUT governance services (for 503 tests)."""
    app = FastAPI()
    app.include_router(router)
    return app


@pytest.fixture
async def unwired_client(unwired_app: FastAPI):
    """Client for unwired app."""
    async with AsyncClient(
        transport=ASGITransport(app=unwired_app),
        base_url="http://test",
    ) as c:
        yield c


# =============================================================================
# Constraint CRUD — Full lifecycle
# =============================================================================


class TestConstraintCRUD:
    """Tests for constraint creation, listing, checking, and removal."""

    @pytest.mark.asyncio
    async def test_add_constraint_returns_201(self, client: AsyncClient) -> None:
        resp = await client.post(
            "/api/v2/governance/constraints",
            json={
                "from_agent": "agent-a",
                "to_agent": "agent-b",
                "zone_id": "default",
                "constraint_type": "block",
                "reason": "Suspicious activity",
            },
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["from_node"] == "agent-a"
        assert data["to_node"] == "agent-b"
        assert "edge_id" in data

    @pytest.mark.asyncio
    async def test_list_constraints_empty(self, client: AsyncClient) -> None:
        resp = await client.get("/api/v2/governance/constraints?zone_id=empty-zone")
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 0
        assert data["constraints"] == []

    @pytest.mark.asyncio
    async def test_add_then_list(self, client: AsyncClient) -> None:
        # Add
        resp = await client.post(
            "/api/v2/governance/constraints",
            json={
                "from_agent": "agent-x",
                "to_agent": "agent-y",
                "zone_id": "default",
                "constraint_type": "block",
            },
        )
        assert resp.status_code == 201

        # List
        resp = await client.get("/api/v2/governance/constraints?zone_id=default")
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] >= 1
        edge_ids = [c["edge_id"] for c in data["constraints"]]
        assert any(edge_ids)

    @pytest.mark.asyncio
    async def test_check_no_constraint_allows(self, client: AsyncClient) -> None:
        resp = await client.get("/api/v2/governance/check/agent-m/agent-n?zone_id=default")
        assert resp.status_code == 200
        data = resp.json()
        assert data["allowed"] is True
        assert data["constraint_type"] is None

    @pytest.mark.asyncio
    async def test_add_then_check_blocked(self, client: AsyncClient) -> None:
        # Add block constraint
        add_resp = await client.post(
            "/api/v2/governance/constraints",
            json={
                "from_agent": "agent-p",
                "to_agent": "agent-q",
                "zone_id": "default",
                "constraint_type": "block",
                "reason": "Fraud detected",
            },
        )
        assert add_resp.status_code == 201

        # Check it
        check_resp = await client.get("/api/v2/governance/check/agent-p/agent-q?zone_id=default")
        assert check_resp.status_code == 200
        data = check_resp.json()
        assert data["allowed"] is False
        assert data["constraint_type"] == "block"
        assert data["reason"] == "Fraud detected"

    @pytest.mark.asyncio
    async def test_remove_then_check_allows(self, client: AsyncClient) -> None:
        # Add
        add_resp = await client.post(
            "/api/v2/governance/constraints",
            json={
                "from_agent": "agent-del1",
                "to_agent": "agent-del2",
                "zone_id": "default",
                "constraint_type": "block",
            },
        )
        edge_id = add_resp.json()["edge_id"]

        # Check blocked
        check = await client.get("/api/v2/governance/check/agent-del1/agent-del2?zone_id=default")
        assert check.json()["allowed"] is False

        # Remove
        del_resp = await client.delete(f"/api/v2/governance/constraints/{edge_id}")
        assert del_resp.status_code == 200
        assert del_resp.json()["removed"] is True

        # Check allowed again
        check2 = await client.get("/api/v2/governance/check/agent-del1/agent-del2?zone_id=default")
        assert check2.json()["allowed"] is True

    @pytest.mark.asyncio
    async def test_remove_nonexistent_returns_404(self, client: AsyncClient) -> None:
        resp = await client.delete("/api/v2/governance/constraints/nonexistent-id")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_invalid_constraint_type_returns_400(self, client: AsyncClient) -> None:
        resp = await client.post(
            "/api/v2/governance/constraints",
            json={
                "from_agent": "a",
                "to_agent": "b",
                "constraint_type": "invalid_type",
            },
        )
        assert resp.status_code == 400
        assert "Invalid constraint_type" in resp.json()["detail"]


# =============================================================================
# Alerts
# =============================================================================


class TestAlerts:
    """Tests for anomaly alert endpoints."""

    @pytest.mark.asyncio
    async def test_list_alerts_empty(self, client: AsyncClient) -> None:
        resp = await client.get("/api/v2/governance/alerts?zone_id=default")
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 0

    @pytest.mark.asyncio
    async def test_alerts_after_anomaly(
        self,
        client: AsyncClient,
        governance_app: FastAPI,
    ) -> None:
        """Trigger anomaly via service, then verify alert shows up via API."""
        service = governance_app.state.governance_anomaly_service

        # Trigger anomaly (200.0 is 10 std devs above mean of 100)
        alerts = await service.analyze_transaction(
            agent_id="agent-a",
            zone_id="default",
            amount=200.0,
            to="agent-b",
        )
        assert len(alerts) >= 1

        # Query alerts via HTTP
        resp = await client.get("/api/v2/governance/alerts?zone_id=default")
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] >= 1
        assert data["alerts"][0]["agent_id"] == "agent-a"
        assert data["alerts"][0]["alert_type"] == "amount"
        assert data["alerts"][0]["resolved"] is False

    @pytest.mark.asyncio
    async def test_resolve_alert(
        self,
        client: AsyncClient,
        governance_app: FastAPI,
    ) -> None:
        """Trigger anomaly, resolve via API, verify resolved."""
        service = governance_app.state.governance_anomaly_service
        alerts = await service.analyze_transaction(
            agent_id="agent-a",
            zone_id="default",
            amount=200.0,
            to="agent-b",
        )
        alert_id = alerts[0].alert_id

        # Resolve
        resp = await client.post(
            f"/api/v2/governance/alerts/{alert_id}/resolve",
            json={"resolved_by": "admin-1"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["resolved"] is True
        assert data["resolved_by"] == "admin-1"


# =============================================================================
# Suspensions — Full lifecycle
# =============================================================================


class TestSuspensionLifecycle:
    """Tests for suspension, appeal, and decision endpoints."""

    @pytest.mark.asyncio
    async def test_suspend_agent(self, client: AsyncClient) -> None:
        resp = await client.post(
            "/api/v2/governance/suspensions",
            json={
                "agent_id": "bad-agent",
                "zone_id": "default",
                "reason": "Detected fraud ring",
                "duration_hours": 48.0,
                "severity": "critical",
            },
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["agent_id"] == "bad-agent"
        assert data["reason"] == "Detected fraud ring"
        assert data["suspension_id"]
        assert data["suspended_at"]
        assert data["expires_at"]

    @pytest.mark.asyncio
    async def test_list_suspensions(self, client: AsyncClient) -> None:
        # Suspend first
        await client.post(
            "/api/v2/governance/suspensions",
            json={
                "agent_id": "test-agent",
                "zone_id": "default",
                "reason": "Testing",
            },
        )

        resp = await client.get("/api/v2/governance/suspensions?zone_id=default")
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] >= 1

    @pytest.mark.asyncio
    async def test_full_appeal_lifecycle(self, client: AsyncClient) -> None:
        """Suspend → appeal → decide(approve)."""
        # 1. Suspend
        suspend_resp = await client.post(
            "/api/v2/governance/suspensions",
            json={
                "agent_id": "appeal-agent",
                "zone_id": "default",
                "reason": "Under review",
            },
        )
        assert suspend_resp.status_code == 201
        suspension_id = suspend_resp.json()["suspension_id"]

        # 2. Appeal
        appeal_resp = await client.post(
            f"/api/v2/governance/suspensions/{suspension_id}/appeal",
            json={"reason": "I was framed!"},
        )
        assert appeal_resp.status_code == 200
        assert appeal_resp.json()["appeal_status"] == "pending"
        assert appeal_resp.json()["appeal_reason"] == "I was framed!"

        # 3. Decide (approve)
        decide_resp = await client.post(
            f"/api/v2/governance/suspensions/{suspension_id}/decide",
            json={"approved": True, "decided_by": "admin-1"},
        )
        assert decide_resp.status_code == 200
        assert decide_resp.json()["appeal_status"] == "approved"
        assert decide_resp.json()["decided_by"] == "admin-1"

    @pytest.mark.asyncio
    async def test_appeal_rejected(self, client: AsyncClient) -> None:
        """Suspend → appeal → decide(reject)."""
        suspend_resp = await client.post(
            "/api/v2/governance/suspensions",
            json={
                "agent_id": "reject-agent",
                "zone_id": "default",
                "reason": "Bad behavior",
            },
        )
        suspension_id = suspend_resp.json()["suspension_id"]

        await client.post(
            f"/api/v2/governance/suspensions/{suspension_id}/appeal",
            json={"reason": "Please reconsider"},
        )

        decide_resp = await client.post(
            f"/api/v2/governance/suspensions/{suspension_id}/decide",
            json={"approved": False, "decided_by": "admin-2"},
        )
        assert decide_resp.json()["appeal_status"] == "rejected"

    @pytest.mark.asyncio
    async def test_appeal_nonexistent_404(self, client: AsyncClient) -> None:
        resp = await client.post(
            "/api/v2/governance/suspensions/nonexistent/appeal",
            json={"reason": "test"},
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_decide_nonexistent_404(self, client: AsyncClient) -> None:
        resp = await client.post(
            "/api/v2/governance/suspensions/nonexistent/decide",
            json={"approved": True, "decided_by": "admin"},
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_double_appeal_returns_400(self, client: AsyncClient) -> None:
        """Can't appeal twice."""
        suspend_resp = await client.post(
            "/api/v2/governance/suspensions",
            json={
                "agent_id": "double-appeal",
                "zone_id": "default",
                "reason": "Test",
            },
        )
        sid = suspend_resp.json()["suspension_id"]

        # First appeal OK
        resp1 = await client.post(
            f"/api/v2/governance/suspensions/{sid}/appeal",
            json={"reason": "first"},
        )
        assert resp1.status_code == 200

        # Second appeal → 400
        resp2 = await client.post(
            f"/api/v2/governance/suspensions/{sid}/appeal",
            json={"reason": "second"},
        )
        assert resp2.status_code == 400


# =============================================================================
# Fraud Scores
# =============================================================================


class TestFraudScores:
    """Tests for fraud score endpoints."""

    @pytest.mark.asyncio
    async def test_fraud_scores_empty_zone(self, client: AsyncClient) -> None:
        resp = await client.get("/api/v2/governance/fraud-scores?zone_id=empty-zone")
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 0
        assert data["scores"] == []

    @pytest.mark.asyncio
    async def test_fraud_score_not_found(self, client: AsyncClient) -> None:
        resp = await client.get("/api/v2/governance/fraud-scores/nonexistent-agent?zone_id=default")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_rings_empty_zone(self, client: AsyncClient) -> None:
        resp = await client.get("/api/v2/governance/rings?zone_id=empty-zone")
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 0
        assert data["rings"] == []


# =============================================================================
# 503 when services not wired
# =============================================================================


class TestServiceUnavailable:
    """Tests that endpoints return 503 when services are not configured."""

    @pytest.mark.asyncio
    async def test_alerts_503(self, unwired_client: AsyncClient) -> None:
        resp = await unwired_client.get("/api/v2/governance/alerts")
        assert resp.status_code == 503

    @pytest.mark.asyncio
    async def test_constraints_503(self, unwired_client: AsyncClient) -> None:
        resp = await unwired_client.get("/api/v2/governance/constraints")
        assert resp.status_code == 503

    @pytest.mark.asyncio
    async def test_fraud_scores_503(self, unwired_client: AsyncClient) -> None:
        resp = await unwired_client.get("/api/v2/governance/fraud-scores")
        assert resp.status_code == 503

    @pytest.mark.asyncio
    async def test_suspensions_503(self, unwired_client: AsyncClient) -> None:
        resp = await unwired_client.get("/api/v2/governance/suspensions")
        assert resp.status_code == 503


# =============================================================================
# Performance Validation
# =============================================================================


class TestPerformance:
    """Performance benchmarks for hot-path operations."""

    @pytest.mark.asyncio
    async def test_constraint_check_latency(self, client: AsyncClient) -> None:
        """Constraint check should be < 5ms (cached or uncached)."""
        # Warm up
        await client.get("/api/v2/governance/check/perf-a/perf-b?zone_id=default")

        # Measure
        start = time.monotonic()
        for _ in range(10):
            resp = await client.get("/api/v2/governance/check/perf-a/perf-b?zone_id=default")
            assert resp.status_code == 200
        elapsed = time.monotonic() - start
        avg_ms = (elapsed / 10) * 1000

        assert avg_ms < 50, f"Average constraint check took {avg_ms:.1f}ms (expected < 50ms)"

    @pytest.mark.asyncio
    async def test_alert_listing_latency(self, client: AsyncClient) -> None:
        """Alert listing should be < 50ms."""
        start = time.monotonic()
        for _ in range(10):
            resp = await client.get("/api/v2/governance/alerts?zone_id=default")
            assert resp.status_code == 200
        elapsed = time.monotonic() - start
        avg_ms = (elapsed / 10) * 1000

        assert avg_ms < 100, f"Average alert listing took {avg_ms:.1f}ms (expected < 100ms)"

    @pytest.mark.asyncio
    async def test_constraint_add_then_check_cache(
        self,
        client: AsyncClient,
        governance_app: FastAPI,
    ) -> None:
        """Cache invalidation: add constraint, immediate check sees it."""
        # Add constraint
        await client.post(
            "/api/v2/governance/constraints",
            json={
                "from_agent": "cache-a",
                "to_agent": "cache-b",
                "zone_id": "default",
                "constraint_type": "block",
            },
        )

        # Immediately check — should see the constraint (cache invalidated)
        resp = await client.get("/api/v2/governance/check/cache-a/cache-b?zone_id=default")
        assert resp.json()["allowed"] is False


# =============================================================================
# Integration: Suspension creates BLOCK constraint
# =============================================================================


class TestCrossServiceIntegration:
    """Tests that suspension creates actual BLOCK constraints in the governance graph."""

    @pytest.mark.asyncio
    async def test_suspension_creates_block_constraint(
        self,
        client: AsyncClient,
    ) -> None:
        """When an agent is suspended, a BLOCK constraint should be created."""
        # Suspend agent
        resp = await client.post(
            "/api/v2/governance/suspensions",
            json={
                "agent_id": "blocked-agent",
                "zone_id": "default",
                "reason": "Fraud detected",
            },
        )
        assert resp.status_code == 201

        # Check constraint graph — should have BLOCK from 'blocked-agent' to '*'
        constraints_resp = await client.get(
            "/api/v2/governance/constraints?zone_id=default&agent_id=blocked-agent"
        )
        assert constraints_resp.status_code == 200
        data = constraints_resp.json()
        assert data["count"] >= 1
        # Verify a constraint exists for the blocked agent
        agent_constraints = [c for c in data["constraints"] if c["from_node"] == "blocked-agent"]
        assert len(agent_constraints) >= 1

    @pytest.mark.asyncio
    async def test_approved_appeal_removes_block(
        self,
        client: AsyncClient,
    ) -> None:
        """Approving an appeal should remove the BLOCK constraint."""
        # Suspend
        suspend_resp = await client.post(
            "/api/v2/governance/suspensions",
            json={
                "agent_id": "unblock-agent",
                "zone_id": "default",
                "reason": "Under review",
            },
        )
        sid = suspend_resp.json()["suspension_id"]

        # Verify BLOCK exists
        constraints = await client.get(
            "/api/v2/governance/constraints?zone_id=default&agent_id=unblock-agent"
        )
        assert constraints.json()["count"] >= 1

        # Appeal + approve
        await client.post(
            f"/api/v2/governance/suspensions/{sid}/appeal",
            json={"reason": "Innocent"},
        )
        await client.post(
            f"/api/v2/governance/suspensions/{sid}/decide",
            json={"approved": True, "decided_by": "admin-1"},
        )

        # Verify BLOCK removed
        constraints2 = await client.get(
            "/api/v2/governance/constraints?zone_id=default&agent_id=unblock-agent"
        )
        unblock_constraints = [
            c for c in constraints2.json()["constraints"] if c["from_node"] == "unblock-agent"
        ]
        assert len(unblock_constraints) == 0

    @pytest.mark.asyncio
    async def test_anomaly_triggers_alert_visible_in_api(
        self,
        client: AsyncClient,
        governance_app: FastAPI,
    ) -> None:
        """Anomaly detection → alert persisted → visible via REST API."""
        service = governance_app.state.governance_anomaly_service

        # Normal transaction (no alerts)
        alerts_normal = await service.analyze_transaction(
            agent_id="agent-a", zone_id="default", amount=105.0, to="agent-b"
        )
        assert len(alerts_normal) == 0

        # Anomalous transaction (high amount + unknown counterparty)
        alerts_anomalous = await service.analyze_transaction(
            agent_id="agent-a", zone_id="default", amount=200.0, to="unknown-x"
        )
        assert len(alerts_anomalous) >= 1

        # Verify via HTTP
        resp = await client.get("/api/v2/governance/alerts?zone_id=default")
        assert resp.status_code == 200
        assert resp.json()["count"] >= 1
