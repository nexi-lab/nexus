"""Access manifest HTTP client for CLI."""

from __future__ import annotations

from typing import Any

from nexus.cli.clients.base import BaseServiceClient
from nexus.contracts.constants import ROOT_ZONE_ID


class ManifestClient(BaseServiceClient):
    """Client for access manifest management endpoints."""

    def create(
        self,
        agent_id: str,
        *,
        name: str,
        entries: list[dict[str, Any]],
        zone_id: str = ROOT_ZONE_ID,
        valid_hours: int = 720,
    ) -> dict[str, Any]:
        """Create an access manifest for an agent.

        Each entry in ``entries`` should have:
        - tool_pattern: str (e.g. "read_*", "write_file")
        - permission: str ("allow" or "deny")
        - max_calls_per_minute: int | None (optional rate limit)
        """
        return self._request(
            "POST",
            "/api/v2/access-manifests",
            json_body={
                "agent_id": agent_id,
                "name": name,
                "entries": entries,
                "zone_id": zone_id,
                "valid_hours": valid_hours,
            },
        )

    def list(self) -> dict[str, Any]:
        """List access manifests."""
        return self._request("GET", "/api/v2/access-manifests")

    def show(self, manifest_id: str) -> dict[str, Any]:
        """Get manifest details."""
        return self._request("GET", f"/api/v2/access-manifests/{manifest_id}")

    def evaluate(self, manifest_id: str, *, tool_name: str) -> dict[str, Any]:
        """Evaluate a manifest against a tool request."""
        return self._request(
            "POST",
            f"/api/v2/access-manifests/{manifest_id}/evaluate",
            json_body={"tool_name": tool_name},
        )

    def revoke(self, manifest_id: str) -> dict[str, Any]:
        """Revoke an access manifest."""
        return self._request(
            "POST",
            f"/api/v2/access-manifests/{manifest_id}/revoke",
        )
