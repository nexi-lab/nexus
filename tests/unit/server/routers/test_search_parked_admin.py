"""REST tests for the parked-event admin endpoints (#4337)."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from nexus.server.api.v2.routers.search import router
from nexus.server.dependencies import require_admin


class FakeDaemon:
    def __init__(self) -> None:
        self.skip_calls: list[tuple[str, int]] = []

    def list_parked(self) -> dict[str, list[dict[str, Any]]]:
        return {"bm25": [{"event_id": "search:op-1", "kind": "permanent"}]}

    async def retry_parked(self, consumer: str, event_ids: list[str] | None) -> dict[str, Any]:
        if consumer == "nope":
            raise ValueError("unknown consumer 'nope'")
        return {"retried": 1, "succeeded": ["search:op-1"], "failed": []}

    async def discard_parked(self, consumer: str, event_ids: list[str]) -> dict[str, Any]:
        if consumer == "nope":
            raise ValueError("unknown consumer 'nope'")
        return {"discarded": event_ids}

    async def force_checkpoint(self, consumer: str, sequence: int) -> dict[str, int]:
        if sequence <= 100:
            raise ValueError("sequence 100 must be greater than current checkpoint 100")
        self.skip_calls.append((consumer, sequence))
        return {"previous": 100, "current": sequence}


def _client(daemon: FakeDaemon | None = None, *, admin: bool = True) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    app.state.search_daemon = daemon or FakeDaemon()
    if admin:
        app.dependency_overrides[require_admin] = lambda: {"is_admin": True}
    return TestClient(app, raise_server_exceptions=False)


def test_parked_list_requires_admin() -> None:
    client = _client(admin=False)
    response = client.get("/api/v2/search/parked")
    assert response.status_code in (401, 403)


def test_parked_list_returns_entries() -> None:
    client = _client()
    response = client.get("/api/v2/search/parked")
    assert response.status_code == 200
    assert response.json()["parked"]["bm25"][0]["event_id"] == "search:op-1"


def test_parked_retry_happy_path() -> None:
    client = _client()
    response = client.post(
        "/api/v2/search/parked/retry",
        json={"consumer": "bm25", "event_ids": None},
    )
    assert response.status_code == 200
    assert response.json()["succeeded"] == ["search:op-1"]


def test_parked_retry_unknown_consumer_is_400() -> None:
    client = _client()
    response = client.post("/api/v2/search/parked/retry", json={"consumer": "nope"})
    assert response.status_code == 400


def test_parked_discard() -> None:
    client = _client()
    response = client.post(
        "/api/v2/search/parked/discard",
        json={"consumer": "bm25", "event_ids": ["search:op-1"]},
    )
    assert response.status_code == 200
    assert response.json()["discarded"] == ["search:op-1"]


def test_skip_to_happy_path_and_validation() -> None:
    daemon = FakeDaemon()
    client = _client(daemon)
    response = client.post("/api/v2/search/consumers/bm25/skip-to", json={"sequence": 250})
    assert response.status_code == 200
    assert response.json() == {"previous": 100, "current": 250}
    assert daemon.skip_calls == [("bm25", 250)]

    response = client.post("/api/v2/search/consumers/bm25/skip-to", json={"sequence": 100})
    assert response.status_code == 400


def test_parked_discard_unknown_consumer_is_400() -> None:
    client = _client()
    response = client.post(
        "/api/v2/search/parked/discard",
        json={"consumer": "nope", "event_ids": ["x"]},
    )
    assert response.status_code == 400
