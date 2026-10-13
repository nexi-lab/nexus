"""Live document mutation/indexing adapters, verified through the Search SDK."""

from __future__ import annotations

import time
from collections.abc import Generator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.testkit.kernel import configure_search_plugin

pytestmark = pytest.mark.e2e


@dataclass
class LiveSearchApp:
    client: TestClient
    nx: Any
    headers: dict[str, str]


@pytest.fixture()
def live_search_app(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Generator[LiveSearchApp, None, None]:
    configure_search_plugin(tmp_path, monkeypatch)

    monkeypatch.setenv("NEXUS_ENFORCE_PERMISSIONS", "false")
    monkeypatch.setenv("NEXUS_SEARCH_DAEMON", "true")
    monkeypatch.setenv("NEXUS_ENABLE_WRITE_BUFFER", "false")
    monkeypatch.setenv("NEXUS_ACTIVITY_ENABLED", "0")
    monkeypatch.setenv("NEXUS_ACTIVITY_DB_PATH", str(tmp_path / "activity.db"))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    import nexus
    from nexus.server.fastapi_server import create_app

    database_url = f"sqlite:///{tmp_path / 'records.db'}"
    nx = nexus.connect(
        config={
            "data_dir": str(tmp_path / "data"),
            "profile": "full",
            "database_url": database_url,
            "enforce_permissions": False,
        }
    )

    try:
        kernel_addr = getattr(getattr(nx, "_kernel", None), "_server_address", None)
        assert kernel_addr, "the spawned Kernel must expose its Search gRPC listener"
        monkeypatch.setenv("NEXUS_SEARCH_PLUGIN_TARGET", str(kernel_addr))

        app = create_app(
            nexus_fs=nx,
            api_key="live-search-secret",
            database_url=database_url,
            data_dir=str(tmp_path),
        )
        headers = {"Authorization": "Bearer live-search-secret"}
        with TestClient(app, raise_server_exceptions=False) as client:
            yield LiveSearchApp(client=client, nx=nx, headers=headers)
    finally:
        close = getattr(nx, "close", None)
        if callable(close):
            close()


def _request(
    live: LiveSearchApp,
    method: str,
    path: str,
    *,
    max_wall_ms: float = 2_500.0,
    **kwargs: Any,
) -> tuple[Any, dict[str, Any]]:
    start = time.perf_counter()
    response = live.client.request(method.upper(), path, headers=live.headers, **kwargs)
    wall_ms = (time.perf_counter() - start) * 1000
    assert wall_ms < max_wall_ms, f"{method.upper()} {path} took {wall_ms:.1f}ms"
    try:
        body = response.json()
    except Exception:
        body = {"_raw": response.text}
    return response, body


def _query(live: LiveSearchApp, text: str) -> dict[str, Any]:
    async def query() -> list[dict[str, Any]]:
        return await live.nx.service("search").semantic_search(
            query=text, search_mode="keyword", limit=5
        )

    assert live.client.portal is not None
    return {"results": live.client.portal.call(query)}


def _stats(live: LiveSearchApp, *, max_wall_ms: float) -> dict[str, Any]:
    async def stats() -> dict[str, Any]:
        return await live.nx.service("search").semantic_search_stats()

    assert live.client.portal is not None
    start = time.perf_counter()
    result = live.client.portal.call(stats)
    elapsed_ms = (time.perf_counter() - start) * 1000
    assert elapsed_ms < max_wall_ms, f"Search SDK statistics took {elapsed_ms:.1f}ms"
    return result


def test_live_write_to_searchable_contract(live_search_app: LiveSearchApp) -> None:
    """#4736 acceptance, against the real kernel + worktree plugin.

    write → ONE indexing call → query serves the document, with the
    returned ``index_seq`` visible on ``/search/stats``; refresh on a
    missing / empty path is 404 / 409, never a bare "accepted"; delete
    evicts. The fixture requires a built cluster and signed Search plugin.
    """
    import base64

    live = live_search_app
    slow = 10_000.0  # indexing round-trips, not the read-path latency budget

    live.nx.mkdir("/ws", exist_ok=True)
    live.nx.write("/ws/doc.md", b"zebrafish sentinel document\n")

    # refresh(update) on an existing file → indexed + seq …
    r, body = _request(
        live,
        "post",
        "/api/v2/search/refresh",
        params={"path": "/ws/doc.md", "change_type": "update"},
        max_wall_ms=slow,
    )
    assert r.status_code == 200, body
    assert body["status"] == "indexed" and body["index_seq"] >= 1, body
    seq = body["index_seq"]

    # … stats caught up, in the token's zone …
    stats = _stats(live, max_wall_ms=slow)
    assert stats["last_index_seq"] >= seq, stats
    assert stats["pending"] == 0, stats
    assert stats["last_successful_index_at"] is not None, stats
    assert isinstance(stats["last_index_refresh"], float), stats

    # … and the document is served.
    q = _query(live, "zebrafish")
    assert any(x["path"] == "/ws/doc.md" for x in q["results"]), q

    # Missing path → 404, never "accepted".
    r, body = _request(
        live,
        "post",
        "/api/v2/search/refresh",
        params={"path": "/ws/missing.md", "change_type": "update"},
        max_wall_ms=slow,
    )
    assert r.status_code == 404, body
    assert "accepted" not in r.text

    # Empty content → 409 skipped.
    live.nx.write("/ws/empty.md", b"   \n")
    r, body = _request(
        live,
        "post",
        "/api/v2/search/refresh",
        params={"path": "/ws/empty.md", "change_type": "update"},
        max_wall_ms=slow,
    )
    assert r.status_code == 409, body
    assert body["detail"]["status"] == "skipped", body

    # write + index in ONE call → served.
    r, body = _request(
        live,
        "post",
        "/api/v2/files/write",
        json={"path": "/ws/one.md", "content": "quokka single call document", "index": True},
        max_wall_ms=slow,
    )
    assert r.status_code == 200, body
    assert body["index"]["status"] == "indexed", body
    assert body["index"]["index_seq"] > seq, body
    q = _query(live, "quokka")
    assert any(x["path"] == "/ws/one.md" for x in q["results"]), q

    # batch write + index: text is served, binary is reported skipped.
    r, body = _request(
        live,
        "post",
        "/api/v2/files/batch/write",
        json={
            "index": True,
            "files": [
                {
                    "path": "/ws/b1.md",
                    "content_base64": base64.b64encode(b"wallaby batch doc one").decode(),
                },
                {
                    "path": "/ws/b2.bin",
                    "content_base64": base64.b64encode(b"\xff\xfe\x00").decode(),
                },
            ],
        },
        max_wall_ms=slow,
    )
    assert r.status_code == 200, body
    by_path = {x["path"]: x for x in body["results"]}
    assert by_path["/ws/b1.md"]["index"]["status"] == "indexed", body
    assert by_path["/ws/b2.bin"]["index"]["status"] == "skipped", body
    q = _query(live, "wallaby")
    assert any(x["path"] == "/ws/b1.md" for x in q["results"]), q

    # delete → deleted, and the path is no longer served.
    r, body = _request(
        live,
        "post",
        "/api/v2/search/refresh",
        params={"path": "/ws/doc.md", "change_type": "delete"},
        max_wall_ms=slow,
    )
    assert r.status_code == 200 and body["status"] == "deleted", body
    q = _query(live, "zebrafish")
    assert not any(x["path"] == "/ws/doc.md" for x in q["results"]), q


def test_live_admin_reindex_indexes_replayed_paths(live_search_app: LiveSearchApp) -> None:
    """#4241 / #4736: ``POST /api/v2/admin/reindex`` with ``target=search``
    indexes every replayed path SYNCHRONOUSLY against the real kernel +
    plugin — the response counts completions, not queue depth, and the
    documents are served immediately afterwards."""
    live = live_search_app
    slow = 30_000.0

    live.nx.mkdir("/rx", exist_ok=True)
    live.nx.write("/rx/one.md", b"axolotl reindex document one\n")
    live.nx.write("/rx/two.md", b"axolotl reindex document two\n")
    live.nx.write("/rx/blob.bin", b"\xff\xfe\x00\x01")

    # Nothing is searchable yet — a bare write never indexes.
    q = _query(live, "axolotl")
    assert not any(x["path"].startswith("/rx/") for x in q["results"]), q

    r, body = _request(
        live, "post", "/api/v2/admin/reindex", json={"target": "search"}, max_wall_ms=slow
    )
    assert r.status_code == 200, body
    assert body["processed"] >= 3, body
    assert body["search_paths_indexed"] >= 2, body
    assert body["search_skip_reasons"].get("non_text", 0) >= 1, body
    assert body["search_index_errors"] == 0, body
    assert isinstance(body["search_index_seq"], int) and body["search_index_seq"] >= 1, body
    assert isinstance(body["search_indexed_at"], float), body
    for stale in ("search_paths_enqueued", "search_refresh_enqueued_at"):
        assert stale not in body, body

    stats = _stats(live, max_wall_ms=slow)
    assert stats["last_index_seq"] >= body["search_index_seq"], stats

    q = _query(live, "axolotl")
    hit_paths = {x["path"] for x in q["results"]}
    assert {"/rx/one.md", "/rx/two.md"} <= hit_paths, q
