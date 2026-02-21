"""Spending Policy Service — evaluation, rate limits, budget, and cache.

Issue #1358: Full spending policy engine (Phases 1-4).
Issue #2189: Replaced concrete nexus.storage imports with Repository Protocol.

Hot path (evaluate):
    1. Policy cache lookup (~0ms, 60s TTL)
    2. Spending ledger read via repository (~1ms, single indexed row)
    3. Rule evaluation (~0.1ms, Python field comparisons)
    4. Rate limit check (~0ms, in-memory sliding window)
    Total: ~1.2ms — well under 5ms target.

Ledger update (record_spending):
    Fire-and-forget async UPSERT via repository after successful transfer.
    Does not block the transfer response.

Default behavior: open by default (no policy = allow all transactions).
"""

import logging
import time
from collections import deque
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from cachetools import LRUCache, TTLCache

from nexus.bricks.pay.policy_rules import RuleContext, evaluate_rules
from nexus.bricks.pay.spending_policy import (
    PolicyEvaluation,
    SpendingApproval,
    SpendingPolicy,
)

if TYPE_CHECKING:
    from nexus.bricks.pay.protocols import SpendingPolicyRepository

logger = logging.getLogger(__name__)

# Default approval expiry: 24 hours
_APPROVAL_EXPIRY_HOURS = 24


