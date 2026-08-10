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


def _resolve_search_plugin_dylib() -> Path | None:
    """Worktree build of the Rust search plugin, if present.

    When found, the fixture loads it into the spawned kernel via
    ``NEXUS_PLUGIN_DIR`` and points the app's SearchDaemon at the
    kernel's gRPC listener — the /search surface then runs against the
    REAL plugin instead of 503ing. Build with
    ``cargo build -p nexus-search-plugin``.
    """
    root = _repo_root()
    for directory in (
        root / "rust" / "target" / "release",
        root / "rust" / "target" / "debug",
        root / "target" / "release",
        root / "target" / "debug",
    ):
        for name in ("libnexus_search_plugin.dylib", "libnexus_search_plugin.so"):
            candidate = directory / name
            if candidate.exists():
                return candidate
    return None


@dataclass
class LiveSearchApp:
    client: TestClient
    nx: Any
    headers: dict[str, str]
    # True when the worktree search-plugin dylib was loaded into the
    # spawned kernel — tests that need real plugin-backed /search
    # endpoints skip when False instead of asserting into 503s.
    plugin_loaded: bool = False


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

    # Load the worktree search-plugin dylib into the spawned kernel when
    # available (#4620). Must be set BEFORE nexus.connect — the kernel
    # scans NEXUS_PLUGIN_DIR at boot. The kernel refuses unsigned
    # plugins, so detach-sign the dylib with an ephemeral test-local
    # Ed25519 key and trust it via NEXUS_LOCAL_TRUSTED_KEYS_DIR (the
    # loader's documented local-trust extension point; format contract
    # matches scripts/sign_plugin.py).
    plugin_dylib = _resolve_search_plugin_dylib()
    if plugin_dylib is not None:
        import base64

        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        plugins_dir = tmp_path / "plugins"
        plugins_dir.mkdir()
        (plugins_dir / plugin_dylib.name).symlink_to(plugin_dylib)
        signing_key = Ed25519PrivateKey.generate()
        (plugins_dir / (plugin_dylib.name + ".sig")).write_bytes(
            signing_key.sign(plugin_dylib.read_bytes())
        )
        trust_dir = tmp_path / "trusted_keys"
        trust_dir.mkdir()
        pub_raw = signing_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        (trust_dir / "live-e2e.pub").write_text(base64.b64encode(pub_raw).decode() + "\n")
        monkeypatch.setenv("NEXUS_PLUGIN_DIR", str(plugins_dir))
        monkeypatch.setenv("NEXUS_LOCAL_TRUSTED_KEYS_DIR", str(trust_dir))

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

    # The plugin's SearchService rides the kernel's own gRPC listener
    # (Phase P routing), which binds a RANDOM free port locally — not the
    # 2126 the SearchDaemon dials by default. Point the daemon at the
    # spawned kernel before create_app's lifespan probes it.
    plugin_loaded = False
    if plugin_dylib is not None:
        kernel_addr = getattr(getattr(nx, "_kernel", None), "_server_address", None)
        if kernel_addr:
            monkeypatch.setenv("NEXUS_SEARCH_PLUGIN_TARGET", str(kernel_addr))
            plugin_loaded = True

    app = create_app(
        nexus_fs=nx,
        api_key="live-search-secret",
        database_url=database_url,
        data_dir=str(tmp_path),
    )
    headers = {"Authorization": "Bearer live-search-secret"}

    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            # Decision #17 file-level ReBAC would deny every search hit for
            # the test API key (no permission tuples are seeded), silently
            # emptying /search/query responses. NEXUS_ENFORCE_PERMISSIONS
            # only governs VFS ops, so null the search-path enforcer — this
            # suite tests the search surface, not ReBAC.
            app.state.permission_enforcer = None
            yield LiveSearchApp(client=client, nx=nx, headers=headers, plugin_loaded=plugin_loaded)
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


@pytest.mark.xfail(
    reason=(
        "PR #4189 search surface E2E: glob/grep endpoints return empty in CI "
        "because the search daemon file index is not populated from kernel "
        "writes in the TestClient fixture (no lifespan auto-index hooks fire). "
        "Needs a manual refresh or fixture rework."
    ),
    strict=False,
)
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
        json={"mode": "sandbox"},
    )
    assert mode_response.status_code == 200
    assert mode_body["indexing_mode"] == "sandbox"

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


