"""Pinned-DNS httpx transport for SSRF hardening (Issue #3792).

Wraps ``httpx.AsyncHTTPTransport`` so that the TCP connect uses IPs that
were captured and validated by ``validate_outbound_url``. This closes
the DNS rebinding TOCTOU: the OS resolver is never consulted again after
validation, so an attacker cannot flip DNS between the validator call
and the actual connect.

TLS SNI and the outgoing ``Host`` header continue to use the original
hostname, so certificate verification works normally.

Implementation: a custom ``httpcore.AsyncNetworkBackend`` wraps the
default backend and rewrites the destination host to the next pinned IP
on every ``connect_tcp`` call. The TLS handshake (performed by httpcore
upstream of connect_tcp) still uses the request URL's hostname for SNI.
"""

from __future__ import annotations

import itertools
import logging
from collections.abc import Callable
from typing import Any

import httpcore
import httpx
from httpx import AsyncHTTPTransport

from nexus.lib.security.url_validator import ValidatedURL

logger = logging.getLogger(__name__)


class _PinnedBackend(httpcore.AsyncNetworkBackend):
    """Rewrites TCP connect host to the next pinned IP; round-robins on failure."""

    def __init__(
        self,
        inner: httpcore.AsyncNetworkBackend,
        pinned_ips: list[str],
        server_hostname: str,
    ) -> None:
        if not pinned_ips:
            raise ValueError("pinned_ips must be non-empty")
        self._inner = inner
        self._pinned_ips = pinned_ips
        self._server_hostname = server_hostname
        self._cycle = itertools.cycle(pinned_ips)

    async def connect_tcp(
        self,
        host: str,  # noqa: ARG002 - caller-supplied host is intentionally ignored
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Any = None,
    ) -> httpcore.AsyncNetworkStream:
        # Ignore the caller-supplied host - we pin to the validated IPs.
        # httpx/httpcore performs TLS SNI later using the URL host it
        # tracks separately, so cert verification still works against
        # the original hostname.
        last_exc: BaseException | None = None
        for _ in range(len(self._pinned_ips)):
            pinned = next(self._cycle)
            try:
                return await self._inner.connect_tcp(
                    host=pinned,
                    port=port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except (httpcore.ConnectError, OSError) as exc:
                last_exc = exc
                continue
        assert last_exc is not None
        raise httpcore.ConnectError(str(last_exc)) from last_exc

    async def connect_unix_socket(self, *args: Any, **kwargs: Any) -> Any:
        # Pass through unchanged - unix sockets are not SSRF-relevant.
        return await self._inner.connect_unix_socket(*args, **kwargs)

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


class PinnedResolverTransport(AsyncHTTPTransport):
    """httpx transport that pins TCP connects to pre-validated IPs.

    Attributes:
        pinned_ips: Copy of ``validated.resolved_ips`` that TCP connects
            are restricted to.
        server_hostname: The original hostname from ``validated`` - used
            for logging and exposed for tests.
        network_backend: The underlying default ``httpcore`` network
            backend that actually opens sockets. Exposed so tests can
            patch ``connect_tcp`` to observe the pinned IP that our
            wrapper forwards into it.
    """

    def __init__(self, validated: ValidatedURL, **kwargs: Any) -> None:
        if not validated.resolved_ips:
            raise ValueError("PinnedResolverTransport requires at least one pinned IP")
        super().__init__(**kwargs)
        self.pinned_ips: list[str] = list(validated.resolved_ips)
        self.server_hostname: str = validated.hostname

        # httpx's AsyncHTTPTransport constructs an internal httpcore pool.
        # Replace its network backend with our pinned wrapper that
        # forwards into the original default backend.
        pool = self._pool
        default_backend = pool._network_backend
        # Expose the *inner* default backend so tests can patch
        # ``connect_tcp`` and observe the pinned IP our wrapper passes in.
        self.network_backend: httpcore.AsyncNetworkBackend = default_backend
        self._pinned_backend = _PinnedBackend(
            inner=default_backend,
            pinned_ips=self.pinned_ips,
            server_hostname=self.server_hostname,
        )
        pool._network_backend = self._pinned_backend


PinnedClientFactory = Callable[
    [dict[str, str] | None, httpx.Timeout | None, httpx.Auth | None],
    httpx.AsyncClient,
]


def make_pinned_client_factory(validated: ValidatedURL) -> PinnedClientFactory:
    """Return a callable compatible with ``mcp.shared._httpx_utils.McpHttpClientFactory``.

    The MCP SSE/streamable-HTTP clients accept an
    ``httpx_client_factory(headers, timeout, auth) -> httpx.AsyncClient``
    parameter. We return a factory that builds an AsyncClient wired to
    ``PinnedResolverTransport`` with redirects disabled.
    """

    def factory(
        headers: dict[str, str] | None = None,
        timeout: httpx.Timeout | None = None,
        auth: httpx.Auth | None = None,
    ) -> httpx.AsyncClient:
        transport = PinnedResolverTransport(validated)
        return httpx.AsyncClient(
            transport=transport,
            headers=headers,
            timeout=timeout if timeout is not None else httpx.Timeout(30.0),
            auth=auth,
            follow_redirects=False,
        )

    return factory
