"""HTTP transport for proxy brick.

Wraps ``httpx.AsyncClient`` with connection pooling, HTTP/2, and
per-call retry via ``tenacity``.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any
from urllib.parse import urljoin

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from nexus.proxy.config import ProxyBrickConfig
from nexus.proxy.errors import RemoteCallError
from nexus.server.protocol import decode_rpc_message

logger = logging.getLogger(__name__)


class HttpTransport:
    """HTTP transport for forwarding proxy calls to a remote kernel.

    Parameters
    ----------
    config:
        Proxy configuration (timeouts, pooling, auth, retry).
    client:
        Optional pre-configured ``httpx.AsyncClient`` (for testing).
    """

    def __init__(
        self,
        config: ProxyBrickConfig,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._config = config
        self._remote_url = config.remote_url.rstrip("/")

        if client is not None:
            self._client = client
            self._owns_client = False
        else:
            limits = httpx.Limits(
                max_connections=config.max_connections,
                max_keepalive_connections=config.max_keepalive,
            )
            timeout = httpx.Timeout(
                connect=config.connect_timeout,
                read=config.request_timeout,
                write=config.request_timeout,
                pool=config.request_timeout,
            )
            headers = self._auth_headers(config.api_key)
            self._client = httpx.AsyncClient(
                limits=limits,
                timeout=timeout,
                headers=headers,
                http2=config.http2,
            )
            self._owns_client = True

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        """Make a JSON-RPC-style call to the remote kernel."""
        return await self._call_with_retry(method, params)

    async def stream_upload(
        self,
        method: str,
        data: bytes,
        params: dict[str, Any] | None = None,
    ) -> Any:
        """Upload a large payload via streaming POST."""
        url = urljoin(self._remote_url + "/", f"api/nfs/{method}")
        headers = {
            "Content-Type": "application/octet-stream",
            "X-RPC-Params": _safe_json(params),
        }
        try:
            resp = await self._client.post(url, content=data, headers=headers)
            resp.raise_for_status()
            return resp.json().get("result")
        except httpx.HTTPStatusError as exc:
            raise RemoteCallError(method, status_code=exc.response.status_code, cause=exc) from exc
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            raise RemoteCallError(method, cause=exc) from exc

    async def stream_download(self, method: str, params: dict[str, Any] | None = None) -> bytes:
        """Download a large payload as raw bytes."""
        url = urljoin(self._remote_url + "/", f"api/nfs/{method}")
        headers = {"Accept": "application/octet-stream"}
        if params:
            headers["X-RPC-Params"] = _safe_json(params)
        try:
            resp = await self._client.get(url, headers=headers)
            resp.raise_for_status()
            return resp.content
        except httpx.HTTPStatusError as exc:
            raise RemoteCallError(method, status_code=exc.response.status_code, cause=exc) from exc
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            raise RemoteCallError(method, cause=exc) from exc

    async def close(self) -> None:
        """Close the HTTP client if we own it."""
        if self._owns_client:
            await self._client.aclose()

    @property
    def auth_headers(self) -> dict[str, str]:
        return self._auth_headers(self._config.api_key)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _call_with_retry(self, method: str, params: dict[str, Any] | None) -> Any:
        """Dispatch a JSON-RPC call with tenacity retry."""

        @retry(
            stop=stop_after_attempt(self._config.retry_max_attempts),
            wait=wait_exponential_jitter(
                initial=self._config.retry_initial_wait,
                max=self._config.retry_max_wait,
            ),
            retry=retry_if_exception_type((httpx.ConnectError, httpx.TimeoutException)),
            reraise=True,
        )
        async def _do_call() -> Any:
            url = urljoin(self._remote_url + "/", f"api/nfs/{method}")
            payload = {
                "jsonrpc": "2.0",
                "id": str(uuid.uuid4()),
                "method": method,
                "params": params or {},
            }
            headers = {"Content-Type": "application/json"}
            start = time.monotonic()
            try:
                resp = await self._client.post(url, json=payload, headers=headers)
                elapsed = time.monotonic() - start
                logger.debug(
                    "RPC %s completed in %.3fs (HTTP %d)", method, elapsed, resp.status_code
                )
                resp.raise_for_status()
                body = decode_rpc_message(resp.content)
                if "error" in body and body["error"]:
                    raise RemoteCallError(
                        method,
                        status_code=resp.status_code,
                        cause=RuntimeError(body["error"]),
                    )
                return body.get("result")
            except httpx.HTTPStatusError as exc:
                raise RemoteCallError(
                    method, status_code=exc.response.status_code, cause=exc
                ) from exc

        try:
            return await _do_call()
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            raise RemoteCallError(method, cause=exc) from exc

    @staticmethod
    def _auth_headers(api_key: str | None) -> dict[str, str]:
        headers: dict[str, str] = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        return headers


def _safe_json(params: dict[str, Any] | None) -> str:
    """Encode params to a JSON string safe for use in a header."""
    import json

    return json.dumps(params or {})
