"""SQLAlchemy-backed spending policy repository.

Concrete implementation of SpendingPolicyRepository protocol defined in
bricks/pay/protocols.py. Handles all database operations for spending
policies, ledger entries, and approval workflow records.

All monetary conversions (credits ↔ micro-credits) happen at this boundary.
The service layer works exclusively in Decimal credits.

Issue #2189: Extracted from bricks/pay/spending_policy_service.py.
"""

import json
import logging
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from nexus.contracts.pay_types import (
    SpendingApproval,
    SpendingPolicy,
    credits_to_micro,
    micro_to_credits,
)

if TYPE_CHECKING:
    from nexus.storage.record_store import RecordStoreABC

logger = logging.getLogger(__name__)

# Default approval expiry: 24 hours
_APPROVAL_EXPIRY_HOURS = 24


def _current_period_start(period_type: str, ref: date | None = None) -> date:
    """Calculate the start of the current period."""
    ref = ref or date.today()
    if period_type == "daily":
        return ref
    if period_type == "weekly":
        return ref - timedelta(days=ref.weekday())
    if period_type == "monthly":
        return ref.replace(day=1)
    msg = f"Unknown period_type: {period_type}"
    raise ValueError(msg)


def _to_micro_or_none(value: Decimal | None) -> int | None:
    """Convert credits Decimal to micro-credits int, or None."""
    if value is None:
        return None
    return credits_to_micro(value)


def _model_to_policy(model: Any) -> SpendingPolicy:
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


# -- Updatable field definitions (shared between service and repo) --
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


class SQLAlchemySpendingPolicyRepository:
    """SQLAlchemy-backed implementation of SpendingPolicyRepository.

    Satisfies the SpendingPolicyRepository protocol via structural subtyping.
    All monetary values are converted between Decimal (credits) and int
    (micro-credits) at this boundary.
    """

    def __init__(self, record_store: "RecordStoreABC") -> None:
        self._session_factory = record_store.async_session_factory

    # -- Policy CRUD --

    async def create_policy(
        self,
        *,
        zone_id: str,
        agent_id: str | None,
        daily_limit: Decimal | None,
        weekly_limit: Decimal | None,
        monthly_limit: Decimal | None,
        per_tx_limit: Decimal | None,
        auto_approve_threshold: Decimal | None,
        max_tx_per_hour: int | None,
        max_tx_per_day: int | None,
        rules: list[dict[str, Any]] | None,
        priority: int,
        enabled: bool,
    ) -> SpendingPolicy:
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

        return SpendingPolicy(
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

    async def get_policy(
        self,
        agent_id: str | None,
        zone_id: str,
    ) -> SpendingPolicy | None:
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

    async def update_policy(
        self,
        policy_id: str,
        **updates: Any,
    ) -> tuple[SpendingPolicy | None, tuple[str, str] | None]:
        from sqlalchemy import select

        from nexus.storage.models.spending_policy import SpendingPolicyModel

        async with self._session_factory() as session, session.begin():
            stmt = select(SpendingPolicyModel).where(SpendingPolicyModel.id == policy_id)
            result = await session.execute(stmt)
            model = result.scalar_one_or_none()
            if model is None:
                return None, None

            for key, value in updates.items():
                if key not in _UPDATABLE_FIELDS:
                    continue
                if key in _MICRO_FIELDS:
                    setattr(model, key, _to_micro_or_none(value))
                elif key == "rules":
                    setattr(model, key, json.dumps(value) if value is not None else None)
                else:
                    setattr(model, key, value)

            cache_key = (model.agent_id or "", model.zone_id)
            await session.flush()
            return _model_to_policy(model), cache_key

    async def delete_policy(
        self,
        policy_id: str,
    ) -> tuple[bool, tuple[str, str] | None]:
        from sqlalchemy import delete, select

        from nexus.storage.models.spending_policy import SpendingPolicyModel

        async with self._session_factory() as session, session.begin():
            stmt = select(SpendingPolicyModel).where(SpendingPolicyModel.id == policy_id)
            result = await session.execute(stmt)
            model = result.scalar_one_or_none()
            if model is None:
                return False, None

            cache_key = (model.agent_id or "", model.zone_id)

            del_stmt = delete(SpendingPolicyModel).where(SpendingPolicyModel.id == policy_id)
            await session.execute(del_stmt)

        return True, cache_key

    async def list_policies(self, zone_id: str) -> list[SpendingPolicy]:
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

    async def resolve_policy(
        self,
        agent_id: str,
        zone_id: str,
    ) -> SpendingPolicy | None:
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
                return _model_to_policy(model)

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

            return _model_to_policy(model) if model is not None else None

    # -- Spending Ledger --

    async def record_spending(
        self,
        agent_id: str,
        zone_id: str,
        amount: Decimal,
    ) -> None:
        from sqlalchemy import func
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        from nexus.storage.models.spending_policy import SpendingLedgerModel

        micro_amount = credits_to_micro(amount)

        async with self._session_factory() as session, session.begin():
            for period_type in ("daily", "weekly", "monthly"):
                period_start = _current_period_start(period_type)
                stmt = (
                    pg_insert(SpendingLedgerModel)
                    .values(
                        agent_id=agent_id,
                        zone_id=zone_id,
                        period_type=period_type,
                        period_start=period_start,
                        amount_spent=micro_amount,
                        tx_count=1,
                        updated_at=func.now(),
                    )
                    .on_conflict_do_update(
                        constraint="uq_spending_ledger_agent_period",
                        set_={
                            "amount_spent": SpendingLedgerModel.amount_spent + micro_amount,
                            "tx_count": SpendingLedgerModel.tx_count + 1,
                            "updated_at": func.now(),
                        },
                    )
                )
                await session.execute(stmt)

    async def get_spending(
        self,
        agent_id: str,
        zone_id: str,
    ) -> dict[str, Decimal]:
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

        for pt in periods:
            if pt not in result_map:
                result_map[pt] = Decimal("0")

        return result_map

    async def get_daily_tx_count(
        self,
        agent_id: str,
        zone_id: str,
    ) -> int:
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

    # -- Approvals --

    async def create_approval(
        self,
        *,
        policy_id: str,
        agent_id: str,
        zone_id: str,
        amount: Decimal,
        to: str,
        memo: str,
        expires_at: datetime,
    ) -> SpendingApproval:
        from nexus.storage.models.spending_policy import SpendingApprovalModel

        approval_id = str(uuid.uuid4())
        now = datetime.now(UTC)

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
        if model.agent_id != agent_id:
            return None
        if model.status != "approved":
            return None
        if datetime.now(UTC) > model.expires_at:
            return None
        if credits_to_micro(amount) != model.amount:
            return None

        return _approval_model_to_dataclass(model)

    async def decide_approval(
        self,
        approval_id: str,
        decision: str,
        decided_by: str,
    ) -> SpendingApproval | None:
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

    async def list_pending_approvals(
        self,
        zone_id: str,
    ) -> list[SpendingApproval]:
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
