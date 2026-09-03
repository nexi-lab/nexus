"""#4738 acceptance: ``kill -9`` during a write burst loses zero version rows.

Boots a real ``nexusd`` (Rust kernel subprocess + SQLite RecordStore), fires
a burst of ``files/write`` calls from several threads, SIGKILLs the whole
server process group mid-burst, restarts ``nexusd`` on the same data
directory, runs ``POST /api/v2/admin/reconcile-projections`` and then
compares the projection against the kernel path by path:

* every file the kernel has under the burst prefix has an active
  ``file_paths`` row whose ``current_version`` equals the kernel ``version``,
  exactly that many ``version_history`` rows, and at least that many
  ``operation_log`` write rows;
* nothing the kernel does not have is projected.

The burst size defaults to the issue's 20 000 writes; set
``NEXUS_CRASH_BURST`` to shrink it locally.  Skips without a cluster
binary (``NEXUS_KERNEL_BINARY`` or a worktree ``target/`` build).
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Generator
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select

pytestmark = [pytest.mark.e2e, pytest.mark.slow]

_BURST = int(os.environ.get("NEXUS_CRASH_BURST", "20000"))
_THREADS = 8
_KILL_AT_FRACTION = 0.4
_API_KEY = "crash-e2e-admin-key"
_PREFIX = "/burst"


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


def _free_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@dataclass
class _Server:
    env: dict[str, str]
    data_dir: Path
    port: int = 0
    pgid: int = 0
    process: subprocess.Popen[bytes] | None = None
    lines: list[str] = field(default_factory=list)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self, timeout: float = 180.0) -> None:
        self.port = _free_port()
        nexusd_bin = str(Path(sys.executable).parent / "nexusd")
        self.lines = []
        self.process = subprocess.Popen(
            [
                nexusd_bin,
                "--host",
                "127.0.0.1",
                "--port",
                str(self.port),
                "--data-dir",
                str(self.data_dir),
                "--api-key",
                _API_KEY,
                "--auth-type",
                "static",
                "--log-level",
                "info",
            ],
            env=self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            # Own process group: nexusd's kernel child inherits it, so one
            # SIGKILL to the group is the whole-node crash we want.
            preexec_fn=os.setsid,
        )
        self.pgid = os.getpgid(self.process.pid)

        def _drain(pipe: Any) -> None:
            try:
                for raw in iter(pipe.readline, b""):
                    self.lines.append(raw.decode(errors="replace"))
            except ValueError:
                pass

        threading.Thread(target=_drain, args=(self.process.stdout,), daemon=True).start()

        # Readiness = the HTTP health route answers; log markers depend on
        # the log level and uvicorn's formatter.
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                pytest.fail(
                    f"nexusd exited with {self.process.returncode} before ready:\n"
                    + "".join(self.lines[-60:])
                )
            try:
                r = httpx.get(f"{self.base_url}/health", timeout=2.0)
                if r.status_code < 500:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.25)
        self.stop()
        pytest.fail("nexusd did not become ready:\n" + "".join(self.lines[-60:]))

    def _killpg(self, sig: int) -> None:
        try:
            os.killpg(self.pgid, sig)
        except ProcessLookupError:
            pass

    def kill9(self) -> None:
        """The crash: SIGKILL the server AND its kernel child, no shutdown hooks."""
        assert self.process is not None
        self._killpg(signal.SIGKILL)
        self.process.wait(timeout=30)
        time.sleep(0.5)
        self._killpg(signal.SIGKILL)  # anything the group still holds

    def stop(self) -> None:
        if self.process is None:
            return
        if self.process.poll() is None:
            self._killpg(signal.SIGTERM)
            try:
                self.process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                pass
        # The kernel child does not always honour SIGTERM promptly; never
        # leave it orphaned behind the test.
        time.sleep(0.5)
        self._killpg(signal.SIGKILL)
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass


@pytest.fixture()
def crash_server(tmp_path: Path) -> Generator[_Server, None, None]:
    cluster = _resolve_cluster_binary()
    if cluster is None:
        pytest.skip("crash-recovery E2E requires a cluster binary (NEXUS_KERNEL_BINARY)")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "nexus-cluster").symlink_to(cluster)
    (bin_dir / "nexusd-cluster").symlink_to(cluster)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{bin_dir}{os.pathsep}{env.get('PATH', '')}",
            "NEXUS_KERNEL_BINARY": str(cluster),
            "NEXUS_DATA_DIR": str(data_dir),
            "NEXUS_DATABASE_URL": f"sqlite:///{tmp_path / 'records.db'}",
            "NEXUS_API_KEY": _API_KEY,
            # The observer under test; the pytest process env forces the
            # legacy one for the in-process suite.
            "NEXUS_ENABLE_WRITE_BUFFER": "true",
            "NEXUS_ENFORCE_PERMISSIONS": "false",
            "NEXUS_SEARCH_DAEMON": "false",
            "NEXUS_ACTIVITY_ENABLED": "0",
            "NEXUS_ACTIVITY_DB_PATH": str(tmp_path / "activity.db"),
        }
    )
    for noisy in ("OPENAI_API_KEY", "OPENROUTER_API_KEY"):
        env.pop(noisy, None)
    server = _Server(env=env, data_dir=data_dir)
    try:
        yield server
    finally:
        server.stop()


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {_API_KEY}"}


def _kernel_files(base_url: str, *, settle_s: float = 60.0) -> dict[str, dict[str, Any]]:
    """Recursive kernel listing under the burst prefix (files with content).

    Polls for up to ``settle_s`` after a restart: the kernel answers health
    before the zone's raft log has finished replaying.
    """
    deadline = time.monotonic() + settle_s
    last_body: Any = None
    while True:
        resp = httpx.post(
            f"{base_url}/api/nfs/sys_readdir",
            headers=_headers(),
            json={
                "method": "sys_readdir",
                "params": {"path": _PREFIX, "recursive": True, "details": True},
            },
            timeout=120.0,
        )
        assert resp.status_code == 200, resp.text
        last_body = resp.json()
        entries = last_body.get("result", last_body) if isinstance(last_body, dict) else last_body
        if isinstance(entries, dict):  # sys_readdir wire shape: {"files": [...], ...}
            entries = entries.get("files") or []
        files = {
            e["path"]: e
            for e in (entries or [])
            if isinstance(e, dict) and e.get("content_id") and not e.get("is_directory")
        }
        if files or time.monotonic() >= deadline:
            if not files:
                print(f"\nsys_readdir({_PREFIX}) after restart: {str(last_body)[:800]}")
            return files
        time.sleep(1.0)


def test_kill_9_during_burst_loses_zero_version_rows(crash_server: _Server, tmp_path: Path) -> None:
    server = crash_server
    server.start()
    with httpx.Client(base_url=server.base_url, headers=_headers(), timeout=30.0) as c:
        assert c.post("/api/v2/files/mkdir", json={"path": _PREFIX}).status_code == 200

    acked: set[str] = set()
    acked_lock = threading.Lock()
    attempted = 0
    attempted_lock = threading.Lock()
    stop = threading.Event()
    per_thread = _BURST // _THREADS

    def worker(t: int) -> None:
        nonlocal attempted
        with httpx.Client(base_url=server.base_url, headers=_headers(), timeout=30.0) as c:
            for i in range(per_thread):
                if stop.is_set():
                    return
                path = f"{_PREFIX}/t{t}/f{i:05d}.txt"
                with attempted_lock:
                    attempted += 1
                try:
                    r = c.post("/api/v2/files/write", json={"path": path, "content": f"{t}:{i}"})
                except httpx.HTTPError:
                    return  # the crash happened under us
                if r.status_code == 200 and isinstance(r.json().get("projection_seq"), int):
                    with acked_lock:
                        acked.add(path)

    threads = [threading.Thread(target=worker, args=(t,), daemon=True) for t in range(_THREADS)]
    started = time.monotonic()
    for th in threads:
        th.start()
    kill_at = int(_BURST * _KILL_AT_FRACTION)
    while time.monotonic() - started < 600:
        with acked_lock:
            done = len(acked)
        if done >= kill_at or all(not th.is_alive() for th in threads):
            break
        time.sleep(0.02)

    # ── The crash ────────────────────────────────────────────────────
    server.kill9()
    stop.set()
    for th in threads:
        th.join(timeout=60)
    acked_before_crash = set(acked)
    assert acked_before_crash, "no write was acknowledged before the crash"

    # ── Restart on the same data dir + RecordStore, then reconcile ───
    server.start()
    kernel = _kernel_files(server.base_url)
    assert kernel, "kernel lost every write under the burst prefix"
    kernel_missing_acked = sorted(acked_before_crash - set(kernel))

    reconcile = httpx.post(
        f"{server.base_url}/api/v2/admin/reconcile-projections",
        headers=_headers(),
        json={"prefix": _PREFIX},
        timeout=600.0,
    )
    assert reconcile.status_code == 200, reconcile.text
    report = reconcile.json()
    assert report["errors"] == 0, report

    # ── Compare projection against kernel, path by path ──────────────
    from nexus.storage.models import FilePathModel, OperationLogModel, VersionHistoryModel
    from nexus.storage.record_store import SQLAlchemyRecordStore

    rs = SQLAlchemyRecordStore(db_path=tmp_path / "records.db")
    try:
        with rs.session_factory() as session:
            rows = (
                session.execute(
                    select(FilePathModel).where(
                        FilePathModel.virtual_path.like(f"{_PREFIX}/%"),
                        FilePathModel.deleted_at.is_(None),
                    )
                )
                .scalars()
                .all()
            )
            projected = {r.virtual_path: r for r in rows}
            version_counts = dict(
                session.execute(
                    select(VersionHistoryModel.resource_id, func.count())
                    .where(VersionHistoryModel.resource_type == "file")
                    .group_by(VersionHistoryModel.resource_id)
                ).all()
            )
            write_rows = dict(
                session.execute(
                    select(OperationLogModel.path, func.count())
                    .where(
                        OperationLogModel.operation_type == "write",
                        OperationLogModel.path.like(f"{_PREFIX}/%"),
                    )
                    .group_by(OperationLogModel.path)
                ).all()
            )
    finally:
        rs.close()

    summary = {
        "burst": _BURST,
        "attempted": attempted,
        "acked_before_crash": len(acked_before_crash),
        "kernel_files": len(kernel),
        "acked_but_missing_in_kernel": len(kernel_missing_acked),
        "reconcile": {k: report[k] for k in ("scanned", "in_sync", "created", "repaired")},
        "elapsed_s": round(time.monotonic() - started, 1),
    }
    print(f"\ncrash-recovery summary: {summary}")

    mismatches: list[str] = []
    for path, entry in kernel.items():
        row = projected.get(path)
        if row is None:
            mismatches.append(f"{path}: no file_paths row")
            continue
        kernel_version = int(entry.get("version") or 1)
        if row.current_version != kernel_version:
            mismatches.append(
                f"{path}: current_version {row.current_version} != kernel {kernel_version}"
            )
        if version_counts.get(row.path_id, 0) != kernel_version:
            mismatches.append(
                f"{path}: {version_counts.get(row.path_id, 0)} version rows != {kernel_version}"
            )
        if write_rows.get(path, 0) < kernel_version:
            mismatches.append(f"{path}: {write_rows.get(path, 0)} operation_log rows")
    extra = sorted(set(projected) - set(kernel))
    assert not mismatches, mismatches[:20]
    assert not extra, extra[:20]
    # The kernel is authoritative; an acknowledged write it does not hold
    # after the crash would be a kernel durability bug, not a projection one.
    assert not kernel_missing_acked, kernel_missing_acked[:20]
