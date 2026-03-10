"""Access manifest HTTP client for CLI."""

from __future__ import annotations

from typing import Any

from nexus.cli.clients.base import BaseServiceClient


class ManifestClient(BaseServiceClient):
    """Client for access manifest management endpoints."""

    def create(self, agent_id: str, *, sources: list[str]) -> dict[str, Any]:
        """Create an access manifest for an agent."""
        return self._request(
            "POST",
            "/api/v2/access-manifests",
            json_body={"agent_id": agent_id, "sources": sources},
        )

    def list(self) -> dict[str, Any]:
        """List access manifests."""
        return self._request("GET", "/api/v2/access-manifests")

    def show(self, manifest_id: str) -> dict[str, Any]:
        """Get manifest details."""
        return self._request("GET", f"/api/v2/access-manifests/{manifest_id}")

    def evaluate(self, manifest_id: str, *, tool: str) -> dict[str, Any]:
        """Evaluate a manifest against a tool request."""
        return self._request(
            "POST",
            f"/api/v2/access-manifests/{manifest_id}/evaluate",
            json_body={"tool": tool},
        )

    def revoke(self, manifest_id: str) -> dict[str, Any]:
        """Revoke an access manifest."""
        return self._request(
            "POST",
            f"/api/v2/access-manifests/{manifest_id}/revoke",
        )
