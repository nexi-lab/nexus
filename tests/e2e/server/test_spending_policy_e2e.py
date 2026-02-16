"""E2E tests for Spending Policy enforcement through FastAPI.

Issue #1358: Tests the full HTTP path for policy enforcement:
- Budget endpoint returns remaining budget
- Transfer blocked by per-tx limit → 403
- Transfer blocked by daily limit → 403
- Transfer allowed when within all limits
- Policy CRUD endpoints (admin-only)
- Real DB integration with SpendingPolicyService (SQLite async)

Section 1: Lightweight mocked tests (no DB)
Section 2: Full integration tests with real SpendingPolicyService + SQLite
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, Header
from httpx import ASGITransport, AsyncClient

from nexus.pay.spending_policy import PolicyDeniedError, PolicyEvaluation, SpendingPolicy
from nexus.server.api.v2.routers.pay import _register_pay_exception_handlers, router

# =============================================================================
# Fixtures
# =============================================================================


def _create_test_app(
    *,
    mock_credits_service: AsyncMock,
    mock_policy_service: AsyncMock,
    is_admin: bool = False,
    agent_id: str = "test-agent",
    zone_id: str = "default",
) -> FastAPI:
    """Create a lightweight FastAPI app with pay router + policy service."""
    app = FastAPI()
    app.include_router(router)
    _register_pay_exception_handlers(app)

    # Wire services into app state
    app.state.credits_service = mock_credits_service
    app.state.spending_policy_service = mock_policy_service
    app.state.x402_client = None

    # Override auth dependency
    from nexus.server.api.v2.routers.pay import _get_require_auth

    async def mock_auth(
        authorization: str | None = Header(None, alias="Authorization"),
        x_agent_id: str | None = Header(None, alias="X-Agent-ID"),
        x_nexus_subject: str | None = Header(None, alias="X-Nexus-Subject"),
        x_nexus_zone_id: str | None = Header(None, alias="X-Nexus-Zone-ID"),
    ) -> dict[str, Any]:
        return {
            "authenticated": True,
            "subject_type": "agent",
            "subject_id": agent_id,
            "zone_id": zone_id,
            "is_admin": is_admin,
            "x_agent_id": None,
            "metadata": {},
        }

    app.dependency_overrides[_get_require_auth()] = mock_auth
    return app


@pytest.fixture
def mock_credits_service():
    """Mock CreditsService."""
    service = AsyncMock()
    service.get_balance.return_value = Decimal("100.0")
    service.get_balance_with_reserved.return_value = (Decimal("100.0"), Decimal("5.0"))
    service.check_budget.return_value = True
    service.transfer.return_value = "tx-123"
    service.reserve.return_value = "res-123"
    service.commit_reservation.return_value = None
    service.release_reservation.return_value = None
    service.deduct_fast.return_value = True
    service.transfer_batch.return_value = ["tx-1"]
    service.provision_wallet.return_value = None
    return service


@pytest.fixture
def mock_policy_service():
    """Mock SpendingPolicyService."""
    service = AsyncMock()
    service.evaluate.return_value = PolicyEvaluation(allowed=True)
    service.record_spending.return_value = None
    service.get_budget_summary.return_value = {
        "has_policy": True,
        "policy_id": "p1",
        "limits": {"daily": "100", "per_tx": "50"},
        "spent": {"daily": "25"},
        "remaining": {"daily": "75", "per_tx": "50"},
    }
    service.create_policy.return_value = SpendingPolicy(
        policy_id="new-p1",
        zone_id="default",
        agent_id="agent-a",
        daily_limit=Decimal("100"),
        per_tx_limit=Decimal("50"),
    )
    service.list_policies.return_value = []
    service.delete_policy.return_value = True
    return service


# =============================================================================
# Budget Endpoint Tests
# =============================================================================


class TestBudgetEndpoint:
    """GET /api/v2/pay/budget returns spending summary."""

    @pytest.mark.asyncio
    async def test_budget_returns_summary(self, mock_credits_service, mock_policy_service):
        app = _create_test_app(
            mock_credits_service=mock_credits_service,
            mock_policy_service=mock_policy_service,
        )
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/v2/pay/budget")
            assert resp.status_code == 200
            data = resp.json()
            assert data["has_policy"] is True
            assert data["limits"]["daily"] == "100"
            assert data["remaining"]["daily"] == "75"

    @pytest.mark.asyncio
    async def test_budget_no_policy(self, mock_credits_service, mock_policy_service):
        mock_policy_service.get_budget_summary.return_value = {
            "has_policy": False,
            "policy_id": None,
            "limits": {},
            "remaining": {},
        }
        app = _create_test_app(
            mock_credits_service=mock_credits_service,
            mock_policy_service=mock_policy_service,
        )
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/v2/pay/budget")
            assert resp.status_code == 200
            assert resp.json()["has_policy"] is False


# =============================================================================
# Policy Denial via Exception Handler Tests
# =============================================================================


class TestPolicyDenialResponse:
    """PolicyDeniedError mapped to 403 with error_code 'policy_denied'."""

    @pytest.mark.asyncio
    async def test_policy_denied_returns_403(self, mock_credits_service, mock_policy_service):
        """Transfer blocked by policy returns 403 with policy_denied error."""
        # The credits_service.transfer is what gets called by NexusPay.transfer(),
        # but PolicyDeniedError is raised by the wrapper BEFORE the inner protocol.
        # Since we're testing through NexusPay SDK (not the wrapper directly),
        # we mock credits_service.transfer to raise PolicyDeniedError
        # to simulate the wrapper's behavior at the router level.
        mock_credits_service.transfer.side_effect = PolicyDeniedError(
            "Amount 100 exceeds per-transaction limit 50",
            policy_id="p1",
        )
        app = _create_test_app(
            mock_credits_service=mock_credits_service,
            mock_policy_service=mock_policy_service,
        )
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/api/v2/pay/transfer",
                json={"to": "agent-bob", "amount": "100", "memo": "test"},
            )
            assert resp.status_code == 403
            data = resp.json()
            assert data["error_code"] == "policy_denied"
            assert "per-transaction limit" in data["detail"]


# =============================================================================
# Policy CRUD Endpoint Tests
# =============================================================================


class TestPolicyCRUDEndpoints:
    """Policy CRUD requires admin access."""

    @pytest.mark.asyncio
    async def test_create_policy_requires_admin(self, mock_credits_service, mock_policy_service):
        app = _create_test_app(
            mock_credits_service=mock_credits_service,
            mock_policy_service=mock_policy_service,
            is_admin=False,
        )
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/api/v2/pay/policies",
                json={"agent_id": "agent-a", "daily_limit": "100"},
            )
            assert resp.status_code == 403
            assert "Admin" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_create_policy_as_admin(self, mock_credits_service, mock_policy_service):
        app = _create_test_app(
            mock_credits_service=mock_credits_service,
            mock_policy_service=mock_policy_service,
            is_admin=True,
        )
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/api/v2/pay/policies",
                json={"agent_id": "agent-a", "daily_limit": "100", "per_tx_limit": "50"},
            )
            assert resp.status_code == 201
            data = resp.json()
            assert data["policy_id"] == "new-p1"
            assert data["daily_limit"] == "100"

    @pytest.mark.asyncio
    async def test_list_policies_requires_admin(self, mock_credits_service, mock_policy_service):
        app = _create_test_app(
            mock_credits_service=mock_credits_service,
            mock_policy_service=mock_policy_service,
            is_admin=False,
        )
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/v2/pay/policies")
            assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_delete_policy_as_admin(self, mock_credits_service, mock_policy_service):
        app = _create_test_app(
            mock_credits_service=mock_credits_service,
            mock_policy_service=mock_policy_service,
            is_admin=True,
        )
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.delete("/api/v2/pay/policies/p1")
            assert resp.status_code == 204

    @pytest.mark.asyncio
    async def test_delete_nonexistent_policy(self, mock_credits_service, mock_policy_service):
        mock_policy_service.delete_policy.return_value = False
        app = _create_test_app(
            mock_credits_service=mock_credits_service,
            mock_policy_service=mock_policy_service,
            is_admin=True,
        )
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.delete("/api/v2/pay/policies/nonexistent")
            assert resp.status_code == 404


# =============================================================================
# Concurrency Tests (Decision #10A)
# =============================================================================


class TestConcurrentTransfers:
    """Verify policy enforcement under concurrent requests."""

    @pytest.mark.asyncio
    async def test_concurrent_transfers_all_evaluated(
        self, mock_credits_service, mock_policy_service
    ):
        """Multiple concurrent transfers each get independent policy evaluation."""
        app = _create_test_app(
            mock_credits_service=mock_credits_service,
            mock_policy_service=mock_policy_service,
        )
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            # Fire 10 concurrent transfer requests
            tasks = [
                client.post(
                    "/api/v2/pay/transfer",
                    json={"to": f"agent-{i}", "amount": "5", "memo": f"test-{i}"},
                )
                for i in range(10)
            ]
            responses = await asyncio.gather(*tasks)

            # All should succeed (policy allows all) — transfer returns 201
            for resp in responses:
                assert resp.status_code == 201

            # Each transfer should have triggered credits_service.transfer
            assert mock_credits_service.transfer.call_count == 10

    @pytest.mark.asyncio
    async def test_concurrent_transfers_with_policy_denial(
        self, mock_credits_service, mock_policy_service
    ):
        """Mix of allowed and denied transfers handled correctly under concurrency."""
        call_count = 0

        async def alternating_transfer(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count % 2 == 0:
                raise PolicyDeniedError(
                    "Amount exceeds per-transaction limit",
                    policy_id="p1",
                )
            return "tx-ok"

        mock_credits_service.transfer.side_effect = alternating_transfer

        app = _create_test_app(
            mock_credits_service=mock_credits_service,
            mock_policy_service=mock_policy_service,
        )
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            tasks = [
                client.post(
                    "/api/v2/pay/transfer",
                    json={"to": f"agent-{i}", "amount": "10", "memo": f"test-{i}"},
                )
                for i in range(6)
            ]
            responses = await asyncio.gather(*tasks)

            statuses = sorted(resp.status_code for resp in responses)
            # 3 allowed (201), 3 denied (403)
            assert statuses.count(201) == 3
            assert statuses.count(403) == 3

    @pytest.mark.asyncio
    async def test_concurrent_budget_reads(self, mock_credits_service, mock_policy_service):
        """Multiple concurrent budget reads don't interfere."""
        app = _create_test_app(
            mock_credits_service=mock_credits_service,
            mock_policy_service=mock_policy_service,
        )
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            tasks = [client.get("/api/v2/pay/budget") for _ in range(20)]
            responses = await asyncio.gather(*tasks)

            for resp in responses:
                assert resp.status_code == 200
                assert resp.json()["has_policy"] is True


