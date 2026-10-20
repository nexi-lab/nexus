"""Live document mutation/indexing adapters, verified through the Search SDK."""

from __future__ import annotations

import socket
import time
from collections.abc import Generator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from tests.testkit.kernel import configure_search_plugin

pytestmark = pytest.mark.e2e


@dataclass
class LiveSearchApp:
    client: TestClient
    native: httpx.Client
    nx: Any
    headers: dict[str, str]


@pytest.fixture()
def live_search_app(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Generator[LiveSearchApp, None, None]:
    configure_search_plugin(tmp_path, monkeypatch)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        http_address = f"127.0.0.1:{listener.getsockname()[1]}"
    monkeypatch.setenv("NEXUS_HTTP_ADDR", http_address)
    monkeypatch.setenv("NEXUS_REBAC_ENABLED", "true")

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
        with (
            TestClient(app, raise_server_exceptions=False) as client,
            httpx.Client(base_url=f"http://{http_address}", timeout=30, trust_env=False) as native,
        ):
            deadline = time.monotonic() + 30
            while True:
                try:
                    response = native.get("/v2/search/health", headers=headers)
                    response.raise_for_status()
                    break
                except httpx.ConnectError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(0.02)
            yield LiveSearchApp(client=client, native=native, nx=nx, headers=headers)
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
    client = live.native if path.startswith("/v2/") else live.client
    response = client.request(method.upper(), path, headers=live.headers, **kwargs)
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


def test_display_path_read_uses_vfs(live_search_app: LiveSearchApp) -> None:
    live = live_search_app
    path = "/mnt/display/message.md"
    content = "# Visible\n\nmarigold message\n"
    live.nx.mkdir("/mnt/display", parents=True, exist_ok=True)
    live.nx.write(path, content.encode())
    response, body = _request(
        live, "get", "/api/v2/files/read", params={"path": path, "include_metadata": True}
    )
    assert response.status_code == 200, body
    assert body["content"] == content
    assert body["size"] == len(content.encode())
    assert body["content_id"]
    response, body = _request(
        live, "get", "/api/v2/files/read", params={"path": path, "section": "Missing"}
    )
    assert response.status_code == 404, body
    assert content not in response.text


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

    assert "/ws/doc.md" in live.nx.service("search").list("/ws", recursive=True)

    # Read and index the current VFS file in its owning Kernel.
    r, body = _request(
        live,
        "post",
        "/v2/documents/file",
        json={"path": "/ws/doc.md"},
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
        "/v2/documents/file",
        json={"path": "/ws/missing.md"},
        max_wall_ms=slow,
    )
    assert r.status_code == 404, body
    assert "accepted" not in r.text

    # Empty content → 409 skipped.
    live.nx.write("/ws/empty.md", b"   \n")
    r, body = _request(
        live,
        "post",
        "/v2/documents/file",
        json={"path": "/ws/empty.md"},
        max_wall_ms=slow,
    )
    assert r.status_code == 409, body
    assert body["status"] == "skipped" and body["reason"] == "empty", body
    assert body["index_seq"] > seq, body

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
        "delete",
        "/v2/documents/file",
        params={"path": "/ws/doc.md"},
        max_wall_ms=slow,
    )
    assert r.status_code == 200 and body["status"] == "deleted", body
    assert live.nx.read("/ws/doc.md") == b"zebrafish sentinel document\n"
    q = _query(live, "zebrafish")
    assert not any(x["path"] == "/ws/doc.md" for x in q["results"]), q
