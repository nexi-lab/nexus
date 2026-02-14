"""PolicyEnforcedPayment — recursive wrapper for spending policy enforcement.

Issue #1358: Wraps any PaymentProtocol with budget policy checks.
Follows Lego Architecture Mechanism 2: recursive wrapping (same-Protocol).

Phases:
    1. Budget limits (per-tx, daily, weekly, monthly)
    2. Approval workflows (auto_approve_threshold → ApprovalRequiredError)
    3. Rate controls (max_tx_per_hour, max_tx_per_day)
    4. DSL rules (recipient, time window, metadata, amount range)
"""

from __future__ import annotations

import asyncio
import logging
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from nexus.pay.audit_types import TransactionProtocol
from nexus.pay.protocol import PaymentProtocol, ProtocolTransferRequest, ProtocolTransferResult
from nexus.pay.spending_policy import ApprovalRequiredError, PolicyDeniedError

if TYPE_CHECKING:
    from nexus.pay.spending_policy_service import SpendingPolicyService

logger = logging.getLogger(__name__)


class PolicyEnforcedPayment(PaymentProtocol):
    """Wraps a PaymentProtocol with spending policy enforcement.

    Implements the full PaymentProtocol interface (same-Protocol wrapper).
    Evaluates spending policies before delegating to the inner protocol.
    On success, asynchronously records spending in the ledger.

    Default behavior: if no policy exists for the agent, the transfer
    is allowed (open by default).
    """

    def __init__(
        self,
        inner: PaymentProtocol,
        policy_service: SpendingPolicyService,
    ) -> None:
        self._inner = inner
        self._policy_service = policy_service

    @property
    def protocol_name(self) -> TransactionProtocol:
        """Delegate to inner protocol."""
        return self._inner.protocol_name

    def can_handle(self, to: str, metadata: dict[str, Any] | None = None) -> bool:
        """Delegate to inner protocol."""
        return self._inner.can_handle(to, metadata)

    async def transfer(self, request: ProtocolTransferRequest) -> ProtocolTransferResult:
        """Execute transfer with policy enforcement.

        Flow:
            1. Check for pre-approved approval_id in metadata
            2. Evaluate spending policy (~1.2ms)
            3. If requires_approval → raise ApprovalRequiredError
            4. If denied → raise PolicyDeniedError (inner NOT called)
            5. Delegate to inner protocol
            6. On success → fire-and-forget ledger update
        """
        zone_id = request.metadata.get("zone_id", "default") if request.metadata else "default"
        approval_id = request.metadata.get("approval_id") if request.metadata else None

        # 1. If approval_id provided, check it before evaluation
        if approval_id:
            approval = await self._policy_service.check_approval(
                approval_id=approval_id,
                agent_id=request.from_agent,
                amount=request.amount,
            )
            if approval is not None:
                # Approval valid — skip policy evaluation, proceed to inner
                result = await self._inner.transfer(request)
                self._fire_and_forget_record(request.from_agent, zone_id, request.amount)
                return result
            # Invalid approval — fall through to normal evaluation
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug(
                    "Invalid approval_id=%s for agent=%s — falling back to evaluation",
                    approval_id,
                    request.from_agent,
                )

        # 2. Evaluate policy
        evaluation = await self._policy_service.evaluate(
            agent_id=request.from_agent,
            zone_id=zone_id,
            amount=request.amount,
            to=request.to,
            metadata=request.metadata,
        )

        if not evaluation.allowed:
            # 3. Approval required (Phase 2)
            if evaluation.requires_approval:
                if logger.isEnabledFor(logging.DEBUG):
                    logger.debug(
                        "Approval required: agent=%s amount=%s",
                        request.from_agent,
                        request.amount,
                    )
                raise ApprovalRequiredError(
                    evaluation.denied_reason or "Approval required for this transaction",
                    policy_id=evaluation.policy_id,
                )

            # 4. Hard deny
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug(
                    "Policy denied transfer: agent=%s amount=%s reason=%s",
                    request.from_agent,
                    request.amount,
                    evaluation.denied_reason,
                )
            raise PolicyDeniedError(
                evaluation.denied_reason or "Transaction denied by spending policy",
                policy_id=evaluation.policy_id,
            )

        # 5. Delegate to inner protocol
        result = await self._inner.transfer(request)

        # 6. Record spending (fire-and-forget — does not block response)
        self._fire_and_forget_record(request.from_agent, zone_id, request.amount)

        return result

    def _fire_and_forget_record(self, agent_id: str, zone_id: str, amount: Decimal) -> None:
        """Schedule spending recording as a background task."""
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(
                self._safe_record_spending(agent_id, zone_id, amount),
            )
        except RuntimeError:
            logger.warning("Cannot record spending: no running event loop")

    async def _safe_record_spending(self, agent_id: str, zone_id: str, amount: Decimal) -> None:
        """Record spending with error logging (fire-and-forget safe)."""
        try:
            await self._policy_service.record_spending(
                agent_id=agent_id,
                zone_id=zone_id,
                amount=amount,
            )
        except Exception:
            logger.exception(
                "Failed to record spending: agent=%s zone=%s amount=%s",
                agent_id,
                zone_id,
                amount,
            )
