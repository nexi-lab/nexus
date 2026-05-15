"""Tests for SANDBOX route-level allowlist (Issue #3778)."""

from fastapi import FastAPI
from starlette.routing import Route


class TestSandboxRouteFilter:
    def test_filter_retains_allowlisted_routes(self) -> None:
        from nexus.server.fastapi_server import _filter_routes_for_sandbox

        app = FastAPI()

        @app.get("/health")
        def _h() -> dict:
            return {"ok": True}

        @app.get("/api/v2/features")
        def _f() -> dict:
            return {}

        @app.get("/api/v2/skills/list")
        def _s() -> dict:
            return {}

        @app.get("/api/v2/pay/charge")
        def _p() -> dict:
            return {}

        _filter_routes_for_sandbox(app)

        paths = {r.path for r in app.router.routes if isinstance(r, Route)}
        assert "/health" in paths
        assert "/api/v2/features" in paths
        assert "/api/v2/skills/list" not in paths
        assert "/api/v2/pay/charge" not in paths

    def test_filter_preserves_openapi_docs(self) -> None:
        from nexus.server.fastapi_server import _filter_routes_for_sandbox

        app = FastAPI()
        _filter_routes_for_sandbox(app)
        paths = {r.path for r in app.router.routes if isinstance(r, Route)}
        assert "/openapi.json" in paths

    def test_filter_idempotent(self) -> None:
        from nexus.server.fastapi_server import _filter_routes_for_sandbox

        app = FastAPI()

        @app.get("/health")
        def _h() -> dict:
            return {}

        _filter_routes_for_sandbox(app)
        before = len(app.router.routes)
        _filter_routes_for_sandbox(app)
        assert len(app.router.routes) == before

    def test_filter_removes_nonallowlisted_route(self) -> None:
        from nexus.server.fastapi_server import _filter_routes_for_sandbox

        app = FastAPI()

        @app.get("/random")
        def _r() -> dict:
            return {}

        @app.get("/health")
        def _h() -> dict:
            return {}

        _filter_routes_for_sandbox(app)
        paths = {r.path for r in app.router.routes if isinstance(r, Route)}
        assert "/random" not in paths
        assert "/health" in paths
