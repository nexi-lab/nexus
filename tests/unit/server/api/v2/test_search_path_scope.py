"""Multi-prefix search scope: several path prefixes, ONE fused ranking.

Callers that need several subtrees (Koodle: ``documents/`` + ``notes/`` +
``private-inbox/``, excluding ``brief/*``) used to issue one query per prefix
and merge the lists by score — but fused scores are normalised per result
list, so the tops of tiny lists outranked the right document. A repeated
``path`` (GET) or a ``path`` list (batch) now reaches the plugin as
``QueryRequest.path_filters`` for one fused ranking over the union.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from nexus.contracts.search_types import SearchRequest, split_path_scope
from nexus.server.api.v2.routers._search_batch import ParsedBatchSpec, parse_batch_query_spec

_AUTH = {
    "authenticated": True,
    "subject_type": "user",
    "subject_id": "alice",
    "zone_id": "eng",
    "zone_perms": [["eng", "r"]],
    "is_admin": False,
}


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, (None, ())),
        ("", (None, ())),
        ("/ws/documents/", ("/ws/documents/", ())),
        (["/ws/documents/"], ("/ws/documents/", ())),
        (["/ws/documents/", "", "/ws/documents/"], ("/ws/documents/", ())),
        (
            ["/ws/documents/", "/ws/notes/", "/ws/documents/"],
            (None, ("/ws/documents/", "/ws/notes/")),
        ),
    ],
)
def test_split_path_scope(raw: Any, expected: tuple[str | None, tuple[str, ...]]) -> None:
    assert split_path_scope(raw) == expected


def test_batch_spec_accepts_a_path_list() -> None:
    spec = parse_batch_query_spec({"q": "revenue", "path": ["/ws/documents/", "/ws/notes/"]})
    assert isinstance(spec, ParsedBatchSpec)
    assert spec.path_filter is None
    assert spec.path_filters == ("/ws/documents/", "/ws/notes/")

    single = parse_batch_query_spec({"q": "revenue", "path": "/ws/documents/"})
    assert isinstance(single, ParsedBatchSpec)
    assert single.path_filter == "/ws/documents/"
    assert single.path_filters == ()


@pytest.mark.parametrize("bad", [5, ["/ws/", 3], {"a": 1}])
def test_batch_spec_rejects_non_string_paths(bad: Any) -> None:
    assert parse_batch_query_spec({"q": "revenue", "path": bad}) == (
        "path must be a string or a list of strings"
    )


def test_daemon_maps_path_filters_onto_the_proto() -> None:
    from nexus.bricks.search.daemon import _request_to_pb

    pb = _request_to_pb(
        SearchRequest(query="q", path_filters=("/ws/documents/", "/ws/notes/")),
        chunks_per_page=0,
    )
    assert pb.path_filter == ""
    assert list(pb.path_filters) == ["/ws/documents/", "/ws/notes/"]


class _Runner:
    async def call(self, work: Any) -> Any:
        return await work()


class _Registry:
    def runner_for(self, zone_id: str) -> _Runner:
        return _Runner()


def _client(monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, MagicMock]:
    from nexus.server.api.v2.routers.search import router
    from nexus.server.dependencies import require_auth

    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("NEXUS_DATABASE_URL", raising=False)
    daemon = MagicMock()
    daemon.is_initialized = True
    daemon.config = MagicMock()
    daemon.config.txtai_graph = False
    daemon.search = AsyncMock(return_value=[])
    app = FastAPI()
    app.state.search_daemon = daemon
    app.state.record_store = object()
    app.state.async_read_session_factory = object()
    app.state.permission_enforcer = None
    app.state.zone_registry = _Registry()
    app.dependency_overrides[require_auth] = lambda: _AUTH
    app.include_router(router)
    return TestClient(app), daemon


def test_repeated_path_reaches_the_plugin_as_one_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    client, daemon = _client(monkeypatch)
    with client:
        resp = client.get(
            "/api/v2/search/query",
            params=[("q", "revenue"), ("path", "/ws/documents/"), ("path", "/ws/notes/")],
        )
    assert resp.status_code == 200, resp.text
    req = daemon.search.call_args.args[0]
    assert req.path_filter is None
    assert req.path_filters == ("/ws/documents/", "/ws/notes/")


def test_single_path_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    client, daemon = _client(monkeypatch)
    with client:
        resp = client.get("/api/v2/search/query", params={"q": "revenue", "path": "/ws/"})
    assert resp.status_code == 200, resp.text
    req = daemon.search.call_args.args[0]
    assert req.path_filter == "/ws/"
    assert req.path_filters == ()


@pytest.mark.parametrize("extra", [("graph_mode", "low"), ("federated", "true")])
def test_multiple_paths_are_refused_where_unsupported(
    monkeypatch: pytest.MonkeyPatch, extra: tuple[str, str]
) -> None:
    client, daemon = _client(monkeypatch)
    with client:
        resp = client.get(
            "/api/v2/search/query",
            params=[("q", "revenue"), ("path", "/a/"), ("path", "/b/"), extra],
        )
    assert resp.status_code == 400, resp.text
    assert "Multiple path prefixes" in resp.text
    daemon.search.assert_not_called()
