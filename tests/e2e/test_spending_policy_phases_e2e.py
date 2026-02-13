"""E2E tests for Spending Policy Phases 2-4 through FastAPI.

Issue #1358:
- Phase 2: Approval workflows (request, approve, reject, list)
- Phase 3: Rate limits (max_tx_per_hour, max_tx_per_day)
- Phase 4: DSL rules (recipient, time window, amount range)

All tests use real SpendingPolicyService + SQLite async backend.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, Header
from httpx import ASGITransport, AsyncClient

from nexus.pay.spending_policy import (
    ApprovalRequiredError,
    SpendingRateLimitError,
)
from nexus.server.api.v2.routers.pay import _register_pay_exception_handlers, router

# =============================================================================
# Fixtures
# =============================================================================


def _create_app(
    *,
    policy_service: Any,
    mock_credits_service: AsyncMock,
    is_admin: bool = False,
    agent_id: str = "test-agent",
    zone_id: str = "default",
) -> FastAPI:
    """Create FastAPI app with real policy service + mocked credits."""
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
def mock_credits_service():
    service = AsyncMock()
    service.get_balance.return_value = Decimal("1000.0")
    service.get_balance_with_reserved.return_value = (Decimal("1000.0"), Decimal("0"))
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
async def async_session_factory():
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    import nexus.storage.models.spending_policy  # noqa: F401
    from nexus.storage.models._base import Base

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest.fixture
def policy_service(async_session_factory):
    from nexus.pay.spending_policy_service import SpendingPolicyService

    return SpendingPolicyService(session_factory=async_session_factory)


# =============================================================================
# Phase 2: Approval Workflow E2E Tests
# =============================================================================


class TestApprovalWorkflow:
    """Full approval lifecycle: create policy → request approval → approve → list."""

    @pytest.mark.asyncio
    async def test_approval_required_exception_returns_402(self, mock_credits_service):
        """ApprovalRequiredError mapped to HTTP 402."""
        mock_credits_service.transfer.side_effect = ApprovalRequiredError(
            "Amount exceeds auto-approve threshold",
            policy_id="p1",
        )
        from nexus.server.api.v2.routers.pay import _get_require_auth

        app = FastAPI()
        app.include_router(router)
        _register_pay_exception_handlers(app)
        app.state.credits_service = mock_credits_service
        app.state.spending_policy_service = AsyncMock()
        app.state.x402_client = None

        async def mock_auth(
            authorization: str | None = Header(None, alias="Authorization"),
            x_agent_id: str | None = Header(None, alias="X-Agent-ID"),
            x_nexus_subject: str | None = Header(None, alias="X-Nexus-Subject"),
            x_nexus_zone_id: str | None = Header(None, alias="X-Nexus-Zone-ID"),
        ) -> dict[str, Any]:
            return {
                "authenticated": True,
                "subject_type": "agent",
                "subject_id": "test-agent",
                "zone_id": "default",
                "is_admin": False,
                "x_agent_id": None,
                "metadata": {},
            }

        app.dependency_overrides[_get_require_auth()] = mock_auth

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/api/v2/pay/transfer",
                json={"to": "bob", "amount": "100", "memo": "test"},
            )
            assert resp.status_code == 402
            assert resp.json()["error_code"] == "approval_required"

    @pytest.mark.asyncio
    async def test_rate_limit_exception_returns_429(self, mock_credits_service):
        """SpendingRateLimitError mapped to HTTP 429."""
        mock_credits_service.transfer.side_effect = SpendingRateLimitError(
            "Hourly limit exceeded",
            policy_id="p1",
            limit_type="hourly",
        )
        from nexus.server.api.v2.routers.pay import _get_require_auth

        app = FastAPI()
        app.include_router(router)
        _register_pay_exception_handlers(app)
        app.state.credits_service = mock_credits_service
        app.state.spending_policy_service = AsyncMock()
        app.state.x402_client = None

        async def mock_auth(
            authorization: str | None = Header(None, alias="Authorization"),
            x_agent_id: str | None = Header(None, alias="X-Agent-ID"),
            x_nexus_subject: str | None = Header(None, alias="X-Nexus-Subject"),
            x_nexus_zone_id: str | None = Header(None, alias="X-Nexus-Zone-ID"),
        ) -> dict[str, Any]:
            return {
                "authenticated": True,
                "subject_type": "agent",
                "subject_id": "test-agent",
                "zone_id": "default",
                "is_admin": False,
                "x_agent_id": None,
                "metadata": {},
            }

        app.dependency_overrides[_get_require_auth()] = mock_auth

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/api/v2/pay/transfer",
                json={"to": "bob", "amount": "10", "memo": "test"},
            )
            assert resp.status_code == 429
            assert resp.json()["error_code"] == "rate_limit_exceeded"

    @pytest.mark.asyncio
    async def test_full_approval_lifecycle(self, policy_service, mock_credits_service):
        """Admin creates policy → agent requests approval → admin approves → list."""
        # 1. Admin creates policy with auto_approve_threshold
        admin_app = _create_app(
            policy_service=policy_service,
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
                    "daily_limit": "1000",
                    "auto_approve_threshold": "50",
                },
            )
            assert resp.status_code == 201
            assert resp.json()["auto_approve_threshold"] == "50"

        # 2. Agent requests approval
        agent_app = _create_app(
            policy_service=policy_service,
            mock_credits_service=mock_credits_service,
            is_admin=False,
            agent_id="test-agent",
        )
        async with AsyncClient(
            transport=ASGITransport(app=agent_app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v2/pay/approvals",
                json={"amount": "75", "to": "vendor-x", "memo": "large purchase"},
            )
            assert resp.status_code == 201
            approval = resp.json()
            assert approval["status"] == "pending"
            assert approval["amount"] == "75"
            approval_id = approval["approval_id"]

        # 3. Admin lists pending approvals
        async with AsyncClient(
            transport=ASGITransport(app=admin_app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v2/pay/approvals")
            assert resp.status_code == 200
            pending = resp.json()
            assert len(pending) == 1
            assert pending[0]["approval_id"] == approval_id

        # 4. Admin approves
        async with AsyncClient(
            transport=ASGITransport(app=admin_app), base_url="http://test"
        ) as client:
            resp = await client.post(f"/api/v2/pay/approvals/{approval_id}/approve")
            assert resp.status_code == 200
            assert resp.json()["status"] == "approved"

        # 5. Pending list now empty
        async with AsyncClient(
            transport=ASGITransport(app=admin_app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v2/pay/approvals")
            assert resp.status_code == 200
            assert len(resp.json()) == 0

    @pytest.mark.asyncio
    async def test_reject_approval(self, policy_service, mock_credits_service):
        """Admin rejects an approval request."""
        admin_app = _create_app(
            policy_service=policy_service,
            mock_credits_service=mock_credits_service,
            is_admin=True,
        )
        # Create policy + request approval
        async with AsyncClient(
            transport=ASGITransport(app=admin_app), base_url="http://test"
        ) as client:
            await client.post(
                "/api/v2/pay/policies",
                json={"agent_id": "test-agent", "auto_approve_threshold": "10"},
            )

        agent_app = _create_app(
            policy_service=policy_service,
            mock_credits_service=mock_credits_service,
            is_admin=False,
            agent_id="test-agent",
        )
        async with AsyncClient(
            transport=ASGITransport(app=agent_app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v2/pay/approvals",
                json={"amount": "50", "to": "vendor"},
            )
            approval_id = resp.json()["approval_id"]

        # Admin rejects
        async with AsyncClient(
            transport=ASGITransport(app=admin_app), base_url="http://test"
        ) as client:
            resp = await client.post(f"/api/v2/pay/approvals/{approval_id}/reject")
            assert resp.status_code == 200
            assert resp.json()["status"] == "rejected"

    @pytest.mark.asyncio
    async def test_approval_endpoints_require_admin(self, policy_service, mock_credits_service):
        """List/approve/reject require admin."""
        agent_app = _create_app(
            policy_service=policy_service,
            mock_credits_service=mock_credits_service,
            is_admin=False,
        )
        async with AsyncClient(
            transport=ASGITransport(app=agent_app), base_url="http://test"
        ) as client:
            assert (await client.get("/api/v2/pay/approvals")).status_code == 403
            assert (await client.post("/api/v2/pay/approvals/x/approve")).status_code == 403
            assert (await client.post("/api/v2/pay/approvals/x/reject")).status_code == 403


# =============================================================================
# Phase 3: Rate Limit E2E Tests
# =============================================================================


class TestRateLimits:
    """Policy with rate limits creates policies with rate fields."""

    @pytest.mark.asyncio
    async def test_create_policy_with_rate_limits(self, policy_service, mock_credits_service):
        """Admin creates policy with rate limit fields."""
        app = _create_app(
            policy_service=policy_service,
            mock_credits_service=mock_credits_service,
            is_admin=True,
        )
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/api/v2/pay/policies",
                json={
                    "agent_id": "rate-agent",
                    "max_tx_per_hour": 10,
                    "max_tx_per_day": 50,
                    "daily_limit": "500",
                },
            )
            assert resp.status_code == 201
            data = resp.json()
            assert data["max_tx_per_hour"] == 10
            assert data["max_tx_per_day"] == 50

    @pytest.mark.asyncio
    async def test_budget_shows_rate_limits(self, policy_service, mock_credits_service):
        """Budget summary includes rate limit info."""
        admin_app = _create_app(
            policy_service=policy_service,
            mock_credits_service=mock_credits_service,
            is_admin=True,
        )
        async with AsyncClient(
            transport=ASGITransport(app=admin_app), base_url="http://test"
        ) as client:
            await client.post(
                "/api/v2/pay/policies",
                json={
                    "agent_id": "test-agent",
                    "max_tx_per_hour": 5,
                    "max_tx_per_day": 20,
                },
            )

        agent_app = _create_app(
            policy_service=policy_service,
            mock_credits_service=mock_credits_service,
            is_admin=False,
            agent_id="test-agent",
        )
        async with AsyncClient(
            transport=ASGITransport(app=agent_app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v2/pay/budget")
            assert resp.status_code == 200
            data = resp.json()
            assert data["rate_limits"]["max_tx_per_hour"] == 5
            assert data["rate_limits"]["max_tx_per_day"] == 20


# =============================================================================
# Phase 4: DSL Rules E2E Tests
# =============================================================================


class TestDSLRules:
    """Policy with JSON rules creates and returns rules correctly."""

    @pytest.mark.asyncio
    async def test_create_policy_with_rules(self, policy_service, mock_credits_service):
        """Admin creates policy with DSL rules."""
        app = _create_app(
            policy_service=policy_service,
            mock_credits_service=mock_credits_service,
            is_admin=True,
        )
        rules = [
            {"type": "recipient_blocklist", "recipients": ["banned-agent"]},
            {"type": "amount_range", "min": "1", "max": "500"},
        ]
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/api/v2/pay/policies",
                json={
                    "agent_id": "rules-agent",
                    "daily_limit": "1000",
                    "rules": rules,
                },
            )
            assert resp.status_code == 201
            data = resp.json()
            assert data["rules"] == rules

    @pytest.mark.asyncio
    async def test_budget_shows_has_rules(self, policy_service, mock_credits_service):
        """Budget summary shows has_rules=True when rules configured."""
        admin_app = _create_app(
            policy_service=policy_service,
            mock_credits_service=mock_credits_service,
            is_admin=True,
        )
        async with AsyncClient(
            transport=ASGITransport(app=admin_app), base_url="http://test"
        ) as client:
            await client.post(
                "/api/v2/pay/policies",
                json={
                    "agent_id": "test-agent",
                    "rules": [{"type": "recipient_blocklist", "recipients": ["x"]}],
                },
            )

        agent_app = _create_app(
            policy_service=policy_service,
            mock_credits_service=mock_credits_service,
            is_admin=False,
            agent_id="test-agent",
        )
        async with AsyncClient(
            transport=ASGITransport(app=agent_app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v2/pay/budget")
            assert resp.status_code == 200
            assert resp.json()["has_rules"] is True

    @pytest.mark.asyncio
    async def test_list_policies_includes_all_fields(self, policy_service, mock_credits_service):
        """List policies returns all phase 2-4 fields."""
        app = _create_app(
            policy_service=policy_service,
            mock_credits_service=mock_credits_service,
            is_admin=True,
        )
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            await client.post(
                "/api/v2/pay/policies",
                json={
                    "agent_id": "full-agent",
                    "daily_limit": "100",
                    "auto_approve_threshold": "25",
                    "max_tx_per_hour": 3,
                    "max_tx_per_day": 10,
                    "rules": [{"type": "amount_range", "max": "50"}],
                },
            )
            resp = await client.get("/api/v2/pay/policies")
            assert resp.status_code == 200
            policies = resp.json()
            assert len(policies) == 1
            p = policies[0]
            assert p["daily_limit"] == "100"
            assert p["auto_approve_threshold"] == "25"
            assert p["max_tx_per_hour"] == 3
            assert p["max_tx_per_day"] == 10
            assert len(p["rules"]) == 1
