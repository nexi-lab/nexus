"""Share link HTTP client for CLI."""

from __future__ import annotations

from typing import Any

from nexus.cli.clients.base import BaseServiceClient


class ShareClient(BaseServiceClient):
    """Client for share link management endpoints.

    NOTE: The /api/v2/share-links endpoints are not yet implemented
    server-side. These commands will return 404 until the server adds
    the share-links REST router. See Issue #2812.
    """

    def create(
        self,
        path: str,
        *,
        permission_level: str = "viewer",
        expires_in_hours: int | None = None,
        password: str | None = None,
    ) -> dict[str, Any]:
        """Create a share link for a path."""
        body: dict[str, Any] = {
            "path": path,
            "permission_level": permission_level,
        }
        if expires_in_hours is not None:
            body["expires_in_hours"] = expires_in_hours
        if password:
            body["password"] = password
        return self._request("POST", "/api/v2/share-links", json_body=body)

    def list(self, *, path: str | None = None) -> dict[str, Any]:
        """List share links, optionally filtered by path."""
        return self._request("GET", "/api/v2/share-links", params={"path": path})

    def show(self, token: str) -> dict[str, Any]:
        """Get share link details by token."""
        return self._request("GET", f"/api/v2/share-links/{token}")

    def revoke(self, token: str) -> dict[str, Any]:
        """Revoke a share link."""
        return self._request("DELETE", f"/api/v2/share-links/{token}")