def test_live_batch_search_contract(live_search_app: LiveSearchApp) -> None:
    """The hardened `/query/batch` contract, gated in CI.

    Deliberately NOT part of
    ``test_live_search_http_surface_correctness_and_latency``: that test is
    xfailed for an unrelated reason (its glob/grep assertions need a file
    index the TestClient fixture never populates), which silently made these
    batch assertions dormant. This test seeds its own corpus through the
    explicit `/search/index` endpoint, so it depends on nothing the xfail
    covers and actually fails CI when the batch contract regresses.
    """
    live = live_search_app

    # The fixture starts empty and `/search/index` resolves path_id from the
    # file_paths projection, so the file must exist before it is indexed —
    # otherwise the call burns its full projection-wait budget and 409s.
    live.nx.mkdir("/workspace", exist_ok=True)
    live.nx.write(
        "/workspace/batch-contract.md",
        b"# Batch Contract\nhaystack marker for the batch contract test\n",
    )

    index_response, index_body = _request(
        live,
        "post",
        "/api/v2/search/index",
        max_wall_ms=15_000.0,
        json={
            "documents": [
                {
                    "id": "/workspace/batch-contract.md",
                    "path": "/workspace/batch-contract.md",
                    "text": "haystack marker for the batch contract test",
                }
            ]
        },
    )
    assert index_response.status_code == 200
    assert index_body["count"] == 1

    single_response, single_body = _request(
        live,
        "get",
        "/api/v2/search/query",
        params={"q": "haystack", "type": "keyword", "limit": 5},
    )
    assert single_response.status_code == 200

    batch_response, batch_body = _request(
        live,
        "post",
        "/api/v2/search/query/batch",
        json={
            "queries": [
                {"q": "haystack", "type": "keyword", "limit": 5},
                {"q": "haystack", "type": "hybrid", "limit": 5, "alpha": 0.3, "fusion": "weighted"},
                {"q": "haystack", "type": "semantic", "limit": 5},
            ]
        },
    )
    assert batch_response.status_code == 200
    assert batch_body["total_queries"] == 3
    # Lanes this deployment can serve report NO error — absence of the key is
    # the signal that an empty result set is genuine. Hybrid is included: a
    # fixture with no embedding provider must DEGRADE to keyword-only, exactly
    # as single `/query` does, not fail the query.
    for entry in batch_body["queries"][:2]:
        assert "error" not in entry, entry
    # A semantic-ONLY query is different: the caller asked for dense retrieval
    # this deployment cannot serve, so it must report the typed failure rather
    # than masquerading as a healthy empty result.
    semantic_entry = batch_body["queries"][2]
    assert semantic_entry["error"] == "Vector backend unavailable", semantic_entry
    assert semantic_entry["results"] == []
    # Serializer + result parity with single `/query`.
    assert [r["path"] for r in batch_body["queries"][0]["results"]] == [
        r["path"] for r in single_body["results"]
    ]
    for batch_hit, single_hit in zip(
        batch_body["queries"][0]["results"], single_body["results"], strict=True
    ):
        assert batch_hit == single_hit
    _assert_endpoint_latency(batch_body)

    # A per-query problem stays isolated to its own entry.
    mixed_response, mixed_body = _request(
        live,
        "post",
        "/api/v2/search/query/batch",
        json={"queries": [{"q": "haystack", "limit": 0}, {"q": "haystack", "limit": 2}]},
    )
    assert mixed_response.status_code == 200
    assert "limit" in mixed_body["queries"][0]["error"]
    assert "error" not in mixed_body["queries"][1]

    # Batch-level failures stay whole-request.
    empty_response, _ = _request(live, "post", "/api/v2/search/query/batch", json={"queries": []})
    assert empty_response.status_code == 400
    oversize_response, _ = _request(
        live,
        "post",
        "/api/v2/search/query/batch",
        json={"queries": [{"q": f"q{i}"} for i in range(501)]},
    )
    assert oversize_response.status_code == 400


