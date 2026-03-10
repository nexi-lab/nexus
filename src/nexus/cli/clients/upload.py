"""Upload management HTTP client for CLI."""

from __future__ import annotations

from typing import Any

from nexus.cli.clients.base import BaseServiceClient, NexusAPIError


class UploadClient(BaseServiceClient):
    """Client for tus.io upload management endpoints."""

    def list(self) -> dict[str, Any]:
        """List active uploads."""
        return self._request("GET", "/api/v2/uploads")

    def status(self, upload_id: str) -> dict[str, Any]:
        """Get upload offset/status via HEAD request."""
        response = self._client.request("HEAD", f"/api/v2/uploads/{upload_id}")
        if response.status_code >= 400:
            raise NexusAPIError(response.status_code, "Upload not found")
        return {
            "upload_id": upload_id,
            "offset": int(response.headers.get("Upload-Offset", "0")),
            "length": int(response.headers.get("Upload-Length", "0")),
        }

    def cancel(self, upload_id: str) -> dict[str, Any]:
        """Cancel an in-progress upload."""
        return self._request("DELETE", f"/api/v2/uploads/{upload_id}")
