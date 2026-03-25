"""Governance RPC Service — anomaly alerts, fraud rings, constraints, suspensions.

Issue #1520, #1359.
"""

import logging
from typing import Any

from nexus.contracts.rpc import rpc_expose

logger = logging.getLogger(__name__)


class GovernanceRPCService:
    """RPC surface for governance and anti-fraud operations."""

    def __init__(
        self,
        anomaly_service: Any | None = None,
        collusion_service: Any | None = None,
        graph_service: Any | None = None,
        response_service: Any | None = None,
        nexus_fs: Any | None = None,
    ) -> None:
        self._anomaly_service = anomaly_service
        self._collusion_service = collusion_service
        self._graph_service = graph_service
        self._response_service = response_service
        self._nexus_fs = nexus_fs

    # --- Anomaly Detection ---

    @rpc_expose(description="List anomaly alerts")
    async def governance_alerts(
        self,
        zone_id: str = "root",
        severity: str | None = None,
        resolved: bool | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        if self._anomaly_service is None:
            return {"alerts": [], "count": 0}
        alerts = await self._anomaly_service.get_alerts(
            zone_id=zone_id, severity=severity, resolved=resolved
        )
        alert_list = [
            {
                "alert_id": a.alert_id,
                "agent_id": a.agent_id,
                "zone_id": a.zone_id,
                "severity": a.severity,
                "alert_type": a.alert_type,
                "details": a.details,
                "resolved": a.resolved,
                "created_at": a.created_at.isoformat() if a.created_at else None,
            }
            for a in (alerts[:limit] if alerts else [])
        ]
        return {"alerts": alert_list, "count": len(alert_list)}

    @rpc_expose(description="Resolve an anomaly alert", admin_only=True)
    async def governance_resolve_alert(
        self, alert_id: str, resolved_by: str, zone_id: str = "root"
    ) -> dict[str, Any]:
        if self._anomaly_service is None:
            return {"error": "Anomaly service not available"}
        alert = await self._anomaly_service.resolve_alert(
            alert_id=alert_id, resolved_by=resolved_by, zone_id=zone_id
        )
        if alert is None:
            return {"error": f"Alert {alert_id} not found"}
        return {"alert_id": alert.alert_id, "resolved": alert.resolved}

    # --- Collusion Detection ---

    @rpc_expose(description="List stored fraud scores")
    async def governance_fraud_scores(self, zone_id: str = "root") -> dict[str, Any]:
        if self._collusion_service is None:
            return {"scores": [], "count": 0}
        scores = await self._collusion_service.list_fraud_scores(zone_id=zone_id)
        return {
            "scores": [
                {
                    "agent_id": s.agent_id,
                    "zone_id": s.zone_id,
                    "score": s.score,
                    "components": s.components,
                    "computed_at": s.computed_at.isoformat() if s.computed_at else None,
                }
                for s in scores
            ],
            "count": len(scores),
        }

    @rpc_expose(description="Get fraud score for a specific agent")
    async def governance_fraud_score(self, agent_id: str, zone_id: str = "root") -> dict[str, Any]:
        if self._collusion_service is None:
            return {"error": "Collusion service not available"}
        score = await self._collusion_service.get_fraud_score(agent_id=agent_id, zone_id=zone_id)
        if score is None:
            return {"error": f"No fraud score for agent {agent_id}"}
        return {
            "agent_id": score.agent_id,
            "score": score.score,
            "components": score.components,
        }

    @rpc_expose(description="Recompute fraud scores for all agents", admin_only=True)
    async def governance_compute_fraud_scores(self, zone_id: str = "root") -> dict[str, Any]:
        if self._collusion_service is None:
            return {"error": "Collusion service not available"}
        scores = await self._collusion_service.compute_and_persist_fraud_scores(zone_id=zone_id)
        return {"count": len(scores)}

    @rpc_expose(description="List detected fraud rings")
    async def governance_rings(self, zone_id: str = "root") -> dict[str, Any]:
        if self._collusion_service is None:
            return {"rings": [], "count": 0}
        rings = await self._collusion_service.detect_rings(zone_id=zone_id)
        ring_list = [
            {
                "ring_id": r.ring_id,
                "zone_id": r.zone_id,
                "agents": r.agents,
                "ring_type": r.ring_type,
                "confidence": r.confidence,
                "total_volume": r.total_volume,
                "detected_at": r.detected_at.isoformat() if r.detected_at else None,
            }
            for r in (rings or [])
        ]
        return {"rings": ring_list, "count": len(ring_list)}

    # --- Governance Graph (Constraints) ---

    @rpc_expose(description="Add a governance constraint between agents", admin_only=True)
    async def governance_add_constraint(
        self,
        from_agent: str,
        to_agent: str,
        zone_id: str = "root",
        constraint_type: str = "block",
        reason: str = "",
    ) -> dict[str, Any]:
        if self._graph_service is None:
            return {"error": "Graph service not available"}
        edge = await self._graph_service.add_constraint(
            from_agent=from_agent,
            to_agent=to_agent,
            zone_id=zone_id,
            constraint_type=constraint_type,
            reason=reason,
        )
        return {"edge_id": edge.edge_id, "from_node": edge.from_node, "to_node": edge.to_node}

    @rpc_expose(description="List governance constraints")
    async def governance_list_constraints(
        self, zone_id: str = "root", agent_id: str | None = None
    ) -> dict[str, Any]:
        if self._graph_service is None:
            return {"constraints": [], "count": 0}
        constraints = await self._graph_service.list_constraints(zone_id=zone_id, agent_id=agent_id)
        return {
            "constraints": [
                {
                    "edge_id": c.edge_id,
                    "from_node": c.from_node,
                    "to_node": c.to_node,
                    "zone_id": c.zone_id,
                    "metadata": c.metadata,
                }
                for c in constraints
            ],
            "count": len(constraints),
        }

    @rpc_expose(description="Remove a governance constraint", admin_only=True)
    async def governance_remove_constraint(
        self, edge_id: str, zone_id: str = "root"
    ) -> dict[str, Any]:
        if self._graph_service is None:
            return {"error": "Graph service not available"}
        removed = await self._graph_service.remove_constraint(edge_id, zone_id=zone_id)
        return {"removed": removed, "edge_id": edge_id}

    @rpc_expose(description="Check governance constraint between two agents")
    async def governance_check_constraint(
        self, from_agent: str, to_agent: str, zone_id: str = "root"
    ) -> dict[str, Any]:
        if self._graph_service is None:
            return {"allowed": True, "reason": "No graph service"}
        result = await self._graph_service.check_constraint(
            from_agent=from_agent, to_agent=to_agent, zone_id=zone_id
        )
        return {
            "allowed": result.allowed,
            "constraint_type": result.constraint_type if result.constraint_type else None,
            "reason": result.reason,
        }

    # --- Suspensions & Appeals ---

    @rpc_expose(description="Suspend an agent", admin_only=True)
    async def governance_suspend(
        self,
        agent_id: str,
        reason: str,
        zone_id: str = "root",
        duration_hours: float = 24.0,
        severity: str = "high",
    ) -> dict[str, Any]:
        if self._response_service is None:
            return {"error": "Response service not available"}
        record = await self._response_service.suspend_agent(
            agent_id=agent_id,
            zone_id=zone_id,
            reason=reason,
            duration_hours=duration_hours,
            severity=severity,
        )
        return {
            "suspension_id": record.suspension_id,
            "agent_id": record.agent_id,
            "zone_id": record.zone_id,
        }

    @rpc_expose(description="List suspensions")
    async def governance_suspensions(
        self, zone_id: str = "root", agent_id: str | None = None
    ) -> dict[str, Any]:
        if self._response_service is None:
            return {"suspensions": [], "count": 0}
        records = await self._response_service.list_suspensions(zone_id=zone_id, agent_id=agent_id)
        return {
            "suspensions": [
                {
                    "suspension_id": r.suspension_id,
                    "agent_id": r.agent_id,
                    "zone_id": r.zone_id,
                    "reason": r.reason,
                    "severity": r.severity,
                    "appeal_status": r.appeal_status,
                }
                for r in records
            ],
            "count": len(records),
        }

    @rpc_expose(description="Appeal a suspension")
    async def governance_appeal(
        self, suspension_id: str, reason: str, zone_id: str = "root"
    ) -> dict[str, Any]:
        if self._response_service is None:
            return {"error": "Response service not available"}
        record = await self._response_service.appeal_suspension(
            suspension_id=suspension_id, reason=reason, zone_id=zone_id
        )
        return {
            "suspension_id": record.suspension_id,
            "appeal_status": record.appeal_status,
        }

    @rpc_expose(description="Decide on a suspension appeal", admin_only=True)
    async def governance_decide_appeal(
        self,
        suspension_id: str,
        approved: bool,
        decided_by: str,
        zone_id: str = "root",
    ) -> dict[str, Any]:
        if self._response_service is None:
            return {"error": "Response service not available"}
        record = await self._response_service.decide_appeal(
            suspension_id=suspension_id,
            approved=approved,
            decided_by=decided_by,
            zone_id=zone_id,
        )
        return {
            "suspension_id": record.suspension_id,
            "appeal_status": record.appeal_status,
        }

    # --- Hotspot Detection ---

    @rpc_expose(description="Get hotspot detection statistics", admin_only=True)
    async def governance_hotspot_stats(self) -> dict[str, Any]:
        if self._nexus_fs is None:
            return {"enabled": False, "message": "NexusFS not available"}
        enforcer = getattr(self._nexus_fs, "_permission_enforcer", None)
        if not enforcer:
            return {"enabled": False, "message": "Permission enforcer not available"}
        detector = getattr(enforcer, "_hotspot_detector", None)
        if not detector:
            return {"enabled": False, "message": "Hotspot tracking not enabled"}
        stats: dict[str, Any] = detector.get_stats()
        return stats

    @rpc_expose(description="Get current hot permission entries", admin_only=True)
    async def governance_hot_entries(self, limit: int = 10) -> dict[str, Any]:
        if self._nexus_fs is None:
            return {"entries": []}
        enforcer = getattr(self._nexus_fs, "_permission_enforcer", None)
        if not enforcer:
            return {"entries": []}
        detector = getattr(enforcer, "_hotspot_detector", None)
        if not detector:
            return {"entries": []}
        entries = detector.get_hot_entries(limit=limit)
        return {
            "entries": [
                {
                    "subject_type": e.subject_type,
                    "subject_id": e.subject_id,
                    "resource_type": e.resource_type,
                    "permission": e.permission,
                    "zone_id": e.zone_id,
                    "access_count": e.access_count,
                    "last_access": e.last_access,
                }
                for e in entries
            ],
        }

    @rpc_expose(description="Get governance overview (alerts + rings)")
    async def governance_status(self) -> dict[str, Any]:
        recent_alerts = await self.governance_alerts(limit=5)
        fraud_rings = await self.governance_rings()
        return {"recent_alerts": recent_alerts, "fraud_rings": fraud_rings}
