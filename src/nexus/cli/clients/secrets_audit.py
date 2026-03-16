"""Secrets audit HTTP client for CLI."""

from __future__ import annotations

from typing import Any

from nexus.cli.clients.base import BaseServiceClient


class SecretsAuditClient(BaseServiceClient):
    """Client for secrets audit log endpoints."""

    def list(
        self,
        *,
        since: str | None = None,
        action: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """List audit events."""
        return self._request(
            "GET",
            "/api/v2/secrets-audit/events",
            params={"since": since, "action": action, "limit": limit},
        )

    def export(self, *, fmt: str = "json", since: str | None = None) -> str:
        """Export audit events as raw text."""
        return self._request_text(
            "GET",
            "/api/v2/secrets-audit/events/export",
            params={"format": fmt, "since": since},
        )

    def show(self, record_id: str) -> dict[str, Any]:
        """Get a single audit record."""
        return self._request("GET", f"/api/v2/secrets-audit/events/{record_id}")

    def verify(self, record_id: str) -> dict[str, Any]:
        """Verify integrity of an audit record."""
        return self._request("GET", f"/api/v2/secrets-audit/integrity/{record_id}")
