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

    # ---- MUST_STAY HTTP routers only ----
    # All query/CRUD endpoints migrated to @rpc_expose services.
    # Remaining: streaming (SSE, CSV), K8s probes, tus.io, OAuth, async files.

    # ---- Audit streaming export ----
    try:
        from nexus.server.api.v2.routers.audit import router as audit_router

        registry.add(RouterEntry(router=audit_router, name="audit", endpoint_count=1))
    except ImportError as e:
        logger.warning("Failed to import Audit routes: %s", e)

    # ---- Events SSE stream + watch ----
    try:
        from nexus.server.api.v2.routers.events_replay import (
            router as events_replay_router,
        )
        from nexus.server.api.v2.routers.events_replay import (
            watch_router,
        )

        registry.add(
            RouterEntry(router=events_replay_router, name="events_replay", endpoint_count=1)
        )
        registry.add(RouterEntry(router=watch_router, name="watch", endpoint_count=1))
    except ImportError as e:
        logger.warning("Failed to import Events replay routes: %s", e)

    # ---- Async files (StreamingResponse) ----
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

    # ---- tus.io resumable uploads (Issue #788) ----
    if chunked_upload_service_getter is not None:
        try:
            from nexus.server.api.v2.routers.tus_uploads import create_tus_uploads_router

            tus_public_router, tus_auth_router = create_tus_uploads_router(
                get_upload_service=chunked_upload_service_getter,
            )
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

    # ---- Bricks health (K8s probe — public) ----
    try:
        from nexus.server.api.v2.routers.bricks import health_router as bricks_health_router

        registry.add(
            RouterEntry(router=bricks_health_router, name="bricks_health", endpoint_count=1)
        )
    except ImportError as e:
        logger.warning("Failed to import Bricks health routes: %s", e)

    # ---- Task Manager SSE events ----
    try:
        from nexus.server.api.v2.routers.task_manager import router as task_manager_router

        registry.add(RouterEntry(router=task_manager_router, name="task_manager", endpoint_count=1))
    except ImportError as e:
        logger.warning("Failed to import Task manager routes: %s", e)

    # ---- IPC SSE stream ----
    try:
        from nexus.server.api.v2.routers.ipc import router as ipc_router

        registry.add(RouterEntry(router=ipc_router, name="ipc", endpoint_count=1))
    except ImportError as e:
        logger.warning("Failed to import IPC routes: %s", e)

    # ---- Secrets audit streaming export ----
    try:
        from nexus.server.api.v2.routers.secrets_audit import router as secrets_audit_router

        registry.add(
            RouterEntry(router=secrets_audit_router, name="secrets_audit", endpoint_count=1)
        )
    except ImportError as e:
        logger.warning("Failed to import Secrets audit routes: %s", e)

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
