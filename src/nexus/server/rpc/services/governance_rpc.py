"""Governance RPC Service — anomaly alerts and fraud ring detection.

Issue #1520.
"""

import logging
from typing import Any

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.contracts.rpc import rpc_expose

logger = logging.getLogger(__name__)


class GovernanceRPCService:
    """RPC surface for governance and anti-fraud operations."""

    def __init__(
        self,
        anomaly_service: Any | None = None,
        collusion_service: Any | None = None,
    ) -> None:
        self._anomaly_service = anomaly_service
        self._collusion_service = collusion_service

    @rpc_expose(description="List anomaly alerts", admin_only=True)
    async def governance_alerts(
        self,
        severity: str | None = None,
        limit: int = 50,
        _context: Any = None,
    ) -> dict[str, Any]:
        if self._anomaly_service is None:
            return {"alerts": [], "count": 0}
        parsed_severity = None
        if severity:
            from nexus.bricks.governance.models import AnomalySeverity

            parsed_severity = AnomalySeverity(severity)
        zone_id = getattr(_context, "zone_id", None) or ROOT_ZONE_ID
        try:
            alerts = await self._anomaly_service.get_alerts(
                zone_id=zone_id,
                severity=parsed_severity,
            )
        except Exception as exc:
            logger.debug("Governance alerts unavailable: %s", exc)
            return {"alerts": [], "count": 0}
        alert_list = [
            {
                "alert_id": str(getattr(a, "alert_id", "")),
                "agent_id": getattr(a, "agent_id", ""),
                "severity": getattr(a, "severity", ""),
                "description": getattr(a, "details", ""),
                "created_at": str(getattr(a, "created_at", "")),
            }
            for a in (alerts[:limit] if alerts else [])
        ]
        return {"alerts": alert_list, "count": len(alert_list)}

    @rpc_expose(description="List detected fraud rings", admin_only=True)
    async def governance_rings(self, _context: Any = None) -> dict[str, Any]:
        if self._collusion_service is None:
            return {"rings": [], "count": 0}
        zone_id = getattr(_context, "zone_id", None) or ROOT_ZONE_ID
        try:
            rings = await self._collusion_service.detect_rings(zone_id=zone_id)
        except Exception as exc:
            logger.debug("Governance rings unavailable: %s", exc)
            return {"rings": [], "count": 0}
        ring_list = [
            {
                "ring_id": str(getattr(r, "ring_id", "")),
                "members": getattr(r, "agents", []),
                "risk_score": getattr(r, "confidence", 0.0),
                "detected_at": str(getattr(r, "detected_at", "")),
            }
            for r in (rings or [])
        ]
        return {"rings": ring_list, "count": len(ring_list)}

    @rpc_expose(description="Get governance overview (alerts + rings)", admin_only=True)
    async def governance_status(self, _context: Any = None) -> dict[str, Any]:
        recent_alerts = await self.governance_alerts(limit=5, _context=_context)
        fraud_rings = await self.governance_rings(_context=_context)
        return {"recent_alerts": recent_alerts, "fraud_rings": fraud_rings}
