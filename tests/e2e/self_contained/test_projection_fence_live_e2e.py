"""Live E2E for fenced projections (#4738) against the real Rust kernel.

Boots a kernel-backed NexusFS with the write-through observer
(``NEXUS_ENABLE_WRITE_BUFFER=true``), the FastAPI app and a SQLite
RecordStore, then checks the issue's acceptance criteria end to end:

* ``list_versions`` immediately after ``files/write`` returns the new
  version — no ``flush_write_observer`` call;
* the write response carries ``projection_seq`` and
  ``GET /api/v2/operations/wait?seq=`` answers 200 for it;
* the crash window (kernel committed, projection not) is repaired by
  ``POST /api/v2/admin/reconcile-projections`` — simulated by writing
  through the kernel with the Python post-hooks bypassed.

Skips unless a cluster binary is available (``NEXUS_KERNEL_BINARY`` or a
worktree ``target/`` build).
"""

from __future__ import annotations

import asyncio
import base64
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
    explicit = os.environ.get("NEXUS_KERNEL_BINARY")
    if explicit:
        candidate = Path(explicit)
        if candidate.exists() and os.access(candidate, os.X_OK):
            return candidate
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
class LiveApp:
    client: TestClient
    nx: Any
    headers: dict[str, str]


@pytest.fixture()
def live_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[LiveApp, None, None]:
    cluster = _resolve_cluster_binary()
    if cluster is None:
        pytest.skip("projection fence E2E requires a cluster binary (NEXUS_KERNEL_BINARY)")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "nexus-cluster").symlink_to(cluster)
    (bin_dir / "nexusd-cluster").symlink_to(cluster)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("NEXUS_KERNEL_BINARY", str(cluster))
    monkeypatch.setenv("NEXUS_ENFORCE_PERMISSIONS", "false")
    monkeypatch.setenv("NEXUS_SEARCH_DAEMON", "false")
    # The write-through group-commit observer is what this suite tests.
    monkeypatch.setenv("NEXUS_ENABLE_WRITE_BUFFER", "true")
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
        api_key="live-projection-secret",
        database_url=database_url,
        data_dir=str(tmp_path),
    )
    headers = {"Authorization": "Bearer live-projection-secret"}
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            yield LiveApp(client=client, nx=nx, headers=headers)
    finally:
        close = getattr(nx, "close", None)
        if callable(close):
            close()


def _list_versions(nx: Any, path: str) -> list[dict[str, Any]]:
    """The consumer named in #4738: VersionService.list_versions, no flush first."""
    service = nx.service("version_service")
    return asyncio.run(service.list_versions(path))