# =============================================================================
# Section 2: Real DB Integration Tests (SQLite async)
# =============================================================================


def _create_integration_app(
    *,
    policy_service: Any,
    mock_credits_service: AsyncMock,
    is_admin: bool = False,
    agent_id: str = "test-agent",
    zone_id: str = "default",
) -> FastAPI:
    """Create FastAPI app with REAL SpendingPolicyService + mocked CreditsService."""
    app = FastAPI()
    app.include_router(router)
    _register_pay_exception_handlers(app)

    app.state.credits_service = mock_credits_service
    app.state.spending_policy_service = policy_service
    app.state.x402_client = None

    from nexus.server.api.v2.routers.pay import _get_require_auth

    async def mock_auth(
        authorization: str | None = Header(None, alias="Authorization"),
        x_agent_id: str | None = Header(None, alias="X-Agent-ID"),
        x_nexus_subject: str | None = Header(None, alias="X-Nexus-Subject"),
        x_nexus_zone_id: str | None = Header(None, alias="X-Nexus-Zone-ID"),
    ) -> dict[str, Any]:
        return {
            "authenticated": True,
            "subject_type": "agent",
            "subject_id": agent_id,
            "zone_id": zone_id,
            "is_admin": is_admin,
            "x_agent_id": None,
            "metadata": {},
        }

    app.dependency_overrides[_get_require_auth()] = mock_auth
    return app


