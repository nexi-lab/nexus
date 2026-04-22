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
import time
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

import httpcore
import httpx
from httpx import AsyncHTTPTransport

from nexus.lib.events import emit_audit_event
from nexus.lib.security.url_validator import ValidatedURL

logger = logging.getLogger(__name__)

# Wall-clock budget for the full retry sweep across pinned IPs. The
# validator's DNS answer is consumed in full (no silent truncation), so
# dual-stack and multi-A records are still attempted; we just stop the
# sweep once the budget is exhausted to bound client-visible latency
# when many addresses are unroutable.
_DEFAULT_TOTAL_CONNECT_BUDGET_SECS = 15.0


def _proxy_for_url(url: str) -> str | None:
    """Return the proxy URL applicable to ``url``, else None.

    Delegates to ``urllib.request.getproxies()`` and ``proxy_bypass()``,
    which implement the standard ``HTTP_PROXY`` / ``HTTPS_PROXY`` /
    ``ALL_PROXY`` / ``NO_PROXY`` semantics (including IPv6-bracketed
    hosts and wildcard bypass entries).
    """
    proxies = urllib.request.getproxies()
    parsed = urllib.parse.urlsplit(url)
    scheme = parsed.scheme.lower()
    proxy = proxies.get(scheme) or proxies.get("all")
    if not proxy:
        return None
    host = parsed.hostname or ""
    try:
        if host and urllib.request.proxy_bypass(host):
            return None
    except (ValueError, TypeError):
        pass
    return proxy


class _PinnedBackend(httpcore.AsyncNetworkBackend):
    """Rewrites TCP connect host to the next pinned IP; round-robins on failure."""

    def __init__(
        self,
        inner: httpcore.AsyncNetworkBackend,
        pinned_ips: list[str],
        total_budget_secs: float = _DEFAULT_TOTAL_CONNECT_BUDGET_SECS,
    ) -> None:
        if not pinned_ips:
            raise ValueError("pinned_ips must be non-empty")
        # Deduplicate while preserving order. No truncation: the full
        # validated DNS answer is attempted in order, bounded by an
        # overall wall-clock budget instead of a fixed count.
        seen: set[str] = set()
        deduped: list[str] = []
        for ip in pinned_ips:
            if ip not in seen:
                seen.add(ip)
                deduped.append(ip)
        self._inner = inner
        self._pinned_ips = deduped
        self._cycle = itertools.cycle(self._pinned_ips)
        self._total_budget_secs = total_budget_secs

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
        total = len(self._pinned_ips)
        # Overall retry budget = min(configured wall-clock, caller connect
        # timeout). Without the caller bound, a connect_timeout of 1s
        # could still balloon to N*1s across N IPs; with it, total
        # elapsed retry time stays within the caller's connect timeout.
        overall_budget = self._total_budget_secs
        if timeout is not None:
            overall_budget = min(overall_budget, timeout)
        deadline = time.monotonic() + overall_budget
        # Distribute remaining wall-clock budget across remaining attempts
        # so a single slow/blackholed IP cannot consume the full budget
        # and leave later validated IPs untried.
        for attempt_idx in range(total):
            remaining_ips = total - attempt_idx
            remaining_budget = deadline - time.monotonic()
            if remaining_budget <= 0:
                break
            fair_share = remaining_budget / remaining_ips
            attempt_timeout = fair_share if timeout is None else min(timeout, fair_share)
            pinned = next(self._cycle)
            try:
                return await self._inner.connect_tcp(
                    host=pinned,
                    port=port,
                    timeout=attempt_timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except (httpcore.ConnectError, httpcore.TimeoutException, OSError) as exc:
                last_exc = exc
                continue
        if last_exc is None:
            raise httpcore.ConnectError(
                f"pinned connect budget exhausted before attempting any IP (budget={self._total_budget_secs}s)"
            )
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
        pinned_backend: The wrapping backend that rewrites TCP connect
            destinations to validated IPs; installed on the internal
            httpx pool in place of the default backend.
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
        # Private test seam: the inner default backend that our wrapper
        # forwards into. Tests patch its ``connect_tcp`` to observe the
        # pinned IP the wrapper passes in.
        self._inner_backend: httpcore.AsyncNetworkBackend = default_backend
        self.pinned_backend = _PinnedBackend(
            inner=default_backend,
            pinned_ips=self.pinned_ips,
        )
        pool._network_backend = self.pinned_backend


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

    When the deployment routes egress through an HTTP(S) proxy (standard
    ``HTTPS_PROXY`` / ``HTTP_PROXY`` envvars), the pinned transport is
    skipped: the TCP connect goes to the proxy (not the origin), so DNS
    rebinding against the origin hostname is not reachable here. The URL
    is still validated upfront, and redirects are still disabled.
    """
    proxy = _proxy_for_url(validated.url)
    if proxy is not None:
        # Proxy-mediated egress: TCP connect goes to the proxy, not the
        # origin, so our client-side IP pinning does not apply. The proxy
        # is responsible for enforcing its own egress policy (including
        # DNS rebinding protection, if any). URL validation + policy
        # decisions already happened upfront; we surface this at WARNING
        # *and* as a structured audit event so operators/detectors can
        # observe when pinning is not being enforced.
        logger.warning(
            "SSRF pinning skipped for %s: HTTP(S)_PROXY in effect; "
            "DNS rebinding protection at TCP connect is delegated to the proxy.",
            validated.hostname,
        )
        emit_audit_event(
            "security.ssrf_pinning_skipped",
            {
                "reason": "proxy_env_active",
                "hostname": validated.hostname,
                "url": validated.url,
            },
        )

    def factory(
        headers: dict[str, str] | None = None,
        timeout: httpx.Timeout | None = None,
        auth: httpx.Auth | None = None,
    ) -> httpx.AsyncClient:
        client_timeout = timeout if timeout is not None else httpx.Timeout(30.0)
        if proxy is not None:
            return httpx.AsyncClient(
                headers=headers,
                timeout=client_timeout,
                auth=auth,
                follow_redirects=False,
                proxy=proxy,
            )
        transport = PinnedResolverTransport(validated)
        return httpx.AsyncClient(
            transport=transport,
            headers=headers,
            timeout=client_timeout,
            auth=auth,
            follow_redirects=False,
        )

    return factory
