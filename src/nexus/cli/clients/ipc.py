"""IPC (agent-to-agent messaging) HTTP client for CLI."""

from __future__ import annotations

from typing import Any

from nexus.cli.clients.base import BaseServiceClient


class IPCClient(BaseServiceClient):
    """Client for IPC messaging endpoints."""

    def send(
        self,
        to_agent: str,
        message: str,
        *,
        message_type: str = "task",
        zone_id: str | None = None,
    ) -> dict[str, Any]:
        """Send message to agent inbox."""
        body: dict[str, Any] = {
            "to_agent": to_agent,
            "body": message,
            "message_type": message_type,
        }
        if zone_id:
            body["zone_id"] = zone_id
        return self._request("POST", "/api/v2/ipc/send", json_body=body)

    def inbox(
        self,
        agent_id: str,
        *,
        limit: int = 50,
    ) -> dict[str, Any]:
        """List messages in agent inbox."""
        return self._request(
            "GET",
            f"/api/v2/ipc/inbox/{agent_id}",
            params={"limit": limit},
        )

    def inbox_count(self, agent_id: str) -> dict[str, Any]:
        """Count messages in agent inbox."""
        return self._request("GET", f"/api/v2/ipc/inbox/{agent_id}/count")
