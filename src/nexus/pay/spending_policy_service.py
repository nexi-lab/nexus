"""Spending Policy Service — CRUD, evaluation, ledger, approvals, and rate limits.

Issue #1358: Full spending policy engine (Phases 1-4).

Hot path (evaluate):
    1. Policy cache lookup (~0ms, 60s TTL)
    2. Spending ledger read (~1ms, single indexed row)
    3. Rule evaluation (~0.1ms, Python field comparisons)
    4. Rate limit check (~0ms, in-memory sliding window)
    Total: ~1.2ms — well under 5ms target.

Ledger update (record_spending):
    Fire-and-forget async UPSERT after successful transfer.
    Does not block the transfer response.

Default behavior: open by default (no policy = allow all transactions).
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections import deque
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from nexus.pay.constants import credits_to_micro, micro_to_credits
from nexus.pay.policy_rules import RuleContext, evaluate_rules
from nexus.pay.spending_policy import (
    PolicyEvaluation,
    SpendingApproval,
    SpendingPolicy,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from sqlalchemy.ext.asyncio import AsyncSession

    from nexus.storage.models.spending_policy import SpendingPolicyModel

logger = logging.getLogger(__name__)

# Default approval expiry: 24 hours
_APPROVAL_EXPIRY_HOURS = 24


def _current_period_start(period_type: str, ref: date | None = None) -> date:
    """Calculate the start of the current period.

    Args:
        period_type: "daily", "weekly", or "monthly".
        ref: Reference date (defaults to today).

    Returns:
        Start date of the current period.
    """
    ref = ref or date.today()
    if period_type == "daily":
        return ref
    if period_type == "weekly":
        # Monday-based week
        return ref - timedelta(days=ref.weekday())
    if period_type == "monthly":
        return ref.replace(day=1)
    msg = f"Unknown period_type: {period_type}"
    raise ValueError(msg)


class SpendingPolicyService:
    """Manages spending policies and evaluates transactions against them.

    Safe for concurrent coroutines within a single event loop.
    Cache invalidation via TTL (60s). Policy changes take up to 60s to propagate.
    """

    _CACHE_TTL: float = 60.0  # seconds

    def __init__(self, session_factory: Callable[[], AsyncSession]) -> None:
        self._session_factory = session_factory
        # Cache: (agent_id, zone_id) → (policy | None, expires_at)
        self._cache: dict[tuple[str, str], tuple[SpendingPolicy | None, float]] = {}
        # Phase 3: in-memory sliding window for hourly rate limits
        # Key: (agent_id, zone_id) → deque of monotonic timestamps
        self._hourly_counters: dict[tuple[str, str], deque[float]] = {}
        # Phase 3: daily tx counts (in-memory, auto-resets on day change)
        self._daily_tx_counts: dict[tuple[str, str], int] = {}
        self._daily_tx_date: date | None = None

    # =========================================================================
    # CRUD
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
        from nexus.storage.models.spending_policy import SpendingPolicyModel

        policy_id = str(uuid.uuid4())
        now = datetime.now(UTC)

        model = SpendingPolicyModel(
            id=policy_id,
            agent_id=agent_id,
            zone_id=zone_id,
            daily_limit=_to_micro_or_none(daily_limit),
            weekly_limit=_to_micro_or_none(weekly_limit),
            monthly_limit=_to_micro_or_none(monthly_limit),
            per_tx_limit=_to_micro_or_none(per_tx_limit),
            auto_approve_threshold=_to_micro_or_none(auto_approve_threshold),
            max_tx_per_hour=max_tx_per_hour,
            max_tx_per_day=max_tx_per_day,
            rules=json.dumps(rules) if rules is not None else None,
            priority=priority,
            enabled=enabled,
        )

        async with self._session_factory() as session, session.begin():
            session.add(model)

        policy = SpendingPolicy(
            policy_id=policy_id,
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
            created_at=now,
            updated_at=now,
        )

        # Invalidate cache for this agent+zone
        cache_key = (agent_id or "", zone_id)
        self._cache.pop(cache_key, None)

        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "Created spending policy %s for agent=%s zone=%s", policy_id, agent_id, zone_id
            )

        return policy

    async def get_policy(self, agent_id: str | None, zone_id: str) -> SpendingPolicy | None:
        """Get a specific policy by agent_id and zone_id."""
        from sqlalchemy import select

        from nexus.storage.models.spending_policy import SpendingPolicyModel

        async with self._session_factory() as session:
            stmt = select(SpendingPolicyModel).where(
                SpendingPolicyModel.zone_id == zone_id,
                SpendingPolicyModel.enabled.is_(True),
            )
            if agent_id is not None:
                stmt = stmt.where(SpendingPolicyModel.agent_id == agent_id)
            else:
                stmt = stmt.where(SpendingPolicyModel.agent_id.is_(None))

            result = await session.execute(stmt)
            model = result.scalar_one_or_none()
            if model is None:
                return None
            return _model_to_policy(model)

    _UPDATABLE_FIELDS = frozenset(
        {
            "daily_limit",
            "weekly_limit",
            "monthly_limit",
            "per_tx_limit",
            "auto_approve_threshold",
            "max_tx_per_hour",
            "max_tx_per_day",
            "rules",
            "priority",
            "enabled",
        }
    )
    _MICRO_FIELDS = frozenset(
        {
            "daily_limit",
            "weekly_limit",
            "monthly_limit",
            "per_tx_limit",
            "auto_approve_threshold",
        }
    )

    async def update_policy(self, policy_id: str, **updates: Any) -> SpendingPolicy | None:
        """Update a spending policy by ID. Returns updated policy or None if not found."""
        from sqlalchemy import select

        from nexus.storage.models.spending_policy import SpendingPolicyModel

        async with self._session_factory() as session, session.begin():
            stmt = select(SpendingPolicyModel).where(SpendingPolicyModel.id == policy_id)
            result = await session.execute(stmt)
            model = result.scalar_one_or_none()
            if model is None:
                return None

            for key, value in updates.items():
                if key not in self._UPDATABLE_FIELDS:
                    continue
                if key in self._MICRO_FIELDS:
                    setattr(model, key, _to_micro_or_none(value))
                elif key == "rules":
                    setattr(model, key, json.dumps(value) if value is not None else None)
                else:
                    setattr(model, key, value)

            # Invalidate cache
            cache_key = (model.agent_id or "", model.zone_id)
            self._cache.pop(cache_key, None)

            await session.flush()
            return _model_to_policy(model)

    async def delete_policy(self, policy_id: str) -> bool:
        """Delete a spending policy by ID. Returns True if deleted."""
        from sqlalchemy import delete, select

        from nexus.storage.models.spending_policy import SpendingPolicyModel

        async with self._session_factory() as session, session.begin():
            # Fetch first to invalidate cache
            stmt = select(SpendingPolicyModel).where(SpendingPolicyModel.id == policy_id)
            result = await session.execute(stmt)
            model = result.scalar_one_or_none()
            if model is None:
                return False

            cache_key = (model.agent_id or "", model.zone_id)
            self._cache.pop(cache_key, None)

            del_stmt = delete(SpendingPolicyModel).where(SpendingPolicyModel.id == policy_id)
            await session.execute(del_stmt)

        return True

    async def list_policies(self, zone_id: str) -> list[SpendingPolicy]:
        """List all policies for a zone."""
        from sqlalchemy import select

        from nexus.storage.models.spending_policy import SpendingPolicyModel

        async with self._session_factory() as session:
            stmt = (
                select(SpendingPolicyModel)
                .where(SpendingPolicyModel.zone_id == zone_id)
                .order_by(SpendingPolicyModel.priority.desc())
            )
            result = await session.execute(stmt)
            models = result.scalars().all()
            return [_model_to_policy(m) for m in models]

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
        spending = await self._get_spending(agent_id, zone_id)
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
        from nexus.storage.models.spending_policy import SpendingApprovalModel

        approval_id = str(uuid.uuid4())
        now = datetime.now(UTC)
        expires_at = now + timedelta(hours=_APPROVAL_EXPIRY_HOURS)

        model = SpendingApprovalModel(
            id=approval_id,
            policy_id=policy_id,
            agent_id=agent_id,
            zone_id=zone_id,
            amount=credits_to_micro(amount),
            to=to,
            memo=memo,
            status="pending",
            requested_at=now,
            expires_at=expires_at,
        )

        async with self._session_factory() as session, session.begin():
            session.add(model)

        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "Approval requested: id=%s agent=%s amount=%s",
                approval_id,
                agent_id,
                amount,
            )

        return SpendingApproval(
            approval_id=approval_id,
            policy_id=policy_id,
            agent_id=agent_id,
            zone_id=zone_id,
            amount=amount,
            to=to,
            memo=memo,
            status="pending",
            requested_at=now,
            expires_at=expires_at,
        )

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
        from sqlalchemy import select

        from nexus.storage.models.spending_policy import SpendingApprovalModel

        async with self._session_factory() as session:
            stmt = select(SpendingApprovalModel).where(
                SpendingApprovalModel.id == approval_id,
            )
            result = await session.execute(stmt)
            model = result.scalar_one_or_none()

        if model is None:
            return None

        # Validate
        if model.agent_id != agent_id:
            return None
        if model.status != "approved":
            return None
        if datetime.now(UTC) > model.expires_at:
            return None
        if credits_to_micro(amount) != model.amount:
            return None

        return _approval_model_to_dataclass(model)

    async def approve_request(self, approval_id: str, decided_by: str) -> SpendingApproval | None:
        """Approve a pending approval request. Returns updated approval or None."""
        return await self._decide_approval(approval_id, "approved", decided_by)

    async def reject_request(self, approval_id: str, decided_by: str) -> SpendingApproval | None:
        """Reject a pending approval request. Returns updated approval or None."""
        return await self._decide_approval(approval_id, "rejected", decided_by)

    async def list_pending_approvals(self, zone_id: str) -> list[SpendingApproval]:
        """List all pending approvals for a zone."""
        from sqlalchemy import select

        from nexus.storage.models.spending_policy import SpendingApprovalModel

        now = datetime.now(UTC)
        async with self._session_factory() as session:
            stmt = (
                select(SpendingApprovalModel)
                .where(
                    SpendingApprovalModel.zone_id == zone_id,
                    SpendingApprovalModel.status == "pending",
                    SpendingApprovalModel.expires_at > now,
                )
                .order_by(SpendingApprovalModel.requested_at.desc())
            )
            result = await session.execute(stmt)
            models = result.scalars().all()
            return [_approval_model_to_dataclass(m) for m in models]

    async def _decide_approval(
        self, approval_id: str, decision: str, decided_by: str
    ) -> SpendingApproval | None:
        """Set approval status to approved/rejected."""
        from sqlalchemy import select

        from nexus.storage.models.spending_policy import SpendingApprovalModel

        now = datetime.now(UTC)
        async with self._session_factory() as session, session.begin():
            stmt = select(SpendingApprovalModel).where(
                SpendingApprovalModel.id == approval_id,
                SpendingApprovalModel.status == "pending",
            )
            result = await session.execute(stmt)
            model = result.scalar_one_or_none()
            if model is None:
                return None

            model.status = decision
            model.decided_at = now
            model.decided_by = decided_by

            await session.flush()
            return _approval_model_to_dataclass(model)

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

        Uses PostgreSQL UPSERT (INSERT ON CONFLICT DO UPDATE) for atomicity.
        Called fire-and-forget after a successful transfer.
        Also updates in-memory rate limit counters.
        """
        from sqlalchemy import text as sa_text

        micro_amount = credits_to_micro(amount)

        async with self._session_factory() as session, session.begin():
            for period_type in ("daily", "weekly", "monthly"):
                period_start = _current_period_start(period_type)
                # UPSERT: insert or increment
                stmt = sa_text("""
                    INSERT INTO spending_ledger
                        (agent_id, zone_id, period_type, period_start,
                         amount_spent, tx_count, updated_at)
                    VALUES (:agent_id, :zone_id, :period_type,
                            :period_start, :amount, 1, NOW())
                    ON CONFLICT (agent_id, zone_id, period_type, period_start)
                    DO UPDATE SET
                        amount_spent = spending_ledger.amount_spent + :amount,
                        tx_count = spending_ledger.tx_count + 1,
                        updated_at = NOW()
                """)
                await session.execute(
                    stmt,
                    {
                        "agent_id": agent_id,
                        "zone_id": zone_id,
                        "period_type": period_type,
                        "period_start": period_start,
                        "amount": micro_amount,
                    },
                )

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

        spending = await self._get_spending(agent_id, zone_id)
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
        """
        # Check cache for agent-specific
        cache_key = (agent_id, zone_id)
        now = time.monotonic()
        cached = self._cache.get(cache_key)
        if cached is not None and cached[1] > now:
            return cached[0]

        # DB lookup: agent-specific policies
        from sqlalchemy import select

        from nexus.storage.models.spending_policy import SpendingPolicyModel

        async with self._session_factory() as session:
            # Agent-specific policy (highest priority)
            stmt = (
                select(SpendingPolicyModel)
                .where(
                    SpendingPolicyModel.zone_id == zone_id,
                    SpendingPolicyModel.agent_id == agent_id,
                    SpendingPolicyModel.enabled.is_(True),
                )
                .order_by(SpendingPolicyModel.priority.desc())
                .limit(1)
            )
            result = await session.execute(stmt)
            model = result.scalar_one_or_none()

            if model is not None:
                policy = _model_to_policy(model)
                self._cache[cache_key] = (policy, now + self._CACHE_TTL)
                return policy

            # Zone-level default (agent_id IS NULL)
            stmt = (
                select(SpendingPolicyModel)
                .where(
                    SpendingPolicyModel.zone_id == zone_id,
                    SpendingPolicyModel.agent_id.is_(None),
                    SpendingPolicyModel.enabled.is_(True),
                )
                .order_by(SpendingPolicyModel.priority.desc())
                .limit(1)
            )
            result = await session.execute(stmt)
            model = result.scalar_one_or_none()

            resolved: SpendingPolicy | None = _model_to_policy(model) if model is not None else None

        self._cache[cache_key] = (resolved, now + self._CACHE_TTL)
        return resolved

    async def _get_spending(self, agent_id: str, zone_id: str) -> dict[str, Decimal]:
        """Get current spending for all active periods (single query).

        Returns dict like {"daily": Decimal("42.50"), "weekly": Decimal("150"), ...}
        """
        import sqlalchemy as sa
        from sqlalchemy import select

        from nexus.storage.models.spending_policy import SpendingLedgerModel

        periods = {pt: _current_period_start(pt) for pt in ("daily", "weekly", "monthly")}

        async with self._session_factory() as session:
            stmt = select(
                SpendingLedgerModel.period_type,
                SpendingLedgerModel.amount_spent,
            ).where(
                SpendingLedgerModel.agent_id == agent_id,
                SpendingLedgerModel.zone_id == zone_id,
                sa.or_(
                    *[
                        sa.and_(
                            SpendingLedgerModel.period_type == pt,
                            SpendingLedgerModel.period_start == start,
                        )
                        for pt, start in periods.items()
                    ]
                ),
            )
            result = await session.execute(stmt)
            rows = result.all()

        result_map: dict[str, Decimal] = {}
        for period_type, micro in rows:
            result_map[period_type] = micro_to_credits(micro) if micro is not None else Decimal("0")

        # Fill in missing periods with zero
        for pt in periods:
            if pt not in result_map:
                result_map[pt] = Decimal("0")

        return result_map

    async def _get_daily_tx_count(self, agent_id: str, zone_id: str) -> int:
        """Get today's transaction count from the ledger."""
        from sqlalchemy import select

        from nexus.storage.models.spending_policy import SpendingLedgerModel

        today = _current_period_start("daily")
        async with self._session_factory() as session:
            stmt = select(SpendingLedgerModel.tx_count).where(
                SpendingLedgerModel.agent_id == agent_id,
                SpendingLedgerModel.zone_id == zone_id,
                SpendingLedgerModel.period_type == "daily",
                SpendingLedgerModel.period_start == today,
            )
            result = await session.execute(stmt)
            row = result.scalar_one_or_none()
        return row or 0

    def clear_cache(self) -> None:
        """Clear the policy cache and rate limit counters (for testing)."""
        self._cache.clear()
        self._hourly_counters.clear()
        self._daily_tx_counts.clear()


