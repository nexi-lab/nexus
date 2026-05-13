from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from fastapi import FastAPI
from fastapi.testclient import TestClient

from nexus.server.api.v2.routers.search import router
from nexus.server.dependencies import require_auth

T = TypeVar("T")


class RecordingRunner:
    async def call(self, work: Callable[[], Awaitable[T]]) -> T:
        return await work()


class RecordingRegistry:
    def __init__(self) -> None:
        self.zones: list[str] = []

    def runner_for(self, zone_id: str) -> RecordingRunner:
        self.zones.append(zone_id)
        return RecordingRunner()


class FakeSearchDaemon:
    is_initialized = True

    async def search(self, **kwargs: Any) -> list[Any]:
        return []

    def get_health(self) -> dict[str, Any]:
        return {"status": "healthy"}

    def get_stats(self) -> dict[str, Any]:
        return {}


def test_search_query_runs_in_auth_zone_runner() -> None:
    registry = RecordingRegistry()
    app = FastAPI()
    app.state.search_daemon = FakeSearchDaemon()
    app.state.record_store = object()
    app.state.async_read_session_factory = object()
    app.state.permission_enforcer = None
    app.state.zone_registry = registry
    app.dependency_overrides[require_auth] = lambda: {
        "authenticated": True,
        "subject_type": "user",
        "subject_id": "alice",
        "zone_id": "eng",
        "zone_perms": [["eng", "r"]],
        "is_admin": False,
    }
    app.include_router(router)

    with TestClient(app) as client:
        response = client.get("/api/v2/search/query", params={"q": "hello"})

    assert response.status_code in (200, 503)
    assert registry.zones == ["eng"]