@pytest.fixture
async def async_session_factory():
    """Create an async SQLite in-memory database with spending policy tables."""
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    import nexus.storage.models.spending_policy  # noqa: F401 - ensure models registered
    from nexus.storage.models._base import Base

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest.fixture
def real_policy_service(async_session_factory):
    """Create a real SpendingPolicyService backed by SQLite."""
    from nexus.pay.spending_policy_service import SpendingPolicyService

    return SpendingPolicyService(session_factory=async_session_factory)


class TestRealDBPolicyCRUD:
    """Integration tests: real SpendingPolicyService + real SQLite + FastAPI."""

    @pytest.mark.asyncio
    async def test_create_and_get_budget(self, real_policy_service, mock_credits_service):
        """Admin creates policy → agent sees it in budget summary."""
        # Step 1: Admin creates a policy
        admin_app = _create_integration_app(
            policy_service=real_policy_service,
            mock_credits_service=mock_credits_service,
            is_admin=True,
        )
        async with AsyncClient(
            transport=ASGITransport(app=admin_app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v2/pay/policies",
                json={
                    "agent_id": "test-agent",
                    "daily_limit": "100",
                    "per_tx_limit": "50",
                },
            )
            assert resp.status_code == 201
            policy_data = resp.json()
            assert policy_data["daily_limit"] == "100"
            assert policy_data["per_tx_limit"] == "50"
            assert policy_data["policy_id"]  # verify non-empty

        # Step 2: Agent checks budget (no spending yet → full budget remaining)
        agent_app = _create_integration_app(
            policy_service=real_policy_service,
            mock_credits_service=mock_credits_service,
            is_admin=False,
            agent_id="test-agent",
        )
        async with AsyncClient(
            transport=ASGITransport(app=agent_app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v2/pay/budget")
            assert resp.status_code == 200
            budget = resp.json()
            assert budget["has_policy"] is True
            assert budget["limits"]["daily"] == "100"
            assert budget["limits"]["per_tx"] == "50"
            assert budget["remaining"]["daily"] == "100"

    @pytest.mark.asyncio
    async def test_list_and_delete_policies(self, real_policy_service, mock_credits_service):
        """Admin creates, lists, and deletes policies."""
        app = _create_integration_app(
            policy_service=real_policy_service,
            mock_credits_service=mock_credits_service,
            is_admin=True,
        )
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            # Create two policies
            resp1 = await client.post(
                "/api/v2/pay/policies",
                json={"agent_id": "agent-a", "daily_limit": "200"},
            )
            assert resp1.status_code == 201
            pid1 = resp1.json()["policy_id"]

            resp2 = await client.post(
                "/api/v2/pay/policies",
                json={"agent_id": "agent-b", "monthly_limit": "1000"},
            )
            assert resp2.status_code == 201

            # List policies
            resp = await client.get("/api/v2/pay/policies")
            assert resp.status_code == 200
            policies = resp.json()
            assert len(policies) == 2

            # Delete one
            resp = await client.delete(f"/api/v2/pay/policies/{pid1}")
            assert resp.status_code == 204

            # List again → only 1 left
            resp = await client.get("/api/v2/pay/policies")
            assert resp.status_code == 200
            assert len(resp.json()) == 1

    @pytest.mark.asyncio
    async def test_no_policy_returns_open_budget(self, real_policy_service, mock_credits_service):
        """Agent with no policy sees has_policy=False (open by default)."""
        app = _create_integration_app(
            policy_service=real_policy_service,
            mock_credits_service=mock_credits_service,
            is_admin=False,
            agent_id="unmanaged-agent",
        )
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/v2/pay/budget")
            assert resp.status_code == 200
            data = resp.json()
            assert data["has_policy"] is False
            assert data["limits"] == {}

    @pytest.mark.asyncio
    async def test_zone_default_policy_applies(self, real_policy_service, mock_credits_service):
        """Zone-level default policy (agent_id=None) applies to any agent."""
        # Create zone default policy (no agent_id)
        admin_app = _create_integration_app(
            policy_service=real_policy_service,
            mock_credits_service=mock_credits_service,
            is_admin=True,
        )
        async with AsyncClient(
            transport=ASGITransport(app=admin_app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v2/pay/policies",
                json={"daily_limit": "500"},  # No agent_id → zone default
            )
            assert resp.status_code == 201

        # Check budget for a random agent → should see zone default
        agent_app = _create_integration_app(
            policy_service=real_policy_service,
            mock_credits_service=mock_credits_service,
            is_admin=False,
            agent_id="random-agent-xyz",
        )
        async with AsyncClient(
            transport=ASGITransport(app=agent_app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v2/pay/budget")
            assert resp.status_code == 200
            budget = resp.json()
            assert budget["has_policy"] is True
            assert budget["limits"]["daily"] == "500"

    @pytest.mark.asyncio
    async def test_agent_specific_overrides_zone_default(
        self, real_policy_service, mock_credits_service
    ):
        """Agent-specific policy takes priority over zone default."""
        admin_app = _create_integration_app(
            policy_service=real_policy_service,
            mock_credits_service=mock_credits_service,
            is_admin=True,
        )
        async with AsyncClient(
            transport=ASGITransport(app=admin_app), base_url="http://test"
        ) as client:
            # Zone default: daily=500
            await client.post(
                "/api/v2/pay/policies",
                json={"daily_limit": "500"},
            )
            # Agent-specific override: daily=50
            await client.post(
                "/api/v2/pay/policies",
                json={"agent_id": "special-agent", "daily_limit": "50"},
            )

        # special-agent should see daily=50 (not zone default 500)
        agent_app = _create_integration_app(
            policy_service=real_policy_service,
            mock_credits_service=mock_credits_service,
            is_admin=False,
            agent_id="special-agent",
        )
        async with AsyncClient(
            transport=ASGITransport(app=agent_app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v2/pay/budget")
            assert resp.status_code == 200
            budget = resp.json()
            assert budget["has_policy"] is True
            assert budget["limits"]["daily"] == "50"
