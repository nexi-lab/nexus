"""Pay RPC Service — balance, transfer, history, policy, approvals.

Issue #1520.
"""

import logging
from decimal import Decimal
from typing import Any

from nexus.contracts.rpc import rpc_expose

logger = logging.getLogger(__name__)


class PayRPCService:
    """RPC surface for agent payment operations."""

    def __init__(
        self,
        credits_service: Any,
        policy_service: Any | None = None,
    ) -> None:
        self._credits_service = credits_service
        self._policy_service = policy_service

    @rpc_expose(description="Get agent credit balance")
    async def pay_balance(self, agent_id: str | None = None) -> dict[str, Any]:
        agent_id = agent_id or "anonymous"
        available, reserved = await self._credits_service.get_balance_with_reserved(agent_id)
        return {
            "available": str(available),
            "reserved": str(reserved),
            "total": str(available + reserved),
        }

    @rpc_expose(description="Check if agent can afford an amount")
    async def pay_can_afford(
        self,
        amount: str,
        agent_id: str = "anonymous",
    ) -> dict[str, Any]:
        dec = Decimal(amount)
        available, _ = await self._credits_service.get_balance_with_reserved(agent_id)
        return {"can_afford": available >= dec, "amount": amount}

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

    @rpc_expose(description="Atomic batch transfer")
    async def pay_transfer_batch(
        self,
        transfers: list[dict[str, Any]],
        from_agent: str = "anonymous",
    ) -> dict[str, Any]:
        from nexus.bricks.pay.credits import TransferRequest

        requests = [
            TransferRequest(
                from_id=from_agent,
                to_id=t["to"],
                amount=Decimal(t["amount"]),
                memo=t.get("memo", ""),
            )
            for t in transfers
        ]
        results = await self._credits_service.transfer_batch(requests)
        return {
            "receipts": [{"id": r.id, "to": r.to_id, "amount": str(r.amount)} for r in results],
            "count": len(results),
        }

    @rpc_expose(description="Reserve credits for pending operation")
    async def pay_reserve(
        self,
        amount: str,
        agent_id: str = "anonymous",
        timeout: int = 300,
        purpose: str = "general",
        task_id: str | None = None,
    ) -> dict[str, Any]:
        reservation = await self._credits_service.reserve(
            agent_id=agent_id,
            amount=Decimal(amount),
            timeout=timeout,
            purpose=purpose,
            task_id=task_id,
        )
        return {
            "id": reservation.id,
            "amount": str(reservation.amount),
            "purpose": reservation.purpose,
            "expires_at": reservation.expires_at.isoformat() if reservation.expires_at else None,
            "status": reservation.status,
        }

    @rpc_expose(description="Commit a reservation")
    async def pay_commit_reservation(
        self,
        reservation_id: str,
        actual_amount: str | None = None,
    ) -> dict[str, Any]:
        dec = Decimal(actual_amount) if actual_amount else None
        await self._credits_service.commit_reservation(reservation_id, actual_amount=dec)
        return {"committed": True, "reservation_id": reservation_id}

    @rpc_expose(description="Release a reservation")
    async def pay_release_reservation(self, reservation_id: str) -> dict[str, Any]:
        await self._credits_service.release_reservation(reservation_id)
        return {"released": True, "reservation_id": reservation_id}

    @rpc_expose(description="Record metered usage")
    async def pay_meter(
        self,
        amount: str,
        agent_id: str = "anonymous",
        event_type: str = "api_call",
    ) -> dict[str, Any]:
        success = await self._credits_service.meter(
            agent_id=agent_id,
            amount=Decimal(amount),
            event_type=event_type,
        )
        return {"success": success}

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

    # --- Spending Policy methods ---

    def _require_policy_service(self) -> Any:
        if self._policy_service is None:
            raise RuntimeError("Spending policy service not available")
        return self._policy_service

    @rpc_expose(description="Get agent budget summary")
    async def pay_budget(
        self, agent_id: str = "anonymous", zone_id: str = "root"
    ) -> dict[str, Any]:
        svc = self._require_policy_service()
        result: dict[str, Any] = await svc.get_budget_summary(agent_id, zone_id)
        return result

    @rpc_expose(description="Create a spending policy", admin_only=True)
    async def pay_create_policy(
        self,
        zone_id: str = "root",
        agent_id: str | None = None,
        daily_limit: str | None = None,
        weekly_limit: str | None = None,
        monthly_limit: str | None = None,
        per_tx_limit: str | None = None,
        auto_approve_threshold: str | None = None,
        max_tx_per_hour: int | None = None,
        max_tx_per_day: int | None = None,
        priority: int = 0,
        enabled: bool = True,
    ) -> dict[str, Any]:
        svc = self._require_policy_service()
        policy = await svc.create_policy(
            zone_id=zone_id,
            agent_id=agent_id,
            daily_limit=Decimal(daily_limit) if daily_limit else None,
            weekly_limit=Decimal(weekly_limit) if weekly_limit else None,
            monthly_limit=Decimal(monthly_limit) if monthly_limit else None,
            per_tx_limit=Decimal(per_tx_limit) if per_tx_limit else None,
            auto_approve_threshold=Decimal(auto_approve_threshold)
            if auto_approve_threshold
            else None,
            max_tx_per_hour=max_tx_per_hour,
            max_tx_per_day=max_tx_per_day,
            priority=priority,
            enabled=enabled,
        )
        return {
            "policy_id": policy.policy_id,
            "zone_id": policy.zone_id,
            "agent_id": policy.agent_id,
        }

    @rpc_expose(description="List spending policies", admin_only=True)
    async def pay_list_policies(self, zone_id: str = "root") -> dict[str, Any]:
        svc = self._require_policy_service()
        policies = await svc.list_policies(zone_id)
        return {
            "policies": [
                {
                    "policy_id": p.policy_id,
                    "zone_id": p.zone_id,
                    "agent_id": p.agent_id,
                    "enabled": p.enabled,
                }
                for p in policies
            ],
            "count": len(policies),
        }

    @rpc_expose(description="Delete a spending policy", admin_only=True)
    async def pay_delete_policy(self, policy_id: str) -> dict[str, Any]:
        svc = self._require_policy_service()
        deleted = await svc.delete_policy(policy_id)
        return {"deleted": deleted, "policy_id": policy_id}

    @rpc_expose(description="Request approval for a transaction")
    async def pay_request_approval(
        self,
        amount: str,
        to: str,
        memo: str = "",
        agent_id: str = "anonymous",
        zone_id: str = "root",
    ) -> dict[str, Any]:
        svc = self._require_policy_service()
        policy = await svc.get_policy(agent_id, zone_id)
        if policy is None:
            policy = await svc.get_policy(None, zone_id)
        if policy is None:
            return {"error": "No policy found for this agent"}
        approval = await svc.request_approval(
            policy_id=policy.policy_id,
            agent_id=agent_id,
            zone_id=zone_id,
            amount=Decimal(amount),
            to=to,
            memo=memo,
        )
        return {
            "approval_id": approval.approval_id,
            "status": approval.status,
        }

    @rpc_expose(description="List pending approvals", admin_only=True)
    async def pay_list_approvals(self, zone_id: str = "root") -> dict[str, Any]:
        svc = self._require_policy_service()
        approvals = await svc.list_pending_approvals(zone_id)
        return {
            "approvals": [
                {
                    "approval_id": a.approval_id,
                    "agent_id": a.agent_id,
                    "amount": str(a.amount),
                    "status": a.status,
                }
                for a in approvals
            ],
            "count": len(approvals),
        }

    @rpc_expose(description="Approve a pending spending request", admin_only=True)
    async def pay_approve_spending(
        self, approval_id: str, decided_by: str = "admin"
    ) -> dict[str, Any]:
        svc = self._require_policy_service()
        result = await svc.approve_request(approval_id, decided_by=decided_by)
        if result is None:
            return {"error": "Approval not found or already decided"}
        return {"approval_id": result.approval_id, "status": result.status}

    @rpc_expose(description="Reject a pending spending request", admin_only=True)
    async def pay_reject_spending(
        self, approval_id: str, decided_by: str = "admin"
    ) -> dict[str, Any]:
        svc = self._require_policy_service()
        result = await svc.reject_request(approval_id, decided_by=decided_by)
        if result is None:
            return {"error": "Approval not found or already decided"}
        return {"approval_id": result.approval_id, "status": result.status}
