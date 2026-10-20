"""Base HTTP client for domain-specific Nexus CLI clients."""

from __future__ import annotations

from typing import Any, Self

__all__ = ["BaseServiceClient", "NexusAPIError"]


class NexusAPIError(Exception):
    """Error from Nexus REST API."""

    def __init__(self, status_code: int, detail: str) -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"HTTP {status_code}: {detail}")


class BaseServiceClient:
    """Shared HTTP client base for Nexus service-level REST endpoints.

    Subclasses bind the native host's domain endpoints.
    """

    def __init__(self, url: str, api_key: str | None = None, *, timeout: float = 30.0) -> None:
        import httpx

        self._url = url.rstrip("/")
        self._api_key = api_key
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        self._client = httpx.Client(base_url=self._url, headers=headers, timeout=timeout)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Make HTTP request and return parsed JSON response."""
        if params:
            params = {k: v for k, v in params.items() if v is not None}

        options: dict[str, Any] = {"timeout": timeout} if timeout is not None else {}
        response = self._client.request(method, path, params=params, json=json_body, **options)

        if response.status_code >= 400:
            try:
                detail = response.json().get("detail", response.text)
            except Exception:
                detail = response.text
            raise NexusAPIError(response.status_code, str(detail))

        if response.status_code == 204:
            return {}

        result: dict[str, Any] = response.json()
        if not isinstance(result, dict):
            raise NexusAPIError(502, "Expected a JSON object from the native host")
        if result.get("error"):
            raise NexusAPIError(503, str(result["error"]))
        return result
