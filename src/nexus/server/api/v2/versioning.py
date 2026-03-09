"""API versioning infrastructure (#995).

Provides:
- RouterEntry: metadata wrapper for versioned routers
- V2_ROUTERS: registry of all v2 router entries
- register_v2_routers(): one-call registration on a FastAPI app
- VersionHeaderMiddleware: adds X-API-Version to every v2 response
- DeprecationMiddleware: adds Deprecation + Sunset headers (RFC 9745 / RFC 8594)

Issue #995: API versioning strategy for breaking changes.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING

from starlette.types import ASGIApp, Message, Receive, Scope, Send

if TYPE_CHECKING:
    from fastapi import APIRouter, FastAPI

logger = logging.getLogger(__name__)

# Current API version — bump on breaking changes.
API_VERSION = "2.0"

# ---------------------------------------------------------------------------
# Router registry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RouterEntry:
    """Metadata for a versioned API router.

    Attributes:
        router: The FastAPI APIRouter instance.
        name: Human-readable name (e.g. "memories").
        prefix: Optional prefix override passed to ``include_router``.
            Most routers embed their prefix internally; use this only
            when the prefix is set externally (e.g. async_files).
        deprecated: ISO-8601 date when the endpoint group was deprecated
            (``None`` if not deprecated).  Drives the Deprecation header.
        sunset: ISO-8601 date when the endpoint group will be removed
            (``None`` if no removal planned).  Drives the Sunset header.
        endpoint_count: Informational count for logging.
    """

    router: "APIRouter"
    name: str
    prefix: str | None = None
    deprecated: str | None = None
    sunset: str | None = None
    endpoint_count: int = 0


@dataclass
class RouterRegistry:
    """Ordered collection of RouterEntry objects."""

    _entries: list[RouterEntry] = field(default_factory=list)

    def add(self, entry: RouterEntry) -> None:
        self._entries.append(entry)

    @property
    def entries(self) -> tuple[RouterEntry, ...]:
        return tuple(self._entries)

    def total_endpoints(self) -> int:
        return sum(e.endpoint_count for e in self._entries)


def build_v2_registry(
    *,
    nexus_fs_getter: object | None = None,
    chunked_upload_service_getter: object | None = None,
) -> RouterRegistry:
    """Import all v2 routers and return a populated registry.

    Each import is isolated in its own try/except so one broken
    module doesn't prevent the rest from loading.

    Args:
        nexus_fs_getter: Optional callable returning the NexusFS
            instance (used by the async_files factory router).
    """
    registry = RouterRegistry()

    # ---- Core v2 routers ----
    try:
        from nexus.server.api.v2.routers import (
            audit,
            conflicts,
            memories,
            mobile_search,
            operations,
            sync_push,
        )

        _core_routers: list[RouterEntry] = [
            RouterEntry(router=memories.router, name="memories", endpoint_count=14),
            RouterEntry(router=mobile_search.router, name="mobile_search", endpoint_count=2),
            RouterEntry(router=conflicts.router, name="conflicts", endpoint_count=3),
            RouterEntry(router=operations.router, name="operations", endpoint_count=2),
            RouterEntry(router=audit.router, name="audit", endpoint_count=5),
            RouterEntry(router=sync_push.router, name="sync_push", endpoint_count=1),
        ]
        for entry in _core_routers:
            registry.add(entry)
    except ImportError as e:
        logger.warning("Failed to import core v2 routes: %s", e)

    # ---- Events replay router (Issue #1139, #2056) ----
    try:
        from nexus.server.api.v2.routers.events_replay import (
            router as events_replay_router,
        )
        from nexus.server.api.v2.routers.events_replay import (
            watch_router,
        )

        registry.add(
            RouterEntry(router=events_replay_router, name="events_replay", endpoint_count=3)
        )
        registry.add(RouterEntry(router=watch_router, name="watch", endpoint_count=1))
    except ImportError as e:
        logger.warning("Failed to import Events replay routes: %s", e)

    # ---- Pay router ----
    try:
        from nexus.server.api.v2.routers.pay import router as pay_router

        registry.add(RouterEntry(router=pay_router, name="pay", endpoint_count=8))
    except ImportError as e:
        logger.warning("Failed to import Pay routes: %s", e)

    # ---- Scheduler router ----
    try:
        from nexus.server.api.v2.routers.scheduler import router as scheduler_router

        registry.add(RouterEntry(router=scheduler_router, name="scheduler", endpoint_count=3))
    except ImportError as e:
        logger.warning("Failed to import Scheduler routes: %s", e)

    # ---- Async files router (factory pattern) ----
    try:
        from nexus.server.api.v2.routers.async_files import create_async_files_router

        async_files_router = create_async_files_router(get_fs=nexus_fs_getter)
        registry.add(
            RouterEntry(
                router=async_files_router,
                name="async_files",
                prefix="/api/v2/files",
                endpoint_count=9,
            )
        )
    except ImportError as e:
        logger.warning("Failed to import async files router: %s", e)

    # ---- Reputation router (Issue #1356) ----
    try:
        from nexus.server.api.v2.routers.reputation import router as reputation_router

        registry.add(RouterEntry(router=reputation_router, name="reputation", endpoint_count=7))
    except ImportError as e:
        logger.warning("Failed to import Reputation routes: %s", e)

    # ---- tus.io resumable uploads router (Issue #788) ----
    if chunked_upload_service_getter is not None:
        try:
            from nexus.server.api.v2.routers.tus_uploads import create_tus_uploads_router

            tus_public_router, tus_auth_router = create_tus_uploads_router(
                get_upload_service=chunked_upload_service_getter,
            )
            # OPTIONS endpoint is public (CORS preflight); all others require auth
            registry.add(
                RouterEntry(
                    router=tus_public_router,
                    name="tus_uploads_public",
                    prefix="/api/v2/uploads",
                    endpoint_count=1,
                )
            )
            registry.add(
                RouterEntry(
                    router=tus_auth_router,
                    name="tus_uploads",
                    prefix="/api/v2/uploads",
                    endpoint_count=4,
                )
            )
        except ImportError as e:
            logger.warning("Failed to import tus uploads router: %s", e)

    # ---- Manifest router (Issue #1427) ----
    try:
        from nexus.server.api.v2.routers.manifest import router as manifest_router

        registry.add(RouterEntry(router=manifest_router, name="manifest", endpoint_count=3))
    except ImportError as e:
        logger.warning("Failed to import Manifest routes: %s", e)

    # ---- Delegation router (Issue #1271) ----
    try:
        from nexus.server.api.v2.routers.delegation import router as delegation_router

        registry.add(RouterEntry(router=delegation_router, name="delegation", endpoint_count=5))
    except ImportError as e:
        logger.warning("Failed to import Delegation routes: %s", e)

    # ---- RLM inference router (Issue #1306) ----
    try:
        from nexus.server.api.v2.routers.rlm import router as rlm_router

        registry.add(RouterEntry(router=rlm_router, name="rlm", endpoint_count=1))
    except ImportError as e:
        logger.warning("Failed to import RLM routes: %s", e)

    # ---- Workflows router (Issue #1522) ----
    try:
        from nexus.server.api.v2.routers.workflows import router as workflows_router

        registry.add(RouterEntry(router=workflows_router, name="workflows", endpoint_count=8))
    except ImportError as e:
        logger.warning("Failed to import Workflows routes: %s", e)

    # ---- Snapshots router (Issue #1752) ----
    try:
        from nexus.server.api.v2.routers.snapshots import router as snapshots_router

        registry.add(RouterEntry(router=snapshots_router, name="snapshots", endpoint_count=6))
    except ImportError as e:
        logger.warning("Failed to import Snapshots routes: %s", e)

    # ---- Bricks lifecycle router (Issue #1704) ----
    try:
        from nexus.server.api.v2.routers.bricks import health_router as bricks_health_router
        from nexus.server.api.v2.routers.bricks import router as bricks_router

        # Health endpoint is public (K8s probes); admin endpoints require auth
        registry.add(
            RouterEntry(router=bricks_health_router, name="bricks_health", endpoint_count=1)
        )
        registry.add(RouterEntry(router=bricks_router, name="bricks", endpoint_count=5))
    except ImportError as e:
        logger.warning("Failed to import Bricks routes: %s", e)

    # ---- Connector discovery router (Issue #2069) ----
    try:
        from nexus.server.api.v2.routers.connectors import router as connectors_router

        registry.add(RouterEntry(router=connectors_router, name="connectors", endpoint_count=2))
    except ImportError as e:
        logger.warning("Failed to import Connectors routes: %s", e)

    # ---- Batch operations router (Issue #1242) ----
    try:
        from nexus.server.api.v2.routers.batch import create_batch_router

        batch_router = create_batch_router(get_fs=nexus_fs_getter)
        registry.add(
            RouterEntry(
                router=batch_router,
                name="batch",
                prefix="/api/v2",
                endpoint_count=1,
            )
        )
    except ImportError as e:
        logger.warning("Failed to import Batch routes: %s", e)

    # ---- Auth keys router (key lifecycle management) ----
    try:
        from nexus.server.api.v2.routers.auth_keys import router as auth_keys_router

        registry.add(RouterEntry(router=auth_keys_router, name="auth_keys", endpoint_count=4))
    except ImportError as e:
        logger.warning("Failed to import Auth keys routes: %s", e)

    # ---- Eviction router (Issue #2170) ----
    try:
        from nexus.server.api.v2.routers.eviction import router as eviction_router

        registry.add(RouterEntry(router=eviction_router, name="eviction", endpoint_count=1))
    except ImportError as e:
        logger.warning("Failed to import Eviction routes: %s", e)

    # ---- Agent spec/status router (Issue #2169) ----
    try:
        from nexus.server.api.v2.routers.agent_status import router as agent_status_router

        registry.add(RouterEntry(router=agent_status_router, name="agent_status", endpoint_count=3))
    except ImportError as e:
        logger.warning("Failed to import Agent status routes: %s", e)

    # ---- Governance router (Issue #1359) — admin auth required ----
    try:
        from nexus.server.api.v2.routers.governance import router as governance_router

        registry.add(RouterEntry(router=governance_router, name="governance", endpoint_count=16))
    except ImportError as e:
        logger.warning("Failed to import Governance routes: %s", e)

    # ---- Locks router (Issue #2056 — ported from v1) ----
    try:
        from nexus.server.api.v2.routers.locks import router as locks_router

        registry.add(RouterEntry(router=locks_router, name="locks", endpoint_count=4))
    except ImportError as e:
        logger.warning("Failed to import Locks routes: %s", e)

    # ---- Subscriptions router (Issue #2056 — ported from v1) ----
    try:
        from nexus.server.api.v2.routers.subscriptions import router as subscriptions_router

        registry.add(
            RouterEntry(router=subscriptions_router, name="subscriptions", endpoint_count=4)
        )
    except ImportError as e:
        logger.warning("Failed to import Subscriptions routes: %s", e)

    # ---- Identity router (Issue #2056 — ported from v1) ----
    try:
        from nexus.server.api.v2.routers.identity import router as identity_router

        registry.add(RouterEntry(router=identity_router, name="identity", endpoint_count=2))
    except ImportError as e:
        logger.warning("Failed to import Identity routes: %s", e)

    # ---- Credentials router (Issue #1753 — Verifiable Credentials) ----
    try:
        from nexus.server.api.v2.routers.credentials import router as credentials_router

        registry.add(RouterEntry(router=credentials_router, name="credentials", endpoint_count=6))
    except ImportError as e:
        logger.warning("Failed to import Credentials routes: %s", e)

    # ---- Access Manifests router (Issue #1754) ----
    try:
        from nexus.server.api.v2.routers.access_manifests import (
            router as access_manifests_router,
        )

        registry.add(
            RouterEntry(router=access_manifests_router, name="access_manifests", endpoint_count=5)
        )
    except ImportError as e:
        logger.warning("Failed to import Access Manifests routes: %s", e)
    # ---- Search router (Issue #2056 — ported from v1) ----
    try:
        from nexus.server.api.v2.routers.search import router as search_router

        registry.add(RouterEntry(router=search_router, name="search", endpoint_count=5))
    except ImportError as e:
        logger.warning("Failed to import Search routes: %s", e)

    # ---- Graph router (Issue #2056 — ported from v1) ----
    try:
        from nexus.server.api.v2.routers.graph import router as graph_router

        registry.add(RouterEntry(router=graph_router, name="graph", endpoint_count=4))
    except ImportError as e:
        logger.warning("Failed to import Graph routes: %s", e)

    # ---- Cache router (Issue #2056 — ported from v1) ----
    try:
        from nexus.server.api.v2.routers.cache import router as cache_router

        registry.add(RouterEntry(router=cache_router, name="cache", endpoint_count=3))
    except ImportError as e:
        logger.warning("Failed to import Cache routes: %s", e)

    # ---- x402 protocol router (Issue #1206) ----
    try:
        from nexus.server.api.v2.routers.x402 import router as x402_router
        from nexus.server.api.v2.routers.x402 import webhook_router as x402_webhook_router

        # Webhook is public (called by external facilitator); topup/config require auth
        registry.add(RouterEntry(router=x402_webhook_router, name="x402_webhook", endpoint_count=1))
        registry.add(RouterEntry(router=x402_router, name="x402", endpoint_count=2))
    except ImportError as e:
        logger.warning("Failed to import x402 routes: %s", e)

    # ---- Federation router (A4: zone lifecycle + share/join) ----
    try:
        from nexus.server.api.v2.routers.federation import router as federation_router

        registry.add(RouterEntry(router=federation_router, name="federation", endpoint_count=8))
    except ImportError as e:
        logger.warning("Failed to import Federation routes: %s", e)

    return registry


def register_v2_routers(
    app: "FastAPI",
    registry: RouterRegistry,
) -> None:
    """Mount every router in *registry* onto *app*.

    Also registers per-router exception handlers where needed
    (e.g. Pay's custom exception handlers).
    """
    for entry in registry.entries:
        if entry.prefix is not None:
            app.include_router(entry.router, prefix=entry.prefix)
        else:
            app.include_router(entry.router)

    # Pay-specific exception handlers
    try:
        from nexus.server.api.v2.routers.pay import _register_pay_exception_handlers

        _register_pay_exception_handlers(app)
    except ImportError:
        pass  # Pay module not available — nothing to register.

    total = registry.total_endpoints()
    logger.info("API v2 routers registered (%d endpoints)", total)


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------


class VersionHeaderMiddleware:
    """Pure ASGI middleware — adds ``X-API-Version: 2.0`` to ``/api/v2/`` responses.

    Uses raw ASGI protocol instead of BaseHTTPMiddleware to avoid
    buffering the response body (critical for streaming endpoints
    like ``/api/v2/files/stream``).
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope["path"].startswith("/api/v2/"):
            await self.app(scope, receive, send)
            return

        async def send_with_version(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.append((b"x-api-version", API_VERSION.encode()))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_version)


class DeprecationMiddleware:
    """Pure ASGI middleware — adds RFC 9745 ``Deprecation`` and RFC 8594 ``Sunset`` headers.

    Pre-computes a prefix → headers lookup at init time so the per-request
    hot path is a single dict lookup + startswith check with zero allocations
    when no deprecation is active (the common case).

    Uses raw ASGI protocol to avoid buffering (streaming-safe).
    """

    def __init__(self, app: ASGIApp, *, registry: RouterRegistry) -> None:
        self.app = app
        # Pre-compute prefix → list of (header_name, header_value) pairs.
        self._prefix_headers: list[tuple[str, list[tuple[bytes, bytes]]]] = []
        for entry in registry.entries:
            prefix = entry.prefix
            if prefix is None:
                prefix = getattr(entry.router, "prefix", "") or ""
            if not prefix:
                continue

            extra: list[tuple[bytes, bytes]] = []
            if entry.deprecated:
                ts = int(datetime.fromisoformat(entry.deprecated).timestamp())
                extra.append((b"deprecation", f"@{ts}".encode()))
            if entry.sunset:
                dt = datetime.fromisoformat(entry.sunset)
                val = dt.strftime("%a, %d %b %Y %H:%M:%S GMT")
                extra.append((b"sunset", val.encode()))

            if extra:
                self._prefix_headers.append((prefix, extra))

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not self._prefix_headers:
            await self.app(scope, receive, send)
            return

        path = scope["path"]
        matched_headers: list[tuple[bytes, bytes]] | None = None
        for prefix, extra in self._prefix_headers:
            if path.startswith(prefix):
                matched_headers = extra
                break

        if matched_headers is None:
            await self.app(scope, receive, send)
            return

        async def send_with_deprecation(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.extend(matched_headers)
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_deprecation)
