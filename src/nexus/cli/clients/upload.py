"""Upload management HTTP client for CLI."""

from __future__ import annotations

from typing import Any

from nexus.cli.clients.base import BaseServiceClient, NexusAPIError

_TUS_HEADERS = {"Tus-Resumable": "1.0.0"}


class UploadClient(BaseServiceClient):
    """Client for tus.io upload management endpoints.

    Note: The tus protocol does not expose a list endpoint.
    Use ``status`` to check individual uploads.
    """

    def status(self, upload_id: str) -> dict[str, Any]:
        """Get upload offset/status via HEAD request (tus protocol)."""
        response = self._client.request(
            "HEAD",
            f"/api/v2/uploads/{upload_id}",
            headers=_TUS_HEADERS,
        )
        if response.status_code >= 400:
            raise NexusAPIError(response.status_code, "Upload not found")
        return {
            "upload_id": upload_id,
            "offset": int(response.headers.get("Upload-Offset", "0")),
            "length": int(response.headers.get("Upload-Length", "0")),
        }

    def cancel(self, upload_id: str) -> dict[str, Any]:
        """Cancel an in-progress upload (tus DELETE with Tus-Resumable header)."""
        response = self._client.request(
            "DELETE",
            f"/api/v2/uploads/{upload_id}",
            headers=_TUS_HEADERS,
        )
        if response.status_code >= 400:
            try:
                detail = response.json().get("detail", response.text)
            except Exception:
                detail = response.text
            raise NexusAPIError(response.status_code, str(detail))
        return {}
