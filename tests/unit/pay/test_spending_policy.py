"""Tests for spending policy data models and evaluation logic.

Issue #1358: Agent Spending Policy Engine — Phase 1.

Test categories:
1. Data model construction and immutability
2. Exception hierarchy
3. Policy evaluation via SpendingPolicyService.evaluate()
4. Policy resolution (agent-specific → zone default → no policy)
5. Period-based spending checks
6. Edge cases
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from nexus.pay.sdk import NexusPayError
from nexus.pay.spending_policy import (
    ApprovalRequiredError,
    PolicyDeniedError,
    PolicyError,
    PolicyEvaluation,
    SpendingLedgerEntry,
    SpendingPolicy,
    SpendingRateLimitError,
)

# =============================================================================
# 1. Data Model Tests
# =============================================================================


class TestSpendingPolicy:
    """SpendingPolicy frozen dataclass tests."""

    def test_create_minimal(self):
        policy = SpendingPolicy(policy_id="p1", zone_id="default")
        assert policy.policy_id == "p1"
        assert policy.zone_id == "default"
        assert policy.agent_id is None
        assert policy.daily_limit is None
        assert policy.per_tx_limit is None
        assert policy.priority == 0
        assert policy.enabled is True

    def test_create_full(self):
        policy = SpendingPolicy(
            policy_id="p2",
            zone_id="zone-1",
            agent_id="agent-a",
            daily_limit=Decimal("100"),
            weekly_limit=Decimal("500"),
            monthly_limit=Decimal("2000"),
            per_tx_limit=Decimal("50"),
            auto_approve_threshold=Decimal("10"),
            priority=5,
            enabled=True,
        )
        assert policy.agent_id == "agent-a"
        assert policy.daily_limit == Decimal("100")
        assert policy.weekly_limit == Decimal("500")
        assert policy.monthly_limit == Decimal("2000")
        assert policy.per_tx_limit == Decimal("50")
        assert policy.priority == 5

    def test_frozen(self):
        policy = SpendingPolicy(policy_id="p1", zone_id="default")
        with pytest.raises(AttributeError):
            policy.daily_limit = Decimal("100")  # type: ignore[misc]


class TestSpendingLedgerEntry:
    """SpendingLedgerEntry frozen dataclass tests."""

    def test_create_with_defaults(self):
        entry = SpendingLedgerEntry(
            agent_id="agent-a",
            zone_id="default",
            period_type="daily",
            period_start=date.today(),
        )
        assert entry.amount_spent == Decimal("0")
        assert entry.tx_count == 0

    def test_frozen(self):
        entry = SpendingLedgerEntry(
            agent_id="a", zone_id="z", period_type="daily", period_start=date.today()
        )
        with pytest.raises(AttributeError):
            entry.amount_spent = Decimal("10")  # type: ignore[misc]


class TestPolicyEvaluation:
    """PolicyEvaluation frozen dataclass tests."""

    def test_allowed(self):
        ev = PolicyEvaluation(allowed=True, policy_id="p1")
        assert ev.allowed is True
        assert ev.denied_reason is None

    def test_denied(self):
        ev = PolicyEvaluation(
            allowed=False,
            denied_reason="Over daily limit",
            policy_id="p1",
        )
        assert ev.allowed is False
        assert ev.denied_reason == "Over daily limit"

    def test_remaining_budget(self):
        ev = PolicyEvaluation(
            allowed=True,
            remaining_budget={"daily": Decimal("42.50"), "monthly": Decimal("1500")},
        )
        assert ev.remaining_budget["daily"] == Decimal("42.50")


# =============================================================================
# 2. Exception Hierarchy Tests
# =============================================================================


class TestExceptionHierarchy:
    """Verify exception inheritance chain."""

    def test_policy_error_is_nexuspay_error(self):
        assert issubclass(PolicyError, NexusPayError)

    def test_policy_denied_is_policy_error(self):
        assert issubclass(PolicyDeniedError, PolicyError)

    def test_approval_required_is_policy_error(self):
        assert issubclass(ApprovalRequiredError, PolicyError)

    def test_rate_limit_is_policy_error(self):
        assert issubclass(SpendingRateLimitError, PolicyError)

    def test_policy_denied_has_policy_id(self):
        err = PolicyDeniedError("Over limit", policy_id="p1")
        assert err.policy_id == "p1"
        assert err.denied_reason == "Over limit"
        assert str(err) == "Over limit"

    def test_catch_all_policy_errors(self):
        """All policy exceptions can be caught with `except PolicyError`."""
        for exc_cls in (PolicyDeniedError, ApprovalRequiredError, SpendingRateLimitError):
            with pytest.raises(PolicyError):
                raise exc_cls("test")


# =============================================================================
# 3. Policy Evaluation Tests (via SpendingPolicyService)
# =============================================================================


class TestPolicyEvaluationLogic:
    """Test evaluate() method on SpendingPolicyService."""

    @pytest.fixture
    def mock_session_factory(self):
        """Mock async session factory."""
        session = AsyncMock()
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)
        session.begin = MagicMock(
            return_value=AsyncMock(
                __aenter__=AsyncMock(),
                __aexit__=AsyncMock(return_value=False),
            )
        )
        session.execute = AsyncMock()

        factory = MagicMock()
        factory.return_value = session
        factory.return_value.__aenter__ = AsyncMock(return_value=session)
        factory.return_value.__aexit__ = AsyncMock(return_value=False)
        return factory

    @pytest.fixture
    def service(self, mock_session_factory):
        from nexus.pay.spending_policy_service import SpendingPolicyService

        return SpendingPolicyService(session_factory=mock_session_factory)

    @pytest.mark.asyncio
    async def test_no_policy_allows_transfer(self, service):
        """No policy = open by default."""
        # Prepopulate cache with no policy
        import time

        service._cache[("agent-a", "default")] = (None, time.monotonic() + 60)

        result = await service.evaluate("agent-a", "default", Decimal("1000"))
        assert result.allowed is True
        assert result.policy_id is None

    @pytest.mark.asyncio
    async def test_per_tx_limit_exceeded(self, service):
        """Per-tx limit blocks single large transaction."""
        import time

        policy = SpendingPolicy(
            policy_id="p1",
            zone_id="default",
            agent_id="agent-a",
            per_tx_limit=Decimal("10"),
        )
        service._cache[("agent-a", "default")] = (policy, time.monotonic() + 60)

        result = await service.evaluate("agent-a", "default", Decimal("15"))
        assert result.allowed is False
        assert "per-transaction limit" in result.denied_reason
        assert result.policy_id == "p1"

    @pytest.mark.asyncio
    async def test_per_tx_limit_within_budget(self, service):
        """Transfer within per-tx limit is allowed."""
        import time

        policy = SpendingPolicy(
            policy_id="p1",
            zone_id="default",
            agent_id="agent-a",
            per_tx_limit=Decimal("10"),
        )
        service._cache[("agent-a", "default")] = (policy, time.monotonic() + 60)

        # Mock _get_spending to return zero
        service._get_spending = AsyncMock(
            return_value={"daily": Decimal("0"), "weekly": Decimal("0"), "monthly": Decimal("0")}
        )

        result = await service.evaluate("agent-a", "default", Decimal("5"))
        assert result.allowed is True

    @pytest.mark.asyncio
    async def test_daily_limit_exceeded(self, service):
        """Daily limit blocks when cumulative spend exceeds limit."""
        import time

        policy = SpendingPolicy(
            policy_id="p1",
            zone_id="default",
            agent_id="agent-a",
            daily_limit=Decimal("100"),
        )
        service._cache[("agent-a", "default")] = (policy, time.monotonic() + 60)
        service._get_spending = AsyncMock(
            return_value={"daily": Decimal("95"), "weekly": Decimal("0"), "monthly": Decimal("0")}
        )

        result = await service.evaluate("agent-a", "default", Decimal("10"))
        assert result.allowed is False
        assert "daily limit" in result.denied_reason
        assert result.remaining_budget.get("daily") == Decimal("5")

    @pytest.mark.asyncio
    async def test_weekly_limit_exceeded(self, service):
        """Weekly limit blocks when cumulative spend exceeds limit."""
        import time

        policy = SpendingPolicy(
            policy_id="p1",
            zone_id="default",
            agent_id="agent-a",
            weekly_limit=Decimal("500"),
        )
        service._cache[("agent-a", "default")] = (policy, time.monotonic() + 60)
        service._get_spending = AsyncMock(
            return_value={"daily": Decimal("0"), "weekly": Decimal("495"), "monthly": Decimal("0")}
        )

        result = await service.evaluate("agent-a", "default", Decimal("10"))
        assert result.allowed is False
        assert "weekly limit" in result.denied_reason

    @pytest.mark.asyncio
    async def test_monthly_limit_exceeded(self, service):
        """Monthly limit blocks when cumulative spend exceeds limit."""
        import time

        policy = SpendingPolicy(
            policy_id="p1",
            zone_id="default",
            agent_id="agent-a",
            monthly_limit=Decimal("2000"),
        )
        service._cache[("agent-a", "default")] = (policy, time.monotonic() + 60)
        service._get_spending = AsyncMock(
            return_value={"daily": Decimal("0"), "weekly": Decimal("0"), "monthly": Decimal("1995")}
        )

        result = await service.evaluate("agent-a", "default", Decimal("10"))
        assert result.allowed is False
        assert "monthly limit" in result.denied_reason

    @pytest.mark.asyncio
    async def test_all_limits_within_budget(self, service):
        """Transfer within all limits is allowed with remaining budget info."""
        import time

        policy = SpendingPolicy(
            policy_id="p1",
            zone_id="default",
            agent_id="agent-a",
            daily_limit=Decimal("100"),
            weekly_limit=Decimal("500"),
            monthly_limit=Decimal("2000"),
            per_tx_limit=Decimal("50"),
        )
        service._cache[("agent-a", "default")] = (policy, time.monotonic() + 60)
        service._get_spending = AsyncMock(
            return_value={
                "daily": Decimal("20"),
                "weekly": Decimal("100"),
                "monthly": Decimal("500"),
            }
        )

        result = await service.evaluate("agent-a", "default", Decimal("10"))
        assert result.allowed is True
        assert result.remaining_budget["daily"] == Decimal("80")
        assert result.remaining_budget["weekly"] == Decimal("400")
        assert result.remaining_budget["monthly"] == Decimal("1500")

    @pytest.mark.asyncio
    async def test_disabled_policy_allows_all(self, service):
        """Disabled policy does not enforce limits."""
        import time

        policy = SpendingPolicy(
            policy_id="p1",
            zone_id="default",
            agent_id="agent-a",
            per_tx_limit=Decimal("1"),
            enabled=False,
        )
        service._cache[("agent-a", "default")] = (policy, time.monotonic() + 60)

        result = await service.evaluate("agent-a", "default", Decimal("1000"))
        assert result.allowed is True

    @pytest.mark.asyncio
    async def test_none_limits_allow_all(self, service):
        """Policy with all None limits allows everything."""
        import time

        policy = SpendingPolicy(policy_id="p1", zone_id="default", agent_id="agent-a")
        service._cache[("agent-a", "default")] = (policy, time.monotonic() + 60)
        service._get_spending = AsyncMock(
            return_value={"daily": Decimal("0"), "weekly": Decimal("0"), "monthly": Decimal("0")}
        )

        result = await service.evaluate("agent-a", "default", Decimal("999999"))
        assert result.allowed is True

    @pytest.mark.asyncio
    async def test_exact_limit_amount_allowed(self, service):
        """Transfer exactly at the limit should be allowed (not >)."""
        import time

        policy = SpendingPolicy(
            policy_id="p1",
            zone_id="default",
            agent_id="agent-a",
            per_tx_limit=Decimal("10"),
        )
        service._cache[("agent-a", "default")] = (policy, time.monotonic() + 60)
        service._get_spending = AsyncMock(
            return_value={"daily": Decimal("0"), "weekly": Decimal("0"), "monthly": Decimal("0")}
        )

        result = await service.evaluate("agent-a", "default", Decimal("10"))
        assert result.allowed is True

    @pytest.mark.asyncio
    async def test_exact_daily_limit_with_spending(self, service):
        """Transfer that would exactly hit the daily limit is allowed."""
        import time

        policy = SpendingPolicy(
            policy_id="p1",
            zone_id="default",
            agent_id="agent-a",
            daily_limit=Decimal("100"),
        )
        service._cache[("agent-a", "default")] = (policy, time.monotonic() + 60)
        service._get_spending = AsyncMock(
            return_value={"daily": Decimal("90"), "weekly": Decimal("0"), "monthly": Decimal("0")}
        )

        # 90 + 10 = 100, exactly at limit
        result = await service.evaluate("agent-a", "default", Decimal("10"))
        assert result.allowed is True


# =============================================================================
# 4. Period Calculation Tests
# =============================================================================


class TestPeriodCalculation:
    """Test _current_period_start helper."""

    def test_daily_period(self):
        from nexus.pay.spending_policy_service import _current_period_start

        assert _current_period_start("daily") == date.today()

    def test_weekly_period_monday(self):
        from nexus.pay.spending_policy_service import _current_period_start

        result = _current_period_start("weekly", ref=date(2026, 2, 11))  # Wednesday
        assert result == date(2026, 2, 9)  # Monday

    def test_monthly_period(self):
        from nexus.pay.spending_policy_service import _current_period_start

        result = _current_period_start("monthly", ref=date(2026, 2, 15))
        assert result == date(2026, 2, 1)

    def test_unknown_period_raises(self):
        from nexus.pay.spending_policy_service import _current_period_start

        with pytest.raises(ValueError, match="Unknown period_type"):
            _current_period_start("hourly")
