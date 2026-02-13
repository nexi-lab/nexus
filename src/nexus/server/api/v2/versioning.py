"""API versioning infrastructure (#995).

Provides:
- RouterEntry: metadata wrapper for versioned routers
- V2_ROUTERS: registry of all v2 router entries
- register_v2_routers(): one-call registration on a FastAPI app
- VersionHeaderMiddleware: adds X-API-Version to every v2 response
- DeprecationMiddleware: adds Deprecation + Sunset headers (RFC 9745 / RFC 8594)

Issue #995: API versioning strategy for breaking changes.
"""

from __future__ import annotations

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

    router: APIRouter
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
    async_nexus_fs_getter: object | None = None,
    chunked_upload_service_getter: object | None = None,
) -> RouterRegistry:
    """Import all v2 routers and return a populated registry.

    Each import is isolated in its own try/except so one broken
    module doesn't prevent the rest from loading.

    Args:
        async_nexus_fs_getter: Optional callable returning the async FS
            instance (used by the async_files factory router).
    """
    registry = RouterRegistry()

    # ---- ACE core routers ----
    try:
        from nexus.server.api.v2.routers import (
            audit,
            conflicts,
            consolidation,
            curate,
            feedback,
            memories,
            mobile_search,
            operations,
            playbooks,
            reflect,
            trajectories,
        )

        _ace_routers: list[RouterEntry] = [
            RouterEntry(router=memories.router, name="memories", endpoint_count=14),
            RouterEntry(router=trajectories.router, name="trajectories", endpoint_count=5),
            RouterEntry(router=feedback.router, name="feedback", endpoint_count=5),
            RouterEntry(router=playbooks.router, name="playbooks", endpoint_count=6),
            RouterEntry(router=reflect.router, name="reflect", endpoint_count=1),
            RouterEntry(router=curate.router, name="curate", endpoint_count=2),
            RouterEntry(router=consolidation.router, name="consolidation", endpoint_count=4),
            RouterEntry(router=mobile_search.router, name="mobile_search", endpoint_count=2),
            RouterEntry(router=conflicts.router, name="conflicts", endpoint_count=3),
            RouterEntry(router=operations.router, name="operations", endpoint_count=2),
            RouterEntry(router=audit.router, name="audit", endpoint_count=5),
        ]
        for entry in _ace_routers:
            registry.add(entry)
    except ImportError as e:
        logger.warning("Failed to import ACE v2 routes: %s", e)

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

        async_files_router = create_async_files_router(get_fs=async_nexus_fs_getter)
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

            tus_router = create_tus_uploads_router(
                get_upload_service=chunked_upload_service_getter,
            )
            registry.add(
                RouterEntry(
                    router=tus_router,
                    name="tus_uploads",
                    prefix="/api/v2/uploads",
                    endpoint_count=5,
                )
            )
        except ImportError as e:
            logger.warning("Failed to import tus uploads router: %s", e)

    return registry


def register_v2_routers(
    app: FastAPI,
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
