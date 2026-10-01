"""``_resolve_route_template`` memoises matched templates, never misses."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI

from nexus.server import metrics


def _scope(app: FastAPI, path: str, method: str = "GET") -> dict[str, Any]:
    return {"type": "http", "app": app, "path": path, "method": method, "root_path": ""}


def test_matched_template_is_memoised(monkeypatch: Any) -> None:
    app = FastAPI()

    @app.get("/items/{item_id}")
    def _item(item_id: str) -> str:
        return item_id

    monkeypatch.setattr(metrics, "_TEMPLATE_CACHE", {})
    assert metrics._resolve_route_template(_scope(app, "/items/42")) == "/items/{item_id}"

    calls = 0
    original = type(app.routes[-1]).matches

    def counting(self: Any, scope: Any) -> Any:
        nonlocal calls
        calls += 1
        return original(self, scope)

    monkeypatch.setattr(type(app.routes[-1]), "matches", counting)
    assert metrics._resolve_route_template(_scope(app, "/items/42")) == "/items/{item_id}"
    assert calls == 0, "second resolution of the same path must not re-walk the routes"


def test_unmatched_path_falls_back_and_is_not_cached(monkeypatch: Any) -> None:
    app = FastAPI()
    monkeypatch.setattr(metrics, "_TEMPLATE_CACHE", {})
    assert metrics._resolve_route_template(_scope(app, "/nope")) == "/nope"
    assert metrics._TEMPLATE_CACHE == {}

    @app.get("/nope")
    def _late() -> str:
        return "ok"

    # A route registered after a miss is still found.
    assert metrics._resolve_route_template(_scope(app, "/nope")) == "/nope"
    assert len(metrics._TEMPLATE_CACHE) == 1
