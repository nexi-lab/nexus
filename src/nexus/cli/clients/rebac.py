"""Bindings for complete native ReBAC tuples; authorization stays in the daemon."""

from __future__ import annotations

from typing import Any

from nexus.cli.clients.base import BaseServiceClient, NexusAPIError


class RebacClient(BaseServiceClient):
    def list(self, zone: str) -> dict[str, Any]:
        result = self._request("GET", "/v2/rebac/tuples", params={"zone": zone})
        if not isinstance(result.get("tuples"), list):
            raise NexusAPIError(502, "Invalid native tuple listing")
        return result

    def grant(self, body: dict[str, str]) -> dict[str, Any]:
        return self._request("POST", "/v2/rebac/tuples", json_body=body)

    def revoke(self, body: dict[str, str]) -> dict[str, Any]:
        result = self._request("DELETE", "/v2/rebac/tuples", json_body=body)
        if type(result.get("existed")) is not bool:
            raise NexusAPIError(502, "Invalid native tuple revocation receipt")
        return result
