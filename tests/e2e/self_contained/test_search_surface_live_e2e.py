"""Live E2E coverage for the search/path-context HTTP surface (#4135).

This test boots the real Rust-backed NexusFS, the FastAPI app, and the
SearchDaemon. It intentionally avoids fake search services so regressions in
kernel listing, app-state DB wiring, schema bootstrap, and route behavior show
up as real API failures.
"""

from __future__ import annotations

import os
import time
from collections.abc import Generator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.e2e


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _resolve_cluster_binary() -> Path | None:
    root = _repo_root()
    for directory in (
        root / "rust" / "target" / "release",
        root / "rust" / "target" / "debug",
        root / "target" / "release",
        root / "target" / "debug",
    ):
        for name in ("nexusd-cluster", "nexus-cluster"):
            candidate = directory / name
            if candidate.exists() and os.access(candidate, os.X_OK):
                return candidate
    return None


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
    cluster = _resolve_cluster_binary()
    if cluster is None:
        pytest.skip(
            "live search E2E requires the worktree cluster binary; "
            "build it with `cargo build -p nexus-cluster`"
        )

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "nexus-cluster").symlink_to(cluster)

    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("NEXUS_ENFORCE_PERMISSIONS", "false")
    monkeypatch.setenv("NEXUS_SEARCH_DAEMON", "true")
    monkeypatch.setenv("NEXUS_TXTAI_USE_API_EMBEDDINGS", "false")
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
    app = create_app(
        nexus_fs=nx,
        api_key="live-search-secret",
        database_url=database_url,
        data_dir=str(tmp_path),
    )
    headers = {"Authorization": "Bearer live-search-secret"}

    try:
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


def _assert_endpoint_latency(body: dict[str, Any], *, key: str = "latency_ms") -> None:
    if key in body:
        assert body[key] < 1_000.0