class SpendingPolicyService:
    """Manages spending policies and evaluates transactions against them.

    Safe for concurrent coroutines within a single event loop.
    Cache invalidation via TTL (60s). Policy changes take up to 60s to propagate.

    All storage operations are delegated to the SpendingPolicyRepository,
    keeping this service free of nexus.storage imports.
    """

    _CACHE_TTL: float = 60.0  # seconds

    def __init__(
        self,
        repo: "SpendingPolicyRepository",
        *,
        cache_ttl: float = _CACHE_TTL,
        max_cache_entries: int = 4096,
    ) -> None:
        self._repo = repo
        # Policy cache with automatic TTL expiration
        self._cache: TTLCache[tuple[str, str], SpendingPolicy | None] = TTLCache(
            maxsize=max_cache_entries, ttl=cache_ttl
        )
        # Phase 3: in-memory sliding window for hourly rate limits (bounded)
        self._hourly_counters: LRUCache[tuple[str, str], deque[float]] = LRUCache(
            maxsize=max_cache_entries
        )
        # Phase 3: daily tx counts (bounded, auto-resets on day change)
        self._daily_tx_counts: LRUCache[tuple[str, str], int] = LRUCache(maxsize=max_cache_entries)
        self._daily_tx_date: date | None = None

    # =========================================================================
    # CRUD (delegated to repository)
    # =========================================================================

    async def create_policy(
        self,
        *,
        zone_id: str,
        agent_id: str | None = None,
        daily_limit: Decimal | None = None,
        weekly_limit: Decimal | None = None,
        monthly_limit: Decimal | None = None,
        per_tx_limit: Decimal | None = None,
        auto_approve_threshold: Decimal | None = None,
        max_tx_per_hour: int | None = None,
        max_tx_per_day: int | None = None,
        rules: list[dict[str, Any]] | None = None,
        priority: int = 0,
        enabled: bool = True,
    ) -> SpendingPolicy:
        """Create a new spending policy."""
        policy = await self._repo.create_policy(
            zone_id=zone_id,
            agent_id=agent_id,
            daily_limit=daily_limit,
            weekly_limit=weekly_limit,
            monthly_limit=monthly_limit,
            per_tx_limit=per_tx_limit,
            auto_approve_threshold=auto_approve_threshold,
            max_tx_per_hour=max_tx_per_hour,
            max_tx_per_day=max_tx_per_day,
            rules=rules,
            priority=priority,
            enabled=enabled,
        )

        # Invalidate cache for this agent+zone
        cache_key = (agent_id or "", zone_id)
        self._cache.pop(cache_key, None)

        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "Created spending policy %s for agent=%s zone=%s",
                policy.policy_id,
                agent_id,
                zone_id,
            )

        return policy

    async def get_policy(self, agent_id: str | None, zone_id: str) -> SpendingPolicy | None:
        """Get a specific policy by agent_id and zone_id."""
        return await self._repo.get_policy(agent_id, zone_id)

    async def update_policy(self, policy_id: str, **updates: Any) -> SpendingPolicy | None:
        """Update a spending policy by ID. Returns updated policy or None if not found."""
        policy, cache_key = await self._repo.update_policy(policy_id, **updates)
        if cache_key is not None:
            self._cache.pop(cache_key, None)
        return policy

    async def delete_policy(self, policy_id: str) -> bool:
        """Delete a spending policy by ID. Returns True if deleted."""
        deleted, cache_key = await self._repo.delete_policy(policy_id)
        if cache_key is not None:
            self._cache.pop(cache_key, None)
        return deleted

    async def list_policies(self, zone_id: str) -> list[SpendingPolicy]:
        """List all policies for a zone."""
        return await self._repo.list_policies(zone_id)

    # =========================================================================
    # Evaluation (hot path — must be <5ms)
    # =========================================================================

    async def evaluate(
        self,
        agent_id: str,
        zone_id: str,
        amount: Decimal,
        *,
        to: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> PolicyEvaluation:
        """Evaluate a transaction against the agent's spending policy.

        Resolution order:
            1. Agent-specific policy (highest priority if multiple)
            2. Zone-level default (agent_id=NULL)
            3. No policy found → allow (open by default)

        Checks (in order):
            1. Per-transaction limit
            2. Period-based budget limits (daily/weekly/monthly)
            3. Rate limits (Phase 3: max_tx_per_hour, max_tx_per_day)
            4. DSL rules (Phase 4: recipient, time window, metadata, amount range)
            5. Approval threshold (Phase 2: auto_approve_threshold)
        """
        policy = await self._resolve_policy(agent_id, zone_id)
        if policy is None:
            return PolicyEvaluation(allowed=True)

        if not policy.enabled:
            return PolicyEvaluation(allowed=True)

        # 1. Per-transaction limit (no DB read needed)
        if policy.per_tx_limit is not None and amount > policy.per_tx_limit:
            return PolicyEvaluation(
                allowed=False,
                denied_reason=(
                    f"Amount {amount} exceeds per-transaction limit {policy.per_tx_limit}"
                ),
                policy_id=policy.policy_id,
            )

        # 2. Period-based checks (requires ledger read)
        spending = await self._repo.get_spending(agent_id, zone_id)
        remaining: dict[str, Decimal] = {}

        for period_type, limit in [
            ("daily", policy.daily_limit),
            ("weekly", policy.weekly_limit),
            ("monthly", policy.monthly_limit),
        ]:
            if limit is None:
                continue
            spent = spending.get(period_type, Decimal("0"))
            left = limit - spent
            remaining[period_type] = left

            if spent + amount > limit:
                return PolicyEvaluation(
                    allowed=False,
                    denied_reason=(
                        f"Amount {amount} would exceed {period_type} limit {limit} "
                        f"(already spent: {spent})"
                    ),
                    policy_id=policy.policy_id,
                    remaining_budget=remaining,
                )

        # 3. Rate limits (Phase 3)
        rate_result = self._check_rate_limits(agent_id, zone_id, policy)
        if rate_result is not None:
            return rate_result

        # 4. DSL rules (Phase 4)
        if policy.rules:
            rule_ctx = RuleContext(
                agent_id=agent_id,
                zone_id=zone_id,
                to=to,
                amount=amount,
                metadata=metadata or {},
            )
            rule_result = evaluate_rules(policy.rules, rule_ctx)
            if not rule_result.allowed:
                return PolicyEvaluation(
                    allowed=False,
                    denied_reason=rule_result.denied_reason,
                    policy_id=policy.policy_id,
                    remaining_budget=remaining,
                )

        # 5. Approval threshold (Phase 2)
        if policy.auto_approve_threshold is not None and amount > policy.auto_approve_threshold:
            return PolicyEvaluation(
                allowed=False,
                denied_reason=(
                    f"Amount {amount} exceeds auto-approve threshold "
                    f"{policy.auto_approve_threshold} — approval required"
                ),
                policy_id=policy.policy_id,
                remaining_budget=remaining,
                requires_approval=True,
            )

        return PolicyEvaluation(
            allowed=True,
            policy_id=policy.policy_id,
            remaining_budget=remaining,
        )

    def _check_rate_limits(
        self,
        agent_id: str,
        zone_id: str,
        policy: SpendingPolicy,
    ) -> PolicyEvaluation | None:
        """Check transaction rate limits. Returns PolicyEvaluation if denied, else None."""
        # Reset daily counters on calendar day change
        today = date.today()
        if self._daily_tx_date != today:
            self._daily_tx_counts.clear()
            self._daily_tx_date = today

        # Daily tx rate limit (from in-memory counter)
        if policy.max_tx_per_day is not None:
            daily_tx = self._daily_tx_counts.get((agent_id, zone_id), 0)
            if daily_tx >= policy.max_tx_per_day:
                return PolicyEvaluation(
                    allowed=False,
                    denied_reason=(
                        f"Daily transaction limit reached ({policy.max_tx_per_day} tx/day)"
                    ),
                    policy_id=policy.policy_id,
                )

        # Hourly tx rate limit (sliding window)
        if policy.max_tx_per_hour is not None:
            key = (agent_id, zone_id)
            now = time.monotonic()
            window = self._hourly_counters.get(key, deque())
            # Remove timestamps older than 1 hour
            cutoff = now - 3600
            while window and window[0] < cutoff:
                window.popleft()
            # Evict empty deques to prevent unbounded dict growth
            if not window:
                self._hourly_counters.pop(key, None)
            if len(window) >= policy.max_tx_per_hour:
                return PolicyEvaluation(
                    allowed=False,
                    denied_reason=(
                        f"Hourly transaction limit reached ({policy.max_tx_per_hour} tx/hour)"
                    ),
                    policy_id=policy.policy_id,
                )

        return None

    def record_rate_limit_hit(self, agent_id: str, zone_id: str) -> None:
        """Record a transaction for rate limiting purposes.

        Called after a successful transfer to update counters.
        """
        key = (agent_id, zone_id)
        now = time.monotonic()

        # Update hourly sliding window
        if key not in self._hourly_counters:
            self._hourly_counters[key] = deque()
        self._hourly_counters[key].append(now)

        # Update daily tx count
        self._daily_tx_counts[key] = self._daily_tx_counts.get(key, 0) + 1

    # =========================================================================
    # Approvals (Phase 2)
    # =========================================================================

    async def request_approval(
        self,
        *,
        policy_id: str,
        agent_id: str,
        zone_id: str,
        amount: Decimal,
        to: str,
        memo: str = "",
    ) -> SpendingApproval:
        """Create a pending approval request."""
        now = datetime.now(UTC)
        expires_at = now + timedelta(hours=_APPROVAL_EXPIRY_HOURS)

        approval = await self._repo.create_approval(
            policy_id=policy_id,
            agent_id=agent_id,
            zone_id=zone_id,
            amount=amount,
            to=to,
            memo=memo,
            expires_at=expires_at,
        )

        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "Approval requested: id=%s agent=%s amount=%s",
                approval.approval_id,
                agent_id,
                amount,
            )

        return approval

    async def check_approval(
        self,
        approval_id: str,
        agent_id: str,
        amount: Decimal,
    ) -> SpendingApproval | None:
        """Check if an approval exists and is valid for this transfer.

        Returns the approval if it is approved, not expired, matches agent/amount.
        Returns None if not found, wrong agent, wrong amount, or not approved.
        """
        return await self._repo.check_approval(approval_id, agent_id, amount)

    async def approve_request(self, approval_id: str, decided_by: str) -> SpendingApproval | None:
        """Approve a pending approval request. Returns updated approval or None."""
        return await self._repo.decide_approval(approval_id, "approved", decided_by)

    async def reject_request(self, approval_id: str, decided_by: str) -> SpendingApproval | None:
        """Reject a pending approval request. Returns updated approval or None."""
        return await self._repo.decide_approval(approval_id, "rejected", decided_by)

    async def list_pending_approvals(self, zone_id: str) -> list[SpendingApproval]:
        """List all pending approvals for a zone."""
        return await self._repo.list_pending_approvals(zone_id)

    # =========================================================================
    # Ledger
    # =========================================================================

    async def record_spending(
        self,
        agent_id: str,
        zone_id: str,
        amount: Decimal,
    ) -> None:
        """Atomically increment spending counters for all active periods.

        Delegates to repository for database UPSERT.
        Also updates in-memory rate limit counters.
        """
        await self._repo.record_spending(agent_id, zone_id, amount)

        # Update rate limit counters
        self.record_rate_limit_hit(agent_id, zone_id)

        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "Recorded spending: agent=%s zone=%s amount=%s",
                agent_id,
                zone_id,
                amount,
            )

    async def get_budget_summary(
        self,
        agent_id: str,
        zone_id: str,
    ) -> dict[str, Any]:
        """Get budget summary for an agent (for API response).

        Returns remaining budget per period and the active policy.
        """
        policy = await self._resolve_policy(agent_id, zone_id)
        if policy is None:
            return {"has_policy": False, "policy_id": None, "limits": {}, "remaining": {}}

        spending = await self._repo.get_spending(agent_id, zone_id)
        limits: dict[str, str] = {}
        remaining: dict[str, str] = {}

        for period_type, limit in [
            ("daily", policy.daily_limit),
            ("weekly", policy.weekly_limit),
            ("monthly", policy.monthly_limit),
            ("per_tx", policy.per_tx_limit),
        ]:
            if limit is not None:
                limits[period_type] = str(limit)
                if period_type != "per_tx":
                    spent = spending.get(period_type, Decimal("0"))
                    remaining[period_type] = str(limit - spent)
                else:
                    remaining[period_type] = str(limit)

        # Phase 2: approval threshold
        if policy.auto_approve_threshold is not None:
            limits["auto_approve"] = str(policy.auto_approve_threshold)

        # Phase 3: rate limits
        rate_limits: dict[str, int] = {}
        if policy.max_tx_per_hour is not None:
            rate_limits["max_tx_per_hour"] = policy.max_tx_per_hour
        if policy.max_tx_per_day is not None:
            rate_limits["max_tx_per_day"] = policy.max_tx_per_day

        return {
            "has_policy": True,
            "policy_id": policy.policy_id,
            "limits": limits,
            "spent": {k: str(v) for k, v in spending.items()},
            "remaining": remaining,
            "rate_limits": rate_limits,
            "has_rules": bool(policy.rules),
        }

    # =========================================================================
    # Internal
    # =========================================================================

    async def _resolve_policy(self, agent_id: str, zone_id: str) -> SpendingPolicy | None:
        """Resolve effective policy with 60s TTL cache.

        Resolution: agent-specific → zone default. Highest priority wins.
        Cache is managed at service level; repository is stateless.
        """
        cache_key = (agent_id, zone_id)
        if cache_key in self._cache:
            return self._cache[cache_key]

        resolved = await self._repo.resolve_policy(agent_id, zone_id)
        self._cache[cache_key] = resolved
        return resolved

    def clear_cache(self) -> None:
        """Clear the policy cache and rate limit counters (for testing)."""
        self._cache.clear()
        self._hourly_counters.clear()
        self._daily_tx_counts.clear()
