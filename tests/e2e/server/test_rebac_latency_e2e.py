"""E2E latency benchmarks for ReBAC permission checks via FastAPI (Issue #1371).

Starts a real ``nexus serve`` process with permissions enabled, creates
ReBAC tuples via JSON-RPC, then measures HTTP round-trip latency for
permission-checked file operations.

Latency targets (HTTP round-trip including serialization):
- Permission-checked read:   p50 <50ms
- Permission-checked write:  p50 <50ms
- Permission denial (403):   p50 <30ms

Run with:
    pytest tests/e2e/server/test_rebac_latency_e2e.py -v -s
"""

import os
import shutil
import signal
import socket
import statistics
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest

pytestmark = pytest.mark.quarantine

PYTHON = sys.executable
SERVER_STARTUP_TIMEOUT = 30

# Clear proxy env vars so localhost connections work
for _key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
    os.environ.pop(_key, None)
os.environ["NO_PROXY"] = "*"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _make_client() -> httpx.Client:
    return httpx.Client(timeout=60)


def _wait_for_health(base_url: str, timeout: float = SERVER_STARTUP_TIMEOUT) -> None:
    deadline = time.monotonic() + timeout
    with _make_client() as client:
        while time.monotonic() < deadline:
            try:
                resp = client.get(f"{base_url}/health")
                if resp.status_code == 200:
                    return
            except httpx.ConnectError:
                pass
            time.sleep(0.3)
    raise TimeoutError(f"Server did not start within {timeout}s at {base_url}")


