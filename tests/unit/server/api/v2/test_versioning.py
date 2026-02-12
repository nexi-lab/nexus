"""Tests for API v2 versioning infrastructure.

Issue #995: API versioning strategy for breaking changes.

Covers:
- RouterEntry / RouterRegistry data structures
- VersionHeaderMiddleware (X-API-Version on /api/v2/ responses)
- DeprecationMiddleware (RFC 9745 Deprecation + RFC 8594 Sunset headers)
- register_v2_routers integration
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from nexus.server.api.v2.versioning import (
    API_VERSION,
    DeprecationMiddleware,
    RouterEntry,
    RouterRegistry,
    VersionHeaderMiddleware,
    register_v2_routers,
)

# =============================================================================
# Helpers
# =============================================================================


def _make_router(prefix: str, tag: str = "test") -> APIRouter:
    """Create a minimal router with a single GET endpoint."""
    r = APIRouter(prefix=prefix, tags=[tag])

    @r.get("/ping")
    async def ping() -> dict[str, str]:
        return {"pong": tag}

    return r


def _make_app_with_version_header(*routers: APIRouter) -> FastAPI:
    """Build a FastAPI app with VersionHeaderMiddleware."""
    app = FastAPI()
    app.add_middleware(VersionHeaderMiddleware)
    for r in routers:
        app.include_router(r)
    return app


# =============================================================================
# RouterEntry / RouterRegistry
# =============================================================================


class TestRouterEntry:
    def test_frozen_dataclass(self) -> None:
        r = _make_router("/api/v2/test")
        entry = RouterEntry(router=r, name="test", endpoint_count=1)
        assert entry.name == "test"
        assert entry.endpoint_count == 1
        assert entry.deprecated is None
        assert entry.sunset is None
        assert entry.prefix is None

    def test_with_deprecation(self) -> None:
        r = _make_router("/api/v2/old")
        entry = RouterEntry(
            router=r,
            name="old",
            deprecated="2026-06-01T00:00:00",
            sunset="2027-01-01T00:00:00",
        )
        assert entry.deprecated == "2026-06-01T00:00:00"
        assert entry.sunset == "2027-01-01T00:00:00"


class TestRouterRegistry:
    def test_empty_registry(self) -> None:
        reg = RouterRegistry()
        assert reg.entries == ()
        assert reg.total_endpoints() == 0

    def test_add_entries(self) -> None:
        reg = RouterRegistry()
        r1 = _make_router("/api/v2/a")
        r2 = _make_router("/api/v2/b")
        reg.add(RouterEntry(router=r1, name="a", endpoint_count=3))
        reg.add(RouterEntry(router=r2, name="b", endpoint_count=5))
        assert len(reg.entries) == 2
        assert reg.total_endpoints() == 8

    def test_entries_returns_tuple(self) -> None:
        """Entries should be an immutable snapshot."""
        reg = RouterRegistry()
        entries = reg.entries
        assert isinstance(entries, tuple)


# =============================================================================
# VersionHeaderMiddleware
# =============================================================================


class TestVersionHeaderMiddleware:
    def test_v2_path_gets_header(self) -> None:
        router = _make_router("/api/v2/test")
        app = _make_app_with_version_header(router)
        client = TestClient(app)

        resp = client.get("/api/v2/test/ping")
        assert resp.status_code == 200
        assert resp.headers["X-API-Version"] == API_VERSION

    def test_non_v2_path_no_header(self) -> None:
        router = APIRouter(prefix="/health", tags=["health"])

        @router.get("/check")
        async def check() -> dict[str, str]:
            return {"ok": "true"}

        app = _make_app_with_version_header(router)
        client = TestClient(app)

        resp = client.get("/health/check")
        assert resp.status_code == 200
        assert "X-API-Version" not in resp.headers

    def test_api_version_value(self) -> None:
        """API_VERSION should be a string like '2.0'."""
        assert isinstance(API_VERSION, str)
        parts = API_VERSION.split(".")
        assert len(parts) == 2
        assert all(p.isdigit() for p in parts)


# =============================================================================
# DeprecationMiddleware
# =============================================================================


class TestDeprecationMiddleware:
    def test_deprecated_endpoint_gets_header(self) -> None:
        router = _make_router("/api/v2/old")
        dep_date = "2026-06-01T00:00:00"
        registry = RouterRegistry()
        registry.add(RouterEntry(router=router, name="old", deprecated=dep_date, endpoint_count=1))

        app = FastAPI()
        app.include_router(router)
        app.add_middleware(DeprecationMiddleware, registry=registry)

        client = TestClient(app)
        resp = client.get("/api/v2/old/ping")
        assert resp.status_code == 200

        expected_ts = int(datetime.fromisoformat(dep_date).timestamp())
        assert resp.headers["Deprecation"] == f"@{expected_ts}"

    def test_sunset_header(self) -> None:
        router = _make_router("/api/v2/legacy")
        sunset_date = "2027-01-01T00:00:00"
        registry = RouterRegistry()
        registry.add(
            RouterEntry(
                router=router,
                name="legacy",
                deprecated="2026-06-01T00:00:00",
                sunset=sunset_date,
                endpoint_count=1,
            )
        )

        app = FastAPI()
        app.include_router(router)
        app.add_middleware(DeprecationMiddleware, registry=registry)

        client = TestClient(app)
        resp = client.get("/api/v2/legacy/ping")
        assert resp.status_code == 200

        dt = datetime.fromisoformat(sunset_date)
        expected_sunset = dt.strftime("%a, %d %b %Y %H:%M:%S GMT")
        assert resp.headers["Sunset"] == expected_sunset

    def test_non_deprecated_endpoint_no_headers(self) -> None:
        router = _make_router("/api/v2/active")
        registry = RouterRegistry()
        registry.add(RouterEntry(router=router, name="active", endpoint_count=1))

        app = FastAPI()
        app.include_router(router)
        app.add_middleware(DeprecationMiddleware, registry=registry)

        client = TestClient(app)
        resp = client.get("/api/v2/active/ping")
        assert resp.status_code == 200
        assert "Deprecation" not in resp.headers
        assert "Sunset" not in resp.headers

    def test_prefix_override_matching(self) -> None:
        """Router with external prefix (like async_files)."""
        router = APIRouter(tags=["files"])

        @router.get("/ping")
        async def ping() -> dict[str, str]:
            return {"pong": "files"}

        registry = RouterRegistry()
        registry.add(
            RouterEntry(
                router=router,
                name="files",
                prefix="/api/v2/files",
                deprecated="2026-12-01T00:00:00",
                endpoint_count=1,
            )
        )

        app = FastAPI()
        app.include_router(router, prefix="/api/v2/files")
        app.add_middleware(DeprecationMiddleware, registry=registry)

        client = TestClient(app)
        resp = client.get("/api/v2/files/ping")
        assert resp.status_code == 200
        assert "Deprecation" in resp.headers


# =============================================================================
# register_v2_routers
# =============================================================================


class TestRegisterV2Routers:
    def test_registers_routers_on_app(self) -> None:
        router = _make_router("/api/v2/test")
        registry = RouterRegistry()
        registry.add(RouterEntry(router=router, name="test", endpoint_count=1))

        app = FastAPI()
        register_v2_routers(app, registry)

        client = TestClient(app)
        resp = client.get("/api/v2/test/ping")
        assert resp.status_code == 200
        assert resp.json() == {"pong": "test"}

    def test_applies_prefix_override(self) -> None:
        router = APIRouter(tags=["ext"])

        @router.get("/ping")
        async def ping() -> dict[str, str]:
            return {"pong": "ext"}

        registry = RouterRegistry()
        registry.add(
            RouterEntry(router=router, name="ext", prefix="/api/v2/external", endpoint_count=1)
        )

        app = FastAPI()
        register_v2_routers(app, registry)

        client = TestClient(app)
        resp = client.get("/api/v2/external/ping")
        assert resp.status_code == 200

    def test_empty_registry(self) -> None:
        """No routers registered — app should still work."""
        registry = RouterRegistry()
        app = FastAPI()
        register_v2_routers(app, registry)

        client = TestClient(app)
        resp = client.get("/nonexistent")
        assert resp.status_code == 404
