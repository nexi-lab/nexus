"""Compare: WriteBuffer ON vs OFF with real PostgreSQL + FastAPI.

Issue #1246 — Measure actual E2E latency difference.

Usage:
    PYTHONPATH=src python3.13 tests/benchmarks/e2e_write_buffer_pg_compare.py
"""

from __future__ import annotations

import base64
import os
import re
import signal
import socket
import subprocess
import sys
import time
import uuid
from contextlib import closing
from pathlib import Path

import httpx
from sqlalchemy import create_engine, text

PG_BASE = "postgresql://nexus_test:nexus_test_password@localhost:5433/nexus_test"
SRC_PATH = str(Path(__file__).parent.parent.parent / "src")
WRITE_COUNT = 50


def find_free_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("", 0))
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        return int(s.getsockname()[1])


def wait_for_server(url: str, timeout: float = 45.0) -> bool:
    start = time.time()
    while time.time() - start < timeout:
        try:
            resp = httpx.get(f"{url}/health", timeout=2.0, trust_env=False)
            if resp.status_code == 200:
                return True
        except (httpx.ConnectError, httpx.ReadTimeout):
            pass
        time.sleep(0.3)
    return False


def rpc(client: httpx.Client, method: str, params: dict, headers: dict | None = None) -> dict:
    resp = client.post(
        f"/api/nfs/{method}",
        json={"jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": method, "params": params},
        headers=headers,
    )
    return resp.json()


def encode_bytes(data: bytes) -> dict:
    return {"__type__": "bytes", "data": base64.b64encode(data).decode()}


def create_fresh_db(db_name: str):
    engine = create_engine(PG_BASE, isolation_level="AUTOCOMMIT")
    with engine.connect() as conn:
        conn.execute(text(f"DROP DATABASE IF EXISTS {db_name}"))
        conn.execute(text(f"CREATE DATABASE {db_name}"))
    engine.dispose()