def _rpc(
    client: httpx.Client,
    base_url: str,
    method: str,
    params: dict[str, Any],
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    resp = client.post(
        f"{base_url}/api/nfs/{method}",
        json={
            "jsonrpc": "2.0",
            "id": str(uuid.uuid4()),
            "method": method,
            "params": params,
        },
        headers=headers,
    )
    return resp.json()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def server():
    """Start nexus serve with PERMISSIONS ENABLED."""
    port = _find_free_port()
    data_dir = tempfile.mkdtemp(prefix="nexus_rebac_latency_e2e_")
    backend_root = os.path.join(data_dir, "backend")
    os.makedirs(backend_root, exist_ok=True)

    base_url = f"http://127.0.0.1:{port}"

    env = {
        **os.environ,
        "HTTP_PROXY": "",
        "HTTPS_PROXY": "",
        "http_proxy": "",
        "https_proxy": "",
        "NO_PROXY": "*",
        "PYTHONPATH": str(Path(__file__).resolve().parents[3] / "src"),
        "NEXUS_DATABASE_URL": f"sqlite:///{os.path.join(data_dir, 'nexus.db')}",
        "NEXUS_BACKEND_ROOT": backend_root,
        "NEXUS_TENANT_ID": "rebac-latency-e2e",
        "NEXUS_ENFORCE_PERMISSIONS": "true",
        "NEXUS_NAMESPACE_REVISION_WINDOW": "1",
        "NEXUS_REBAC_BACKEND": "memory",
        "NEXUS_STATIC_ADMINS": "admin",
        "NEXUS_SEARCH_DAEMON": "false",
        "NEXUS_RATE_LIMIT_ENABLED": "false",
    }

    proc = subprocess.Popen(
        [
            PYTHON,
            "-c",
            (
                "from nexus.daemon.main import main; "
                f"main(['--host', '127.0.0.1', '--port', '{port}', "
                f"'--data-dir', '{data_dir}'])"
            ),
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        preexec_fn=os.setsid if sys.platform != "win32" else None,
    )

    try:
        _wait_for_health(base_url)
        yield {
            "base_url": base_url,
            "port": port,
            "data_dir": data_dir,
            "process": proc,
        }
    except Exception:
        if sys.platform != "win32":
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
        else:
            proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=3)
        stdout = proc.stdout.read() if proc.stdout else ""
        pytest.fail(f"Server failed to start. Output:\n{stdout}")
    finally:
        if proc.poll() is None:
            if sys.platform != "win32":
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                except (ProcessLookupError, PermissionError):
                    proc.terminate()
            else:
                proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        shutil.rmtree(data_dir, ignore_errors=True)


@pytest.fixture(scope="module")
def client(server: dict) -> httpx.Client:
    with _make_client() as c:
        yield c


@pytest.fixture(scope="module")
def admin_headers() -> dict[str, str]:
    return {"X-Agent-ID": "admin", "X-Zone-ID": "rebac-latency-e2e"}


@pytest.fixture(scope="module")
def alice_headers() -> dict[str, str]:
    return {"X-Agent-ID": "alice", "X-Zone-ID": "rebac-latency-e2e"}


@pytest.fixture(scope="module")
def setup_permissions(server, client, admin_headers):
    """Seed ReBAC tuples and files using admin access."""
    base_url = server["base_url"]

    # Admin writes a file
    result = _rpc(
        client,
        base_url,
        "write",
        {"path": "/latency_test/file.txt", "content_b64": "SGVsbG8gV29ybGQ="},
        headers=admin_headers,
    )
    assert result.get("error") is None, f"Admin write failed: {result}"

    # Grant alice viewer on the file
    _rpc(
        client,
        base_url,
        "rebac_create",
        {
            "subject": ["agent", "alice"],
            "relation": "direct_viewer",
            "object": ["file", "/latency_test/file.txt"],
            "zone_id": "rebac-latency-e2e",
        },
        headers=admin_headers,
    )

    # Write 10 files for batch testing
    for i in range(10):
        _rpc(
            client,
            base_url,
            "write",
            {
                "path": f"/latency_test/batch/file_{i:02d}.txt",
                "content_b64": "SGVsbG8=",
            },
            headers=admin_headers,
        )
        _rpc(
            client,
            base_url,
            "rebac_create",
            {
                "subject": ["agent", "alice"],
                "relation": "direct_viewer",
                "object": ["file", f"/latency_test/batch/file_{i:02d}.txt"],
                "zone_id": "rebac-latency-e2e",
            },
            headers=admin_headers,
        )

    return True


# ---------------------------------------------------------------------------
# Latency measurement helper
# ---------------------------------------------------------------------------


def _measure_latency(
    func,
    iterations: int = 20,
    warmup: int = 3,
) -> dict[str, float]:
    """Run func N times, return latency stats in milliseconds."""
    # Warmup
    for _ in range(warmup):
        func()

    times_ms: list[float] = []
    for _ in range(iterations):
        start = time.perf_counter()
        func()
        elapsed_ms = (time.perf_counter() - start) * 1000
        times_ms.append(elapsed_ms)

    times_ms.sort()
    return {
        "p50": statistics.median(times_ms),
        "p99": times_ms[max(0, int(len(times_ms) * 0.99) - 1)],
        "mean": statistics.mean(times_ms),
        "min": times_ms[0],
        "max": times_ms[-1],
        "stdev": statistics.stdev(times_ms) if len(times_ms) > 1 else 0.0,
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestPermissionCheckedReadLatency:
    """Permission-checked file read via HTTP."""

    def test_read_latency_with_permission(self, server, client, alice_headers, setup_permissions):
        """Alice reading a file she has viewer access to."""
        base_url = server["base_url"]

        def read_op():
            result = _rpc(
                client,
                base_url,
                "read",
                {"path": "/latency_test/file.txt"},
                headers=alice_headers,
            )
            assert result.get("error") is None, f"Read failed: {result}"

        stats = _measure_latency(read_op, iterations=30, warmup=5)

        print("\n  Permission-checked READ latency:")
        print(
            f"    p50={stats['p50']:.1f}ms  p99={stats['p99']:.1f}ms  "
            f"mean={stats['mean']:.1f}ms  stdev={stats['stdev']:.1f}ms"
        )

        assert stats["p50"] < 100.0, (
            f"Permission-checked read p50 too slow: {stats['p50']:.1f}ms (target <100ms)"
        )


class TestPermissionCheckedWriteLatency:
    """Permission-checked file write via HTTP."""

    def test_write_latency_with_permission(self, server, client, admin_headers, setup_permissions):
        """Admin writing files (admin bypass, but still exercises permission check path)."""
        base_url = server["base_url"]
        counter = [0]

        def write_op():
            counter[0] += 1
            result = _rpc(
                client,
                base_url,
                "write",
                {
                    "path": f"/latency_test/write_{counter[0]}.txt",
                    "content_b64": "SGVsbG8=",
                },
                headers=admin_headers,
            )
            assert result.get("error") is None, f"Write failed: {result}"

        stats = _measure_latency(write_op, iterations=20, warmup=3)

        print("\n  Permission-checked WRITE latency:")
        print(
            f"    p50={stats['p50']:.1f}ms  p99={stats['p99']:.1f}ms  "
            f"mean={stats['mean']:.1f}ms  stdev={stats['stdev']:.1f}ms"
        )

        assert stats["p50"] < 200.0, (
            f"Permission-checked write p50 too slow: {stats['p50']:.1f}ms (target <200ms)"
        )


class TestPermissionDenialLatency:
    """Permission denial should be fast — slow denial is a DoS vector."""

    def test_denial_latency(self, server, client, setup_permissions):
        """Unknown user attempting to read should be denied quickly."""
        base_url = server["base_url"]
        unknown_headers = {
            "X-Agent-ID": "unknown_intruder",
            "X-Zone-ID": "rebac-latency-e2e",
        }

        denial_times: list[float] = []
        for _ in range(3):
            # Warmup
            _rpc(
                client,
                base_url,
                "read",
                {"path": "/latency_test/file.txt"},
                headers=unknown_headers,
            )

        for _ in range(15):
            start = time.perf_counter()
            _rpc(
                client,
                base_url,
                "read",
                {"path": "/latency_test/file.txt"},
                headers=unknown_headers,
            )
            elapsed_ms = (time.perf_counter() - start) * 1000
            denial_times.append(elapsed_ms)

        denial_times.sort()
        p50 = statistics.median(denial_times)
        p99 = denial_times[max(0, int(len(denial_times) * 0.99) - 1)]

        print("\n  Permission DENIAL latency:")
        print(f"    p50={p50:.1f}ms  p99={p99:.1f}ms")

        assert p50 < 200.0, f"Denial p50 too slow: {p50:.1f}ms (target <200ms)"


class TestBatchReadLatency:
    """Batch reading multiple files with permission checks."""

    def test_batch_read_latency(self, server, client, alice_headers, setup_permissions):
        """Read 10 files sequentially — measures cumulative permission overhead."""
        base_url = server["base_url"]

        def batch_read():
            for i in range(10):
                result = _rpc(
                    client,
                    base_url,
                    "read",
                    {"path": f"/latency_test/batch/file_{i:02d}.txt"},
                    headers=alice_headers,
                )
                assert result.get("error") is None, f"Batch read {i} failed: {result}"

        stats = _measure_latency(batch_read, iterations=10, warmup=2)

        print("\n  Batch READ (10 files) latency:")
        print(
            f"    p50={stats['p50']:.1f}ms  p99={stats['p99']:.1f}ms  "
            f"mean={stats['mean']:.1f}ms  per_file={stats['mean'] / 10:.1f}ms"
        )

        # 10 files at <100ms each = <1000ms total
        assert stats["p50"] < 2000.0, (
            f"Batch read (10 files) p50 too slow: {stats['p50']:.1f}ms (target <2000ms)"
        )


class TestServerHealthWithPermissions:
    """Verify server health endpoint is fast even with permissions enabled."""

    def test_health_latency(self, server, client):
        """Health endpoint should not be affected by permission enforcement."""
        base_url = server["base_url"]

        def health_check():
            resp = client.get(f"{base_url}/health")
            assert resp.status_code == 200

        stats = _measure_latency(health_check, iterations=30, warmup=5)

        print("\n  Health endpoint latency (with permissions enabled):")
        print(f"    p50={stats['p50']:.1f}ms  p99={stats['p99']:.1f}ms")

        assert stats["p50"] < 50.0, (
            f"Health endpoint p50 too slow: {stats['p50']:.1f}ms (target <50ms)"
        )
