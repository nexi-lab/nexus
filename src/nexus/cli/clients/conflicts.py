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

    def resolve(self, conflict_id: str, *, strategy: str) -> dict[str, Any]:
        """Resolve a conflict with the given strategy."""
        return self._request(
            "POST",
            f"/api/v2/sync/conflicts/{conflict_id}/resolve",
            json_body={"strategy": strategy},
        )