def test_live_path_context_weight_moves_ranking(live_search_app: LiveSearchApp) -> None:
    """A weighted path-context row must move ranking through the HTTP surface (#4620).

    The exact differential-probe shape that caught the regression: two docs
    matching the same rare term under different prefixes (one keyword-stronger
    via repetition), then a ``weight: 10.0`` row upserted on the LOSER's
    prefix must flip the order on the next query. Pre-fix the rows persisted
    via CRUD but never reached ``QueryRequest.path_prefix_boosts``, so the
    order (and scores) stayed identical.
    """
    live = live_search_app
    if not live.plugin_loaded:
        pytest.skip(
            "requires the worktree search-plugin dylib; build it with "
            "`cargo build -p nexus-search-plugin`"
        )

    live.nx.mkdir("/workspace", exist_ok=True)
    live.nx.mkdir("/workspace/win", exist_ok=True)
    live.nx.mkdir("/workspace/lose", exist_ok=True)
    live.nx.write(
        "/workspace/win/win.md",
        b"zebrafly zebrafly zebrafly zebrafly keyword-strong doc\n",
    )
    live.nx.write("/workspace/lose/lose.md", b"zebrafly keyword-weak doc\n")

    index_response, index_body = _request(
        live,
        "post",
        "/api/v2/search/index",
        max_wall_ms=15_000.0,
        json={
            "documents": [
                {
                    "id": "/workspace/win/win.md",
                    "path": "/workspace/win/win.md",
                    "text": "zebrafly zebrafly zebrafly zebrafly keyword-strong doc",
                },
                {
                    "id": "/workspace/lose/lose.md",
                    "path": "/workspace/lose/lose.md",
                    "text": "zebrafly keyword-weak doc",
                },
            ]
        },
    )
    assert index_response.status_code == 200
    # Tolerate the count-shape drift tracked in #4617 (int pre-P12,
    # {"indexed": …, "skipped": …} from the P12 proxy) — this test is
    # about ranking, not the index response contract.
    raw_count = index_body["count"]
    indexed = raw_count["indexed"] if isinstance(raw_count, dict) else raw_count
    assert indexed == 2, index_body

    # Index visibility is eventually-consistent (#4623) — poll until both
    # docs are queryable before asserting on order.
    deadline = time.monotonic() + 5.0
    before_paths: list[str] = []
    while time.monotonic() < deadline:
        before_response, before_body = _request(
            live,
            "get",
            "/api/v2/search/query",
            params={"q": "zebrafly", "type": "keyword", "limit": 5},
        )
        assert before_response.status_code == 200
        before_paths = [r["path"] for r in before_body["results"]]
        if len(before_paths) >= 2:
            break
        time.sleep(0.1)
    assert before_paths[:2] == ["/workspace/win/win.md", "/workspace/lose/lose.md"], before_paths

    put_response, _ = _request(
        live,
        "put",
        "/api/v2/path-contexts/",
        json={
            "zone_id": "root",
            "path_prefix": "workspace/lose",
            "description": "tier-boosted loser prefix",
            "weight": 10.0,
        },
    )
    assert put_response.status_code == 200

    after_response, after_body = _request(
        live,
        "get",
        "/api/v2/search/query",
        params={"q": "zebrafly", "type": "keyword", "limit": 5},
    )
    assert after_response.status_code == 200
    after_paths = [r["path"] for r in after_body["results"]]
    assert after_paths[:2] == ["/workspace/lose/lose.md", "/workspace/win/win.md"], (
        f"weight 10.0 on the loser prefix must flip the order: "
        f"before={before_paths} after={after_paths}"
    )

    # And deleting the row must restore the raw BM25 order — the boost is
    # config, not index state.
    delete_response, _ = _request(
        live,
        "delete",
        "/api/v2/path-contexts/",
        params={"zone_id": "root", "path_prefix": "workspace/lose"},
    )
    assert delete_response.status_code == 200

    reset_response, reset_body = _request(
        live,
        "get",
        "/api/v2/search/query",
        params={"q": "zebrafly", "type": "keyword", "limit": 5},
    )
    assert reset_response.status_code == 200
    reset_paths = [r["path"] for r in reset_body["results"]]
    assert reset_paths[:2] == ["/workspace/win/win.md", "/workspace/lose/lose.md"], reset_paths
