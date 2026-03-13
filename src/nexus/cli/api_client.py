"""Thin HTTP client for CLI commands that talk to Nexus REST APIs (Issue #2930).

Wraps httpx with auth, base URL resolution, and error handling. Parallels
the TypeScript FetchClient in packages/nexus-api-client.

Resolution order for base URL:
1. Explicit url parameter
2. NEXUS_URL environment variable
3. Default http://localhost:2026

Resolution order for API key:
1. Explicit api_key parameter
2. NEXUS_API_KEY environment variable
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "http://localhost:2026"
DEFAULT_TIMEOUT = 30.0


class NexusApiClient:
    """HTTP client for CLI -> REST API calls."""

    def __init__(
        self,
        *,
        url: str | None = None,
        api_key: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        resolved = url or os.environ.get("NEXUS_URL") or DEFAULT_BASE_URL
        self._base_url = resolved.rstrip("/")
        self._api_key = api_key or os.environ.get("NEXUS_API_KEY", "")
        self._timeout = timeout

    def _headers(self) -> dict[str, str]:
        headers: dict[str, str] = {"Accept": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """GET request. Returns parsed JSON response."""
        url = f"{self._base_url}{path}"
        resp = httpx.get(url, headers=self._headers(), params=params, timeout=self._timeout)
        resp.raise_for_status()
        return resp.json()

    def put(self, path: str, json_body: dict[str, Any]) -> Any:
        """PUT request. Returns parsed JSON response."""
        url = f"{self._base_url}{path}"
        resp = httpx.put(url, headers=self._headers(), json=json_body, timeout=self._timeout)
        resp.raise_for_status()
        return resp.json()

    def post(self, path: str, json_body: dict[str, Any] | None = None) -> Any:
        """POST request. Returns parsed JSON response."""
        url = f"{self._base_url}{path}"
        resp = httpx.post(url, headers=self._headers(), json=json_body, timeout=self._timeout)
        resp.raise_for_status()
        return resp.json()

    def delete(self, path: str) -> None:
        """DELETE request. No return value (expects 204)."""
        url = f"{self._base_url}{path}"
        resp = httpx.delete(url, headers=self._headers(), timeout=self._timeout)
        resp.raise_for_status()


def get_api_client_from_options(
    remote_url: str | None = None,
    remote_api_key: str | None = None,
) -> NexusApiClient:
    """Build a NexusApiClient from CLI options / env vars / project config.

    Resolution order:
    1. Explicit ``remote_url`` / ``remote_api_key`` arguments
    2. ``NEXUS_URL`` / ``NEXUS_API_KEY`` environment variables (handled by NexusApiClient)
    3. Project config (``nexus.yaml`` / ``nexus.yml`` in cwd)
    4. Default ``http://localhost:2026``
    """
    # Try project config (nexus.yaml) if no explicit URL and no env var
    if not remote_url and not os.environ.get("NEXUS_URL"):
        try:
            import yaml

            for candidate in ("./nexus.yaml", "./nexus.yml"):
                p = Path(candidate)
                if p.exists():
                    with open(p) as f:
                        cfg = yaml.safe_load(f) or {}
                    ports = cfg.get("ports", {})
                    remote_url = f"http://localhost:{ports.get('http', 2026)}"
                    if not remote_api_key:
                        remote_api_key = cfg.get("api_key", "")
                    break
        except Exception:
            pass
    return NexusApiClient(url=remote_url, api_key=remote_api_key)