def test_live_search_http_surface_correctness_and_latency(live_search_app: LiveSearchApp) -> None:
    live = live_search_app

    live.nx.mkdir("/workspace", exist_ok=True)
    live.nx.mkdir("/workspace/src", exist_ok=True)
    live.nx.write(
        "/workspace/src/main.py",
        b"# TODO: implement search e2e\nprint('needle')\n",
    )
    live.nx.write(
        "/workspace/docs.md",
        b"# Retrieval Guide\nneedle retrieval semantic index\n",
    )
    recursive_paths = {
        entry["path"]
        for entry in live.nx.sys_readdir("/workspace", recursive=True, details=True)
        if isinstance(entry, dict)
    }
    assert "/workspace/src/main.py" in recursive_paths

    health_response, health = _request(live, "get", "/api/v2/search/health")
    assert health_response.status_code == 200
    assert health["status"] == "healthy"
    assert health["initialized"] is True

    stats_response, stats = _request(live, "get", "/api/v2/search/stats")
    assert stats_response.status_code == 200
    assert stats["initialized"] is True

    glob_response, glob_body = _request(
        live,
        "get",
        "/api/v2/search/glob",
        params={"pattern": "**/*.py", "path": "/workspace", "limit": 5},
    )
    assert glob_response.status_code == 200
    assert "/workspace/src/main.py" in glob_body["items"]
    _assert_endpoint_latency(glob_body)

    glob_post_response, glob_post = _request(
        live,
        "post",
        "/api/v2/search/glob",
        json={"pattern": "**/*.md", "path": "/workspace", "limit": 5},
    )
    assert glob_post_response.status_code == 200
    assert glob_post["items"] == ["/workspace/docs.md"]
    _assert_endpoint_latency(glob_post)

    grep_response, grep_body = _request(
        live,
        "get",
        "/api/v2/search/grep",
        params={"pattern": "TODO", "path": "/workspace", "limit": 5},
    )
    assert grep_response.status_code == 200
    assert any(item["file"] == "/workspace/src/main.py" for item in grep_body["items"])
    _assert_endpoint_latency(grep_body)

    grep_post_response, grep_post = _request(
        live,
        "post",
        "/api/v2/search/grep",
        json={"pattern": "needle", "path": "/workspace", "limit": 5},
    )
    assert grep_post_response.status_code == 200
    assert {item["file"] for item in grep_post["items"]} == {
        "/workspace/docs.md",
        "/workspace/src/main.py",
    }
    _assert_endpoint_latency(grep_post)

    index_response, index_body = _request(
        live,
        "post",
        "/api/v2/search/index",
        json={
            "documents": [
                {
                    "id": "/workspace/docs.md",
                    "path": "/workspace/docs.md",
                    "text": "needle manual document",
                }
            ]
        },
    )
    assert index_response.status_code == 200
    assert index_body["status"] == "indexed"
    assert index_body["count"] == 1

    query_response, query_body = _request(
        live,
        "get",
        "/api/v2/search/query",
        params={"q": "needle", "type": "keyword", "limit": 5},
    )
    assert query_response.status_code == 200
    assert query_body["query"] == "needle"
    assert isinstance(query_body["results"], list)
    assert any(result["path"] == "/workspace/docs.md" for result in query_body["results"])
    _assert_endpoint_latency(query_body)

    batch_response, batch_body = _request(
        live,
        "post",
        "/api/v2/search/query/batch",
        json={"queries": [{"q": "needle", "limit": 2}, {"q": "retrieval", "limit": 2}]},
    )
    assert batch_response.status_code == 200
    assert batch_body["total_queries"] == 2
    assert len(batch_body["queries"]) == 2
    assert any(
        result["path"] == "/workspace/docs.md" for result in batch_body["queries"][0]["results"]
    )
    _assert_endpoint_latency(batch_body)

    refresh_response, refresh_body = _request(
        live,
        "post",
        "/api/v2/search/refresh",
        params={"path": "/workspace/src/main.py", "change_type": "update"},
    )
    assert refresh_response.status_code == 200
    assert refresh_body == {
        "status": "accepted",
        "path": "/workspace/src/main.py",
        "change_type": "update",
    }

    expand_response, expand_body = _request(
        live,
        "post",
        "/api/v2/search/expand",
        params={"q": "needle"},
    )
    assert expand_response.status_code == 503
    assert "No API key configured" in expand_body["detail"]

    put_context_response, put_context = _request(
        live,
        "put",
        "/api/v2/path-contexts/",
        json={
            "zone_id": "root",
            "path_prefix": "workspace/src",
            "description": "Source files",
        },
    )
    assert put_context_response.status_code == 200
    assert put_context["path_prefix"] == "workspace/src"

    list_context_response, list_context = _request(
        live,
        "get",
        "/api/v2/path-contexts/",
        params={"zone_id": "root"},
    )
    assert list_context_response.status_code == 200
    assert any(c["path_prefix"] == "workspace/src" for c in list_context["contexts"])

    delete_context_response, delete_context = _request(
        live,
        "delete",
        "/api/v2/path-contexts/",
        params={"zone_id": "root", "path_prefix": "workspace/src"},
    )
    assert delete_context_response.status_code == 200
    assert delete_context["status"] == "deleted"

    index_dir_response, index_dir = _request(
        live,
        "post",
        "/api/v2/search/index-directory",
        json={"path": "/workspace"},
    )
    assert index_dir_response.status_code == 200
    assert index_dir["path"] == "/workspace"
    assert index_dir["status"] in {"registered", "already_registered"}

    indexed_dirs_response, indexed_dirs = _request(live, "get", "/api/v2/search/indexed-dirs")
    assert indexed_dirs_response.status_code == 200
    assert "/workspace" in indexed_dirs["directories"]

    mode_response, mode_body = _request(
        live,
        "post",
        "/api/v2/search/indexing-mode",
        json={"mode": "scoped"},
    )
    assert mode_response.status_code == 200
    assert mode_body["indexing_mode"] == "scoped"

    purge_response, purge_body = _request(
        live,
        "post",
        "/api/v2/search/purge-unscoped",
        json={},
    )
    assert purge_response.status_code == 200
    assert set(purge_body["purged"]) >= {"document_chunks", "vector_docs", "txtai_docs"}

    unregister_response, unregister_body = _request(
        live,
        "delete",
        "/api/v2/search/index-directory",
        json={"path": "/workspace"},
    )
    assert unregister_response.status_code == 200
    assert unregister_body["status"] == "unregistered"

    deadline = time.monotonic() + 2.0
    locate_body: dict[str, Any] = {}
    while time.monotonic() < deadline:
        locate_response, locate_body = _request(
            live,
            "post",
            "/api/v2/search/locate",
            json={"q": "main", "limit": 5},
        )
        assert locate_response.status_code == 200
        if any(c["path"] == "/workspace/src/main.py" for c in locate_body["candidates"]):
            break
        time.sleep(0.1)
    assert any(c["path"] == "/workspace/src/main.py" for c in locate_body["candidates"])
    _assert_endpoint_latency(locate_body, key="elapsed_ms")
