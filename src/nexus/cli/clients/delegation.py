"""Delegation HTTP client for CLI."""

from __future__ import annotations

from typing import Any

from nexus.cli.clients.base import BaseServiceClient


class DelegationClient(BaseServiceClient):
    """Client for agent delegation endpoints."""

    def create(
        self,
        coordinator: str,
        worker: str,
        *,
        mode: str = "COPY",
        scope_prefix: str | None = None,
        ttl_seconds: int | None = None,
        zone_id: str | None = None,
    ) -> dict[str, Any]:
        """Create a delegation from coordinator to worker."""
        body: dict[str, Any] = {
            "coordinator_agent_id": coordinator,
            "worker_id": worker,
            "delegation_mode": mode,
        }
        if scope_prefix:
            body["scope_prefix"] = scope_prefix
        if ttl_seconds is not None:
            body["ttl_seconds"] = ttl_seconds
        if zone_id:
            body["zone_id"] = zone_id
        return self._request("POST", "/api/v2/agents/delegate", json_body=body)

    def list(
        self,
        coordinator_agent_id: str | None = None,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        """List delegations."""
        return self._request(
            "GET",
            "/api/v2/agents/delegate",
            params={
                "coordinator_agent_id": coordinator_agent_id,
                "limit": limit,
                "offset": offset,
            },
        )

    def revoke(self, delegation_id: str) -> dict[str, Any]:
        """Revoke a delegation."""
        return self._request("DELETE", f"/api/v2/agents/delegate/{delegation_id}")

    def show(self, delegation_id: str) -> dict[str, Any]:
        """Get delegation chain details."""
        return self._request("GET", f"/api/v2/agents/delegate/{delegation_id}/chain")

    def complete(self, delegation_id: str) -> dict[str, Any]:
        """Mark a delegation as completed."""
        return self._request("POST", f"/api/v2/agents/delegate/{delegation_id}/complete")
