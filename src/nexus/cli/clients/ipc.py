"""IPC (agent-to-agent messaging) HTTP client for CLI."""

from __future__ import annotations

from typing import Any

from nexus.cli.clients.base import BaseServiceClient


class IPCClient(BaseServiceClient):
    """Client for IPC messaging endpoints."""

    def send(
        self,
        sender: str,
        recipient: str,
        message: str,
        *,
        message_type: str = "task",
        ttl_seconds: int | None = None,
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        """Send message to agent inbox."""
        body: dict[str, Any] = {
            "sender": sender,
            "recipient": recipient,
            "type": message_type,
            "payload": {"body": message},
        }
        if ttl_seconds is not None:
            body["ttl_seconds"] = ttl_seconds
        if correlation_id is not None:
            body["correlation_id"] = correlation_id
        return self._request("POST", "/api/v2/ipc/send", json_body=body)

    def inbox(self, agent_id: str) -> dict[str, Any]:
        """List messages in agent inbox."""
        return self._request("GET", f"/api/v2/ipc/inbox/{agent_id}")

    def inbox_count(self, agent_id: str) -> dict[str, Any]:
        """Count messages in agent inbox."""
        return self._request("GET", f"/api/v2/ipc/inbox/{agent_id}/count")
