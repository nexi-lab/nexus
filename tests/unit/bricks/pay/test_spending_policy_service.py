"""Unit tests for SpendingPolicyService with mock repository.

Issue #2189: Tests service logic in isolation from database.
Mock repository verifies that service delegates correctly and manages
caching, rate limits, and evaluation independently.
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from nexus.bricks.pay.spending_policy import (
    SpendingApproval,
    SpendingPolicy,
)
from nexus.bricks.pay.spending_policy_service import SpendingPolicyService

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_policy(**overrides) -> SpendingPolicy:
    """Create a SpendingPolicy with sensible defaults."""
    defaults = {
        "policy_id": "pol-1",
        "zone_id": "root",
        "agent_id": "agent-a",
        "daily_limit": Decimal("100"),
        "weekly_limit": None,
        "monthly_limit": None,
        "per_tx_limit": Decimal("50"),
        "auto_approve_threshold": None,
        "max_tx_per_hour": None,
        "max_tx_per_day": None,
        "rules": None,
        "priority": 0,
        "enabled": True,
    }
    defaults.update(overrides)
    return SpendingPolicy(**defaults)


def _make_approval(**overrides) -> SpendingApproval:
    defaults = {
        "approval_id": "apr-1",
        "policy_id": "pol-1",
        "agent_id": "agent-a",
        "zone_id": "root",
        "amount": Decimal("100"),
        "to": "bob",
        "memo": "",
        "status": "pending",
        "requested_at": "2025-01-01T00:00:00+00:00",
        "decided_at": None,
        "decided_by": None,
        "expires_at": "2025-01-02T00:00:00+00:00",
    }
    defaults.update(overrides)
    return SpendingApproval(**defaults)


def _mock_repo() -> AsyncMock:
    """Create a mock repository satisfying SpendingPolicyRepository."""
    repo = AsyncMock()
    repo.resolve_policy = AsyncMock(return_value=None)
    repo.get_spending = AsyncMock(return_value={})
    repo.record_spending = AsyncMock(return_value=None)
    repo.create_policy = AsyncMock()
    repo.get_policy = AsyncMock(return_value=None)
    repo.update_policy = AsyncMock(return_value=(None, None))
    repo.delete_policy = AsyncMock(return_value=(False, None))
    repo.list_policies = AsyncMock(return_value=[])
    repo.create_approval = AsyncMock()
    repo.check_approval = AsyncMock(return_value=None)
    repo.decide_approval = AsyncMock(return_value=None)
    repo.list_pending_approvals = AsyncMock(return_value=[])
    return repo


@pytest.fixture
def repo():
    return _mock_repo()


@pytest.fixture
def service(repo):
    return SpendingPolicyService(repo=repo, cache_ttl=0.1)


# ===========================================================================
# Evaluate — open by default
# ===========================================================================


class TestEvaluateOpenByDefault:
    """No policy → allow all transactions."""

    @pytest.mark.asyncio
    async def test_no_policy_allows(self, service, repo):
        repo.resolve_policy.return_value = None
        result = await service.evaluate("agent-a", "root", Decimal("999"))
        assert result.allowed is True
        assert result.policy_id is None

    @pytest.mark.asyncio
    async def test_disabled_policy_allows(self, service, repo):
        repo.resolve_policy.return_value = _make_policy(enabled=False)
        result = await service.evaluate("agent-a", "root", Decimal("999"))
        assert result.allowed is True


# ===========================================================================
# Evaluate — per-tx limit
# ===========================================================================


class TestEvaluatePerTxLimit:
    @pytest.mark.asyncio
    async def test_within_per_tx_limit(self, service, repo):
        repo.resolve_policy.return_value = _make_policy(per_tx_limit=Decimal("50"))
        repo.get_spending.return_value = {"daily": Decimal("0")}
        result = await service.evaluate("agent-a", "root", Decimal("49"))
        assert result.allowed is True

    @pytest.mark.asyncio
    async def test_exceeds_per_tx_limit(self, service, repo):
        repo.resolve_policy.return_value = _make_policy(per_tx_limit=Decimal("50"))
        result = await service.evaluate("agent-a", "root", Decimal("51"))
        assert result.allowed is False
        assert "per-transaction limit" in result.denied_reason


# ===========================================================================
# Evaluate — period-based budget limits
# ===========================================================================


class TestEvaluateBudgetLimits:
    @pytest.mark.asyncio
    async def test_within_daily_limit(self, service, repo):
        repo.resolve_policy.return_value = _make_policy(
            daily_limit=Decimal("100"), per_tx_limit=None
        )
        repo.get_spending.return_value = {"daily": Decimal("50")}
        result = await service.evaluate("agent-a", "root", Decimal("49"))
        assert result.allowed is True
        assert result.remaining_budget["daily"] == Decimal("50")

    @pytest.mark.asyncio
    async def test_exceeds_daily_limit(self, service, repo):
        repo.resolve_policy.return_value = _make_policy(
            daily_limit=Decimal("100"), per_tx_limit=None
        )
        repo.get_spending.return_value = {"daily": Decimal("90")}
        result = await service.evaluate("agent-a", "root", Decimal("20"))
        assert result.allowed is False
        assert "daily limit" in result.denied_reason

    @pytest.mark.asyncio
    async def test_weekly_limit_check(self, service, repo):
        repo.resolve_policy.return_value = _make_policy(
            daily_limit=None, weekly_limit=Decimal("500"), per_tx_limit=None
        )
        repo.get_spending.return_value = {"weekly": Decimal("490")}
        result = await service.evaluate("agent-a", "root", Decimal("20"))
        assert result.allowed is False
        assert "weekly limit" in result.denied_reason

    @pytest.mark.asyncio
    async def test_monthly_limit_check(self, service, repo):
        repo.resolve_policy.return_value = _make_policy(
            daily_limit=None, monthly_limit=Decimal("1000"), per_tx_limit=None
        )
        repo.get_spending.return_value = {"monthly": Decimal("995")}
        result = await service.evaluate("agent-a", "root", Decimal("10"))
        assert result.allowed is False
        assert "monthly limit" in result.denied_reason


# ===========================================================================
# Evaluate — rate limits
# ===========================================================================


class TestEvaluateRateLimits:
    @pytest.mark.asyncio
    async def test_hourly_rate_limit(self, service, repo):
        repo.resolve_policy.return_value = _make_policy(
            max_tx_per_hour=2, per_tx_limit=None, daily_limit=None
        )
        repo.get_spending.return_value = {}
        # Simulate 2 prior transactions
        service.record_rate_limit_hit("agent-a", "root")
        service.record_rate_limit_hit("agent-a", "root")
        result = await service.evaluate("agent-a", "root", Decimal("1"))
        assert result.allowed is False
        assert "Hourly" in result.denied_reason

    @pytest.mark.asyncio
    async def test_daily_rate_limit(self, service, repo):
        repo.resolve_policy.return_value = _make_policy(
            max_tx_per_day=3, per_tx_limit=None, daily_limit=None
        )
        repo.get_spending.return_value = {}
        # First evaluate initializes _daily_tx_date; record spending after
        await service.evaluate("agent-a", "root", Decimal("1"))
        for _ in range(3):
            service.record_rate_limit_hit("agent-a", "root")
        result = await service.evaluate("agent-a", "root", Decimal("1"))
        assert result.allowed is False
        assert "Daily" in result.denied_reason


# ===========================================================================
# Evaluate — approval threshold
# ===========================================================================


class TestEvaluateApprovalThreshold:
    @pytest.mark.asyncio
    async def test_below_threshold_allowed(self, service, repo):
        repo.resolve_policy.return_value = _make_policy(
            auto_approve_threshold=Decimal("50"),
            per_tx_limit=None,
            daily_limit=None,
        )
        repo.get_spending.return_value = {}
        result = await service.evaluate("agent-a", "root", Decimal("49"))
        assert result.allowed is True

    @pytest.mark.asyncio
    async def test_above_threshold_requires_approval(self, service, repo):
        repo.resolve_policy.return_value = _make_policy(
            auto_approve_threshold=Decimal("50"),
            per_tx_limit=None,
            daily_limit=None,
        )
        repo.get_spending.return_value = {}
        result = await service.evaluate("agent-a", "root", Decimal("51"))
        assert result.allowed is False
        assert result.requires_approval is True
        assert "auto-approve threshold" in result.denied_reason


# ===========================================================================
# Caching
# ===========================================================================


class TestPolicyCache:
    @pytest.mark.asyncio
    async def test_cache_avoids_second_repo_call(self, service, repo):
        policy = _make_policy()
        repo.resolve_policy.return_value = policy
        repo.get_spending.return_value = {}
        await service.evaluate("agent-a", "root", Decimal("1"))
        await service.evaluate("agent-a", "root", Decimal("1"))
        # resolve_policy called only once due to cache
        assert repo.resolve_policy.call_count == 1

    @pytest.mark.asyncio
    async def test_cache_invalidated_on_create(self, service, repo):
        policy = _make_policy()
        repo.resolve_policy.return_value = policy
        repo.create_policy.return_value = policy
        repo.get_spending.return_value = {}
        # Populate cache
        await service.evaluate("agent-a", "root", Decimal("1"))
        # Create invalidates cache
        await service.create_policy(zone_id="root", agent_id="agent-a")
        await service.evaluate("agent-a", "root", Decimal("1"))
        assert repo.resolve_policy.call_count == 2

    @pytest.mark.asyncio
    async def test_clear_cache(self, service, repo):
        repo.resolve_policy.return_value = _make_policy()
        repo.get_spending.return_value = {}
        await service.evaluate("agent-a", "root", Decimal("1"))
        service.clear_cache()
        await service.evaluate("agent-a", "root", Decimal("1"))
        assert repo.resolve_policy.call_count == 2


# ===========================================================================
# CRUD delegation
# ===========================================================================


class TestCRUDDelegation:
    @pytest.mark.asyncio
    async def test_create_policy_delegates(self, service, repo):
        policy = _make_policy()
        repo.create_policy.return_value = policy
        result = await service.create_policy(zone_id="root", agent_id="agent-a")
        assert result == policy
        repo.create_policy.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_get_policy_delegates(self, service, repo):
        policy = _make_policy()
        repo.get_policy.return_value = policy
        result = await service.get_policy("agent-a", "root")
        assert result == policy
        repo.get_policy.assert_awaited_once_with("agent-a", "root")

    @pytest.mark.asyncio
    async def test_update_policy_delegates(self, service, repo):
        policy = _make_policy()
        repo.update_policy.return_value = (policy, ("agent-a", "root"))
        result = await service.update_policy("pol-1", daily_limit=Decimal("200"))
        assert result == policy
        repo.update_policy.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_delete_policy_delegates(self, service, repo):
        repo.delete_policy.return_value = (True, ("agent-a", "root"))
        result = await service.delete_policy("pol-1")
        assert result is True
        repo.delete_policy.assert_awaited_once_with("pol-1")

    @pytest.mark.asyncio
    async def test_list_policies_delegates(self, service, repo):
        repo.list_policies.return_value = [_make_policy()]
        result = await service.list_policies("root")
        assert len(result) == 1
        repo.list_policies.assert_awaited_once_with("root")


# ===========================================================================
# Record spending
# ===========================================================================


class TestRecordSpending:
    @pytest.mark.asyncio
    async def test_record_spending_delegates_and_updates_rate_limits(self, service, repo):
        await service.record_spending("agent-a", "root", Decimal("10"))
        repo.record_spending.assert_awaited_once_with("agent-a", "root", Decimal("10"))
        # Rate limit counter should have been incremented
        key = ("agent-a", "root")
        assert service._daily_tx_counts.get(key, 0) == 1


# ===========================================================================
# Budget summary
# ===========================================================================


class TestBudgetSummary:
    @pytest.mark.asyncio
    async def test_no_policy_summary(self, service, repo):
        repo.resolve_policy.return_value = None
        result = await service.get_budget_summary("agent-a", "root")
        assert result["has_policy"] is False

    @pytest.mark.asyncio
    async def test_with_policy_summary(self, service, repo):
        repo.resolve_policy.return_value = _make_policy(
            daily_limit=Decimal("100"),
            per_tx_limit=Decimal("50"),
            auto_approve_threshold=Decimal("25"),
            max_tx_per_hour=10,
        )
        repo.get_spending.return_value = {"daily": Decimal("30")}
        result = await service.get_budget_summary("agent-a", "root")
        assert result["has_policy"] is True
        assert result["limits"]["daily"] == "100"
        assert result["limits"]["per_tx"] == "50"
        assert result["limits"]["auto_approve"] == "25"
        assert result["remaining"]["daily"] == "70"
        assert result["rate_limits"]["max_tx_per_hour"] == 10


# ===========================================================================
# Approvals delegation
# ===========================================================================


class TestApprovalsDelegation:
    @pytest.mark.asyncio
    async def test_request_approval_delegates(self, service, repo):
        approval = _make_approval()
        repo.create_approval.return_value = approval
        result = await service.request_approval(
            policy_id="pol-1",
            agent_id="agent-a",
            zone_id="root",
            amount=Decimal("100"),
            to="bob",
        )
        assert result == approval
        repo.create_approval.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_approve_request_delegates(self, service, repo):
        approval = _make_approval(status="approved")
        repo.decide_approval.return_value = approval
        result = await service.approve_request("apr-1", "admin-1")
        assert result == approval
        repo.decide_approval.assert_awaited_once_with("apr-1", "approved", "admin-1")

    @pytest.mark.asyncio
    async def test_reject_request_delegates(self, service, repo):
        approval = _make_approval(status="rejected")
        repo.decide_approval.return_value = approval
        result = await service.reject_request("apr-1", "admin-1")
        assert result == approval
        repo.decide_approval.assert_awaited_once_with("apr-1", "rejected", "admin-1")

    @pytest.mark.asyncio
    async def test_list_pending_approvals_delegates(self, service, repo):
        repo.list_pending_approvals.return_value = [_make_approval()]
        result = await service.list_pending_approvals("root")
        assert len(result) == 1
        repo.list_pending_approvals.assert_awaited_once_with("root")