def _write(live: LiveApp, path: str, content: str) -> dict[str, Any]:
    resp = live.client.post(
        "/api/v2/files/write", headers=live.headers, json={"path": path, "content": content}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_list_versions_immediately_after_write_without_flush(live_app: LiveApp) -> None:
    live = live_app
    live.nx.mkdir("/ws", exist_ok=True)

    first = _write(live, "/ws/a.txt", "v1")
    assert isinstance(first["projection_seq"], int) and first["projection_seq"] >= 1
    versions = _list_versions(live.nx, "/ws/a.txt")
    assert [v["content_id"] for v in versions] == [first["content_id"]]

    second = _write(live, "/ws/a.txt", "v2")
    assert second["projection_seq"] > first["projection_seq"]
    versions = _list_versions(live.nx, "/ws/a.txt")
    assert [v["content_id"] for v in versions] == [second["content_id"], first["content_id"]]
    assert [v["version"] for v in versions] == [2, 1]

    # The fence answers immediately for a sequence the write already committed.
    started = time.perf_counter()
    wait = live.client.get(
        f"/api/v2/operations/wait?seq={second['projection_seq']}", headers=live.headers
    )
    assert wait.status_code == 200, wait.text
    body = wait.json()
    assert body["applied"] is True and body["latest_seq"] >= second["projection_seq"]
    assert (time.perf_counter() - started) < 2.0

    # A sequence nobody has committed yet is an honest 412, not an empty answer.
    missing = live.client.get(
        f"/api/v2/operations/wait?seq={second['projection_seq'] + 1000}&timeout_ms=0",
        headers=live.headers,
    )
    assert missing.status_code == 412, missing.text
    assert missing.json()["detail"]["error"] == "projection_not_applied"

    # The operation log carries the same sequence numbers.
    ops = live.client.get(
        "/api/v2/operations?path_pattern=/ws/a.txt&limit=10", headers=live.headers
    )
    assert ops.status_code == 200, ops.text
    assert len(ops.json()["operations"]) == 2


def test_batch_write_returns_per_item_projection_seq(live_app: LiveApp) -> None:
    live = live_app
    live.nx.mkdir("/batch", exist_ok=True)
    files = [
        {"path": f"/batch/f{i}.txt", "content_base64": base64.b64encode(f"c{i}".encode()).decode()}
        for i in range(3)
    ]
    resp = live.client.post(
        "/api/v2/files/batch/write", headers=live.headers, json={"files": files}
    )
    assert resp.status_code == 200, resp.text
    seqs = [r["projection_seq"] for r in resp.json()["results"]]
    assert all(isinstance(s, int) for s in seqs)
    assert seqs == sorted(seqs) and len(set(seqs)) == 3
    for i in range(3):
        assert len(_list_versions(live.nx, f"/batch/f{i}.txt")) == 1


def test_mkdir_rename_delete_return_projection_seq(live_app: LiveApp) -> None:
    live = live_app
    made = live.client.post("/api/v2/files/mkdir", headers=live.headers, json={"path": "/mut"})
    assert made.status_code == 200, made.text
    mkdir_seq = made.json()["projection_seq"]
    assert isinstance(mkdir_seq, int)

    written = _write(live, "/mut/a.txt", "x")
    renamed = live.client.post(
        "/api/v2/files/rename",
        headers=live.headers,
        json={"source": "/mut/a.txt", "destination": "/mut/b.txt"},
    )
    assert renamed.status_code == 200, renamed.text
    rename_seq = renamed.json()["projection_seq"]
    assert isinstance(rename_seq, int) and rename_seq > written["projection_seq"]

    deleted = live.client.delete("/api/v2/files/delete?path=/mut/b.txt", headers=live.headers)
    assert deleted.status_code == 200, deleted.text
    delete_seq = deleted.json()["projection_seq"]
    assert isinstance(delete_seq, int) and delete_seq > rename_seq

    # Each sequence is a committed operation_log row the fence answers for.
    for seq in (mkdir_seq, rename_seq, delete_seq):
        wait = live.client.get(
            f"/api/v2/operations/wait?seq={seq}&timeout_ms=0", headers=live.headers
        )
        assert wait.status_code == 200, wait.text

    # Python API shapes: dicts carrying the sequence (mkdir used to return None).
    assert live.nx.mkdir("/mut2", exist_ok=True)["projection_seq"] is not None
    assert live.nx.sys_rename("/mut2", "/mut3")["projection_seq"] is not None
    assert live.nx.rmdir("/mut3")["projection_seq"] is not None

    # The deprecated JSON-RPC route carries it on the legacy wire shapes too.
    rpc = live.client.post(
        "/api/nfs/mkdir", headers=live.headers, json={"method": "mkdir", "params": {"path": "/rpc"}}
    )
    assert rpc.status_code == 200, rpc.text
    result = rpc.json().get("result", rpc.json())
    assert result.get("created") is True and isinstance(result.get("projection_seq"), int)


def test_reconcile_repairs_a_kernel_write_the_projection_never_saw(live_app: LiveApp) -> None:
    """Crash-window simulation: the kernel committed, the projection did not."""
    live = live_app
    live.nx.mkdir("/crash", exist_ok=True)
    seen = _write(live, "/crash/a.txt", "seen")
    _write(live, "/crash/b.txt", "b")
    assert [v["content_id"] for v in _list_versions(live.nx, "/crash/a.txt")] == [
        seen["content_id"]
    ]

    # Write straight through the kernel, bypassing the Python post-hooks that
    # feed the observer — the state a kill -9 between the two commits leaves.
    rust_ctx = live.nx._build_rust_ctx(None, True)
    live.nx._kernel.sys_write("/crash/a.txt", rust_ctx, b"lost", 0)
    live.nx._kernel.sys_write("/crash/new.txt", rust_ctx, b"never projected", 0)
    kernel_a = live.nx.sys_stat("/crash/a.txt")
    kernel_new = live.nx.sys_stat("/crash/new.txt")
    # Kernel is ahead of the projection: version 2 in the kernel, one row in
    # history.  (Compare versions, not content_id — a path-addressed local
    # backend reports the path as content_id for every write.)
    assert kernel_a["version"] == 2 and kernel_a["gen"] >= 2
    stale = _list_versions(live.nx, "/crash/a.txt")
    assert [v["version"] for v in stale] == [1] and stale[0]["content_id"] == seen["content_id"]
    assert _list_versions(live.nx, "/crash/new.txt") == []

    dry = live.client.post(
        "/api/v2/admin/reconcile-projections",
        headers=live.headers,
        json={"prefix": "/crash", "dry_run": True},
    )
    assert dry.status_code == 200, dry.text
    assert dry.json()["repaired"] == 1 and dry.json()["created"] == 1 and dry.json()["in_sync"] == 1
    assert _list_versions(live.nx, "/crash/new.txt") == [], "dry run writes nothing"

    fix = live.client.post(
        "/api/v2/admin/reconcile-projections", headers=live.headers, json={"prefix": "/crash"}
    )
    assert fix.status_code == 200, fix.text
    body = fix.json()
    assert (
        body["scanned"],
        body["in_sync"],
        body["created"],
        body["repaired"],
        body["errors"],
    ) == (
        3,
        1,
        1,
        1,
        0,
    )
    assert body["repaired_paths"] == ["/crash/a.txt"] and body["created_paths"] == [
        "/crash/new.txt"
    ]

    # Projection now matches the kernel: newest version first, one per kernel version.
    a_versions = _list_versions(live.nx, "/crash/a.txt")
    assert [v["content_id"] for v in a_versions] == [kernel_a["content_id"], seen["content_id"]]
    assert a_versions[0]["version"] == kernel_a["version"] == 2
    new_versions = _list_versions(live.nx, "/crash/new.txt")
    assert [v["content_id"] for v in new_versions] == [kernel_new["content_id"]]

    # Idempotent: a second pass finds everything in sync.
    again = live.client.post(
        "/api/v2/admin/reconcile-projections", headers=live.headers, json={"prefix": "/crash"}
    )
    assert again.json()["in_sync"] == 3 and again.json()["repaired"] == 0


def test_reconcile_requires_admin(live_app: LiveApp) -> None:
    resp = live_app.client.post(
        "/api/v2/admin/reconcile-projections",
        headers={"Authorization": "Bearer not-the-admin-key"},
        json={"prefix": "/"},
    )
    assert resp.status_code in (401, 403), resp.text