def run_benchmark(db_name: str, enable_write_buffer: bool, write_count: int) -> dict:
    """Start a server, write files, measure latency, return results."""
    pg_url = f"postgresql://nexus_test:nexus_test_password@localhost:5433/{db_name}"
    create_fresh_db(db_name)

    import tempfile
    tmp_path = Path(tempfile.mkdtemp(prefix=f"nexus_wb_{db_name}_"))
    (tmp_path / "storage").mkdir()

    port = find_free_port()
    base_url = f"http://127.0.0.1:{port}"

    env = os.environ.copy()
    env["NEXUS_JWT_SECRET"] = "test-secret-key-for-e2e-12345"
    env["NEXUS_DATABASE_URL"] = pg_url
    env["NEXUS_ENABLE_WRITE_BUFFER"] = "true" if enable_write_buffer else "false"
    env["NEXUS_ENFORCE_PERMISSIONS"] = "true"
    env["NEXUS_RATE_LIMIT_ENABLED"] = "false"
    env["PYTHONPATH"] = SRC_PATH

    mode_name = "BUFFERED" if enable_write_buffer else "SYNC"
    print(f"\n  [{mode_name}] Starting server (port {port})...")

    process = subprocess.Popen(
        [
            sys.executable, "-c",
            (
                "from nexus.cli import main; "
                f"main(['serve', '--host', '127.0.0.1', '--port', '{port}', "
                f"'--data-dir', '{tmp_path}', "
                "'--auth-type', 'database', '--init'])"
            ),
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=str(tmp_path),
        preexec_fn=os.setsid,
    )

    try:
        if not wait_for_server(base_url, timeout=45.0):
            process.terminate()
            stdout, stderr = process.communicate(timeout=10)
            print(f"  FAIL: Server didn't start.\n  stderr: {stderr.decode()[:500]}")
            return {"error": "server_start_failed"}

        # Get admin key
        admin_api_key = None
        env_file = tmp_path / ".nexus-admin-env"
        if env_file.exists():
            for line in env_file.read_text().splitlines():
                m = re.search(r"NEXUS_API_KEY='([^']+)'", line)
                if m:
                    admin_api_key = m.group(1)
                    break

        if not admin_api_key:
            print("  FAIL: No admin API key")
            return {"error": "no_api_key"}

        print(f"  [{mode_name}] Writing {write_count} files...")

        latencies = []
        with httpx.Client(base_url=base_url, timeout=30.0, trust_env=False) as client:
            auth_headers = {"Authorization": f"Bearer {admin_api_key}"}

            for i in range(write_count):
                content = f"File content {i} - benchmark data for performance measurement".encode()
                t0 = time.perf_counter()
                result = rpc(client, "write", {
                    "path": f"/bench/file_{i:04d}.txt",
                    "content": encode_bytes(content),
                }, headers=auth_headers)
                latencies.append(time.perf_counter() - t0)

                if result.get("error"):
                    print(f"  FAIL at write {i}: {result['error']}")
                    return {"error": f"write_failed_{i}"}

        # Wait for flush if buffered
        if enable_write_buffer:
            time.sleep(2.0)

        # Count PG rows
        pg_engine = create_engine(pg_url)
        with pg_engine.connect() as conn:
            fp = conn.execute(text("SELECT count(*) FROM file_paths")).scalar() or 0
            op = conn.execute(text("SELECT count(*) FROM operation_log")).scalar() or 0
            vh = conn.execute(text("SELECT count(*) FROM version_history")).scalar() or 0
        pg_engine.dispose()

        import statistics
        return {
            "mode": mode_name,
            "writes": write_count,
            "total_sec": sum(latencies),
            "mean_ms": statistics.mean(latencies) * 1000,
            "median_ms": statistics.median(latencies) * 1000,
            "p95_ms": sorted(latencies)[int(write_count * 0.95)] * 1000,
            "throughput_wps": write_count / sum(latencies),
            "file_paths": fp,
            "operation_log": op,
            "version_history": vh,
        }

    finally:
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()


def main():
    print("=" * 70)
    print(f"E2E Comparison: WriteBuffer ON vs OFF (PostgreSQL, {WRITE_COUNT} writes)")
    print("=" * 70)

    # Run sync first
    print("\n--- SYNC (WriteBuffer OFF) ---")
    sync = run_benchmark("nexus_wb_sync", enable_write_buffer=False, write_count=WRITE_COUNT)
    if "error" in sync:
        print(f"Sync test failed: {sync['error']}")
        sys.exit(1)

    print(f"  Total: {sync['total_sec']:.3f}s | Mean: {sync['mean_ms']:.1f}ms | "
          f"P95: {sync['p95_ms']:.1f}ms | Throughput: {sync['throughput_wps']:.0f} w/s")
    print(f"  DB: file_paths={sync['file_paths']}, op_log={sync['operation_log']}, versions={sync['version_history']}")

    # Run buffered
    print("\n--- BUFFERED (WriteBuffer ON) ---")
    buf = run_benchmark("nexus_wb_buf", enable_write_buffer=True, write_count=WRITE_COUNT)
    if "error" in buf:
        print(f"Buffered test failed: {buf['error']}")
        sys.exit(1)

    print(f"  Total: {buf['total_sec']:.3f}s | Mean: {buf['mean_ms']:.1f}ms | "
          f"P95: {buf['p95_ms']:.1f}ms | Throughput: {buf['throughput_wps']:.0f} w/s")
    print(f"  DB: file_paths={buf['file_paths']}, op_log={buf['operation_log']}, versions={buf['version_history']}")

    # Comparison
    print(f"\n{'=' * 70}")
    print("COMPARISON (E2E: client → FastAPI → Raft+CAS → PostgreSQL)")
    print(f"{'=' * 70}")
    print(f"  {'Metric':<20} {'Sync':<15} {'Buffered':<15} {'Change':<15}")
    print(f"  {'-'*60}")

    speedup = sync['mean_ms'] / buf['mean_ms'] if buf['mean_ms'] > 0 else float('inf')
    print(f"  {'Mean latency':<20} {sync['mean_ms']:.1f}ms{'':<8} {buf['mean_ms']:.1f}ms{'':<8} {speedup:.1f}x {'faster' if speedup > 1 else 'slower'}")

    speedup_p95 = sync['p95_ms'] / buf['p95_ms'] if buf['p95_ms'] > 0 else float('inf')
    print(f"  {'P95 latency':<20} {sync['p95_ms']:.1f}ms{'':<8} {buf['p95_ms']:.1f}ms{'':<8} {speedup_p95:.1f}x {'faster' if speedup_p95 > 1 else 'slower'}")

    tp_change = buf['throughput_wps'] / sync['throughput_wps'] if sync['throughput_wps'] > 0 else float('inf')
    print(f"  {'Throughput':<20} {sync['throughput_wps']:.0f} w/s{'':<7} {buf['throughput_wps']:.0f} w/s{'':<7} {tp_change:.1f}x")

    print("\n  Note: E2E latency includes HTTP + JSON parsing + Raft consensus +")
    print("  CAS storage. The WriteBuffer only affects the PostgreSQL sync path.")
    print("  Pure PG benefit is ~2000x (see bench_write_buffer_pg.py).")
    print(f"{'=' * 70}")


if __name__ == "__main__":
    main()
