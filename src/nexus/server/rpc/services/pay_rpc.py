"""Pay RPC Service — balance, transfer, history.

Issue #1520.
"""

import logging
from decimal import Decimal
from typing import Any

from nexus.contracts.rpc import rpc_expose

logger = logging.getLogger(__name__)


class PayRPCService:
    """RPC surface for agent payment operations."""

    def __init__(self, credits_service: Any) -> None:
        self._credits_service = credits_service

    @rpc_expose(description="Get agent credit balance")
    async def pay_balance(self, agent_id: str | None = None) -> dict[str, Any]:
        agent_id = agent_id or "anonymous"
        available, reserved = await self._credits_service.get_balance_with_reserved(agent_id)
        return {
            "available": str(available),
            "reserved": str(reserved),
            "total": str(available + reserved),
        }

    @rpc_expose(description="Transfer credits to another agent")
    async def pay_transfer(
        self,
        to: str,
        amount: str,
        memo: str = "",
        method: str = "auto",
        from_agent: str = "anonymous",
    ) -> dict[str, Any]:
        dec_amount = Decimal(amount)
        tx_id = await self._credits_service.transfer(
            from_id=from_agent,
            to_id=to,
            amount=dec_amount,
            memo=memo,
        )
        return {
            "id": tx_id,
            "method": method,
            "amount": amount,
            "from_agent": from_agent,
            "to_agent": to,
            "memo": memo,
        }

    @rpc_expose(description="Get payment history")
    async def pay_history(
        self,
        since: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        audit_logger = getattr(self._credits_service, "_audit_logger", None)
        if audit_logger is None:
            return {"transactions": [], "has_more": False}
        filters: dict[str, Any] = {}
        if since:
            filters["since"] = since
        result: dict[str, Any] = audit_logger.list_transactions_cursor(
            filters=filters, limit=limit, cursor=cursor
        )
        return result
