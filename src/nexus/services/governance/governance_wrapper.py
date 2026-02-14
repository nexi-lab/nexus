"""GovernanceEnforcedPayment — PaymentProtocol wrapper for governance checks.

Issue #1359 Phase 3: Wraps any PaymentProtocol with governance constraint
checks and anomaly detection. Follows the same wrapper pattern as
PolicyEnforcedPayment (Lego Architecture Mechanism 2).

Wrapper chain: GovernanceEnforcedPayment → PolicyEnforcedPayment → CreditsPaymentProtocol
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from nexus.core.sync_bridge import fire_and_forget
from nexus.pay.audit_types import TransactionProtocol
from nexus.pay.protocol import PaymentProtocol, ProtocolTransferRequest, ProtocolTransferResult

if TYPE_CHECKING:
    from nexus.services.governance.anomaly_service import AnomalyService
    from nexus.services.governance.governance_graph_service import GovernanceGraphService

logger = logging.getLogger(__name__)


class GovernanceBlockedError(Exception):
    """Raised when a transaction is blocked by a governance constraint."""

    def __init__(self, message: str, *, edge_id: str | None = None) -> None:
        super().__init__(message)
        self.edge_id = edge_id


class GovernanceApprovalRequired(Exception):
    """Raised when a transaction requires governance approval."""

    def __init__(self, message: str, *, edge_id: str | None = None) -> None:
        super().__init__(message)
        self.edge_id = edge_id


class GovernanceEnforcedPayment(PaymentProtocol):
    """Wraps a PaymentProtocol with governance constraint checks.

    Flow:
        1. Pre-check: check_constraint (sync, <1ms cached)
        2. If BLOCK → raise GovernanceBlockedError
        3. If REQUIRE_APPROVAL → raise GovernanceApprovalRequired
        4. Delegate to inner protocol
        5. Post-analysis: fire-and-forget anomaly detection
    """

    def __init__(
        self,
        inner: PaymentProtocol,
        graph_service: GovernanceGraphService,
        anomaly_service: AnomalyService,
    ) -> None:
        self._inner = inner
        self._graph_service = graph_service
        self._anomaly_service = anomaly_service

    @property
    def protocol_name(self) -> TransactionProtocol:
        """Delegate to inner protocol."""
        return self._inner.protocol_name

    def can_handle(self, to: str, metadata: dict[str, Any] | None = None) -> bool:
        """Delegate to inner protocol."""
        return self._inner.can_handle(to, metadata)

    async def transfer(self, request: ProtocolTransferRequest) -> ProtocolTransferResult:
        """Execute transfer with governance enforcement.

        Pre-check: governance constraint check (~1ms).
        Post-analysis: fire-and-forget anomaly detection.
        """
        zone_id = request.metadata.get("zone_id", "default") if request.metadata else "default"

        # 1. Pre-check: governance constraints
        from nexus.services.governance.models import ConstraintType

        check = await self._graph_service.check_constraint(
            from_agent=request.from_agent,
            to_agent=request.to,
            zone_id=zone_id,
        )

        if not check.allowed:
            if check.constraint_type == ConstraintType.BLOCK:
                raise GovernanceBlockedError(
                    check.reason or "Transaction blocked by governance constraint",
                    edge_id=check.edge_id,
                )
            if check.constraint_type == ConstraintType.REQUIRE_APPROVAL:
                raise GovernanceApprovalRequired(
                    check.reason or "Transaction requires governance approval",
                    edge_id=check.edge_id,
                )
            # RATE_LIMIT or unknown — block by default
            raise GovernanceBlockedError(
                check.reason or "Transaction restricted by governance policy",
                edge_id=check.edge_id,
            )

        # 2. Delegate to inner protocol
        result = await self._inner.transfer(request)

        # 3. Post-analysis: fire-and-forget anomaly detection
        self._fire_and_forget_analysis(
            agent_id=request.from_agent,
            zone_id=zone_id,
            amount=float(request.amount),
            to=request.to,
        )

        return result

    def _fire_and_forget_analysis(
        self,
        agent_id: str,
        zone_id: str,
        amount: float,
        to: str,
    ) -> None:
        """Schedule anomaly analysis as a background task.

        Uses ``fire_and_forget`` from ``sync_bridge`` so this works
        correctly from both async and sync calling contexts.
        """
        fire_and_forget(self._safe_analyze(agent_id, zone_id, amount, to))

    async def _safe_analyze(
        self,
        agent_id: str,
        zone_id: str,
        amount: float,
        to: str,
    ) -> None:
        """Analyze transaction with error logging (fire-and-forget safe)."""
        try:
            await self._anomaly_service.analyze_transaction(
                agent_id=agent_id,
                zone_id=zone_id,
                amount=amount,
                to=to,
            )
        except Exception:
            logger.exception(
                "Failed to analyze transaction: agent=%s zone=%s",
                agent_id,
                zone_id,
            )
