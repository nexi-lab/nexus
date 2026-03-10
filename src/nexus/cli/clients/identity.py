"""Identity and credentials HTTP client for CLI."""

from __future__ import annotations

from typing import Any

from nexus.cli.clients.base import BaseServiceClient


class IdentityClient(BaseServiceClient):
    """Client for identity and credential endpoints."""

    def show(self, agent_id: str) -> dict[str, Any]:
        """Get agent identity (DID, public key, capabilities)."""
        return self._request("GET", f"/api/v2/agents/{agent_id}/identity")

    def verify(self, agent_id: str) -> dict[str, Any]:
        """Verify agent's signature/credential chain."""
        return self._request("POST", f"/api/v2/agents/{agent_id}/verify")

    def credentials_list(self, agent_id: str) -> dict[str, Any]:
        """List agent's active credentials."""
        return self._request("GET", f"/api/v2/agents/{agent_id}/credentials")

    def credential_status(self, credential_id: str) -> dict[str, Any]:
        """Get status of a specific credential."""
        return self._request("GET", f"/api/v2/credentials/{credential_id}")

    def credential_issue(
        self,
        agent_id: str,
        capabilities: list[str],
        ttl_seconds: int = 3600,
    ) -> dict[str, Any]:
        """Issue a new credential to an agent."""
        return self._request(
            "POST",
            "/api/v2/credentials/issue",
            json_body={
                "subject_agent_id": agent_id,
                "capabilities": capabilities,
                "ttl_seconds": ttl_seconds,
            },
        )
