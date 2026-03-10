"""Agent status extension HTTP client for CLI."""

from __future__ import annotations

from typing import Any

from nexus.cli.clients.base import BaseServiceClient


class AgentExtClient(BaseServiceClient):
    """Client for agent status, spec, and warmup endpoints."""

    def status(self, agent_id: str) -> dict[str, Any]:
        """Get agent runtime status."""
        return self._request("GET", f"/api/v2/agents/{agent_id}/status")

    def spec_show(self, agent_id: str) -> dict[str, Any]:
        """Get agent spec."""
        return self._request("GET", f"/api/v2/agents/{agent_id}/spec")

    def spec_set(self, agent_id: str, spec: dict[str, Any]) -> dict[str, Any]:
        """Set agent spec."""
        return self._request("PUT", f"/api/v2/agents/{agent_id}/spec", json_body=spec)

    def warmup(self, agent_id: str) -> dict[str, Any]:
        """Trigger agent warmup."""
        return self._request("POST", f"/api/v2/agents/{agent_id}/warmup")
