"""Authenticated native HTTP readiness, using current daemon responses."""

from __future__ import annotations

import time
from typing import Any

from nexus.cli.clients.auth import AuthClient
from nexus.cli.clients.base import BaseServiceClient, NexusAPIError


def remaining_timeout(deadline: float | None) -> float:
    if deadline is None:
        return 2.0
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("Native readiness deadline elapsed")
    return min(2.0, remaining)


class HostClient(BaseServiceClient):
    def status(self, *, timeout: float | None = None) -> dict[str, Any]:
        result = self._request("GET", "/v2/status", timeout=timeout)
        if result.get("status") != "ok" or not (
            isinstance(result.get("version"), str) and result["version"]
        ):
            raise NexusAPIError(502, "Invalid native host status")
        return result

    def probe(self, *, deadline: float | None = None) -> dict[str, Any]:
        server = self.status(timeout=remaining_timeout(deadline))
        with AuthClient(self._url, self._api_key) as client:
            identity = client.whoami(timeout=remaining_timeout(deadline))
        remaining_timeout(deadline)
        return {"server_url": self._url, "server": server, "identity": identity}