# =============================================================================
# Helpers
# =============================================================================


def _to_micro_or_none(value: Decimal | None) -> int | None:
    """Convert credits Decimal to micro-credits int, or None."""
    if value is None:
        return None
    return credits_to_micro(value)


def _model_to_policy(model: SpendingPolicyModel) -> SpendingPolicy:
    """Convert SQLAlchemy model to frozen dataclass."""
    rules_parsed: list[dict[str, Any]] | None = None
    if model.rules is not None:
        try:
            rules_parsed = json.loads(model.rules)
        except (json.JSONDecodeError, TypeError):
            logger.warning("Invalid rules JSON for policy %s", model.id)

    return SpendingPolicy(
        policy_id=model.id,
        zone_id=model.zone_id,
        agent_id=model.agent_id,
        daily_limit=micro_to_credits(model.daily_limit) if model.daily_limit is not None else None,
        weekly_limit=micro_to_credits(model.weekly_limit)
        if model.weekly_limit is not None
        else None,
        monthly_limit=micro_to_credits(model.monthly_limit)
        if model.monthly_limit is not None
        else None,
        per_tx_limit=micro_to_credits(model.per_tx_limit)
        if model.per_tx_limit is not None
        else None,
        auto_approve_threshold=(
            micro_to_credits(model.auto_approve_threshold)
            if model.auto_approve_threshold is not None
            else None
        ),
        max_tx_per_hour=model.max_tx_per_hour,
        max_tx_per_day=model.max_tx_per_day,
        rules=rules_parsed,
        priority=model.priority,
        enabled=model.enabled,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


def _approval_model_to_dataclass(model: Any) -> SpendingApproval:
    """Convert SpendingApprovalModel to SpendingApproval dataclass."""
    return SpendingApproval(
        approval_id=model.id,
        policy_id=model.policy_id,
        agent_id=model.agent_id,
        zone_id=model.zone_id,
        amount=micro_to_credits(model.amount),
        to=model.to,
        memo=model.memo,
        status=model.status,
        requested_at=model.requested_at,
        decided_at=model.decided_at,
        decided_by=model.decided_by,
        expires_at=model.expires_at,
    )
