"""OCC conflict resolution HTTP client for CLI."""

from __future__ import annotations

from typing import Any

from nexus.cli.clients.base import BaseServiceClient


class ConflictsClient(BaseServiceClient):
    """Client for sync conflict detection and resolution endpoints."""

    def list(self) -> dict[str, Any]:
        """List unresolved conflicts."""
        return self._request("GET", "/api/v2/sync/conflicts")

    def show(self, conflict_id: str) -> dict[str, Any]:
        """Get conflict details."""
        return self._request("GET", f"/api/v2/sync/conflicts/{conflict_id}")

    def resolve(self, conflict_id: str, *, outcome: str) -> dict[str, Any]:
        """Resolve a conflict.

        Args:
            conflict_id: The conflict to resolve.
            outcome: Resolution outcome — "nexus_wins" or "backend_wins".
        """
        return self._request(
            "POST",
            f"/api/v2/sync/conflicts/{conflict_id}/resolve",
            json_body={"outcome": outcome},
        )
