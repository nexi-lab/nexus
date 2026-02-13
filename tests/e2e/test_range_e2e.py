"""E2E tests for HTTP Range request support (Issue #790).

Tests Range requests against a real nexus server with actual file persistence.

Two server configurations tested:
1. Open-access mode (NEXUS_ENFORCE_PERMISSIONS=false) — validates core range logic
2. Auth-enabled mode (--api-key with permissions auto-enabled) — validates range
   requests through the full authentication and permission pipeline

Run with: .venv/bin/python3.12 -m pytest tests/e2e/test_range_e2e.py -v -p no:xdist -o "addopts="
"""

from __future__ import annotations

import base64
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from contextlib import suppress
from pathlib import Path

import httpx
import pytest

# =============================================================================
# Shared helpers
# =============================================================================

PYTHON = sys.executable
SERVER_STARTUP_TIMEOUT = 30
SRC_PATH = str(Path(__file__).resolve().parents[2] / "src")

# 10KB test content with a repeating pattern for easy verification
TEST_CONTENT = bytes(range(256)) * 40  # 10240 bytes

# API key MUST start with "sk-" prefix for StaticAPIKeyAuth token discrimination
API_KEY = "sk-range-e2e-test-key-99999"
AUTH_HEADERS = {"Authorization": f"Bearer {API_KEY}"}


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_health(base_url: str, timeout: float = SERVER_STARTUP_TIMEOUT) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            resp = httpx.get(f"{base_url}/health", timeout=2.0, trust_env=False)
            if resp.status_code == 200:
                return
        except (httpx.ConnectError, httpx.ReadTimeout):
            pass
        time.sleep(0.3)
    raise TimeoutError(f"Server did not start within {timeout}s at {base_url}")


def _base_env(data_dir: str, backend_root: str) -> dict:
    """Common env vars for both server fixtures."""
    return {
        **os.environ,
        "HTTP_PROXY": "",
        "HTTPS_PROXY": "",
        "http_proxy": "",
        "https_proxy": "",
        "NO_PROXY": "*",
        "PYTHONPATH": SRC_PATH,
        "NEXUS_DATABASE_URL": f"sqlite:///{data_dir}/range_e2e.db",
        "NEXUS_BACKEND_ROOT": backend_root,
        "NEXUS_TENANT_ID": "range-e2e",
        "NEXUS_SEARCH_DAEMON": "false",
        "NEXUS_RATE_LIMIT_ENABLED": "false",
        "NEXUS_ENFORCE_ZONE_ISOLATION": "false",
    }


def _start_server(cli_args: list[str], env: dict) -> subprocess.Popen:
    """Start a nexus serve subprocess."""
    args_str = ", ".join(f"'{a}'" for a in cli_args)
    return subprocess.Popen(
        [PYTHON, "-c", f"from nexus.cli import main; main([{args_str}])"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        preexec_fn=os.setsid if sys.platform != "win32" else None,
    )


def _stop_server(proc: subprocess.Popen) -> None:
    """Gracefully stop a server subprocess."""
    if proc.poll() is not None:
        return
    if sys.platform != "win32":
        with suppress(ProcessLookupError, PermissionError):
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    else:
        proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def _write_file(
    client: httpx.Client, base_url: str, path: str, content: bytes,
    headers: dict | None = None,
) -> dict:
    """Upload a file via the v2 API and return write response data."""
    resp = client.post(
        f"{base_url}/api/v2/files/write",
        json={
            "path": path,
            "content": base64.b64encode(content).decode(),
            "encoding": "base64",
        },
        headers=headers or {},
    )
    assert resp.status_code == 200, f"Write failed: {resp.text}"
    return resp.json()


# =============================================================================
# Fixture 1: Open-access server (permissions disabled)
# =============================================================================


@pytest.fixture(scope="module")
def range_server():
    """Start nexus serve with permissions disabled for core range logic tests."""
    port = _find_free_port()
    data_dir = tempfile.mkdtemp(prefix="nexus_range_open_")
    backend_root = os.path.join(data_dir, "backend")
    os.makedirs(backend_root, exist_ok=True)

    base_url = f"http://127.0.0.1:{port}"
    env = _base_env(data_dir, backend_root)
    env["NEXUS_ENFORCE_PERMISSIONS"] = "false"

    cli_args = ["serve", "--host", "127.0.0.1", "--port", str(port), "--data-dir", data_dir]
    proc = _start_server(cli_args, env)

    try:
        _wait_for_health(base_url)
        yield {"base_url": base_url}
    except Exception:
        _stop_server(proc)
        stdout = proc.stdout.read() if proc.stdout else ""
        pytest.fail(f"Server failed to start. Output:\n{stdout}")
    finally:
        _stop_server(proc)
        shutil.rmtree(data_dir, ignore_errors=True)


# =============================================================================
# Fixture 2: Auth-enabled server (permissions auto-enabled via --api-key)
# =============================================================================


@pytest.fixture(scope="module")
def auth_server():
    """Start nexus serve with --api-key (auto-enables permissions).

    Admin API key bypasses all permission checks, validating that Range
    requests work through the full auth + permission pipeline.
    """
    port = _find_free_port()
    data_dir = tempfile.mkdtemp(prefix="nexus_range_auth_")
    backend_root = os.path.join(data_dir, "backend")
    os.makedirs(backend_root, exist_ok=True)

    base_url = f"http://127.0.0.1:{port}"
    env = _base_env(data_dir, backend_root)
    # Do NOT set NEXUS_ENFORCE_PERMISSIONS — let it default from has_auth=True

    cli_args = [
        "serve", "--host", "127.0.0.1", "--port", str(port),
        "--data-dir", data_dir, "--api-key", API_KEY,
    ]
    proc = _start_server(cli_args, env)

    try:
        _wait_for_health(base_url)
        yield {"base_url": base_url}
    except Exception:
        _stop_server(proc)
        stdout = proc.stdout.read() if proc.stdout else ""
        pytest.fail(f"Server failed to start. Output:\n{stdout}")
    finally:
        _stop_server(proc)
        shutil.rmtree(data_dir, ignore_errors=True)


# =============================================================================
# Part 1: Core Range logic (open-access server, no auth)
# =============================================================================

# Identity headers for open-access mode
OPEN_HEADERS = {
    "X-Nexus-Subject": "user:range_tester",
    "X-Nexus-Zone-ID": "range-e2e",
}


class TestBasicRange:
    """Basic range request: first 5KB of a 10KB file."""

    def test_range_first_half(self, range_server: dict) -> None:
        base = range_server["base_url"]
        with httpx.Client(timeout=30, trust_env=False) as c:
            _write_file(c, base, "/range-e2e/test1.bin", TEST_CONTENT, OPEN_HEADERS)

            resp = c.get(
                f"{base}/api/v2/files/stream",
                params={"path": "/range-e2e/test1.bin"},
                headers={**OPEN_HEADERS, "Range": "bytes=0-4999"},
            )
            assert resp.status_code == 206
            assert resp.content == TEST_CONTENT[:5000]
            assert "bytes 0-4999/10240" in resp.headers.get("content-range", "")
            assert resp.headers.get("accept-ranges") == "bytes"


class TestResumeDownload:
    """Simulate download resumption: request bytes from offset to end."""

    def test_range_from_offset(self, range_server: dict) -> None:
        base = range_server["base_url"]
        with httpx.Client(timeout=30, trust_env=False) as c:
            _write_file(c, base, "/range-e2e/test2.bin", TEST_CONTENT, OPEN_HEADERS)

            resp = c.get(
                f"{base}/api/v2/files/stream",
                params={"path": "/range-e2e/test2.bin"},
                headers={**OPEN_HEADERS, "Range": "bytes=5000-"},
            )
            assert resp.status_code == 206
            assert resp.content == TEST_CONTENT[5000:]
            assert "bytes 5000-10239/10240" in resp.headers.get("content-range", "")


class TestNoRange:
    """Without Range header, get full content with Accept-Ranges."""

    def test_full_download_has_accept_ranges(self, range_server: dict) -> None:
        base = range_server["base_url"]
        with httpx.Client(timeout=30, trust_env=False) as c:
            _write_file(c, base, "/range-e2e/test3.bin", TEST_CONTENT, OPEN_HEADERS)

            resp = c.get(
                f"{base}/api/v2/files/stream",
                params={"path": "/range-e2e/test3.bin"},
                headers=OPEN_HEADERS,
            )
            assert resp.status_code == 200
            assert resp.content == TEST_CONTENT
            assert resp.headers.get("accept-ranges") == "bytes"


class TestUnsatisfiableRange:
    """Range beyond file size returns 416."""

    def test_range_past_eof(self, range_server: dict) -> None:
        small_content = b"X" * 100
        base = range_server["base_url"]

        with httpx.Client(timeout=30, trust_env=False) as c:
            _write_file(c, base, "/range-e2e/small.bin", small_content, OPEN_HEADERS)

            resp = c.get(
                f"{base}/api/v2/files/stream",
                params={"path": "/range-e2e/small.bin"},
                headers={**OPEN_HEADERS, "Range": "bytes=200-300"},
            )
            assert resp.status_code == 416
            assert "bytes */100" in resp.headers.get("content-range", "")


# =============================================================================
# Part 2: Auth + Permissions pipeline (--api-key server)
# =============================================================================


class TestAuthRangeRequest:
    """Range requests through the full auth + permission pipeline.

    Server started with --api-key, so permissions are auto-enabled.
    Admin API key bypasses permission checks.
    """

    def test_admin_can_write_and_range_read(self, auth_server: dict) -> None:
        """Admin writes a file, then requests partial content via Range."""
        base = auth_server["base_url"]
        with httpx.Client(timeout=30, trust_env=False) as c:
            _write_file(c, base, "/auth-range/data.bin", TEST_CONTENT, AUTH_HEADERS)

            resp = c.get(
                f"{base}/api/v2/files/stream",
                params={"path": "/auth-range/data.bin"},
                headers={**AUTH_HEADERS, "Range": "bytes=0-999"},
            )
            assert resp.status_code == 206
            assert resp.content == TEST_CONTENT[:1000]
            assert "bytes 0-999/10240" in resp.headers.get("content-range", "")
            assert resp.headers.get("accept-ranges") == "bytes"

    def test_admin_suffix_range(self, auth_server: dict) -> None:
        """Admin requests last 500 bytes via suffix range."""
        base = auth_server["base_url"]
        with httpx.Client(timeout=30, trust_env=False) as c:
            _write_file(c, base, "/auth-range/suffix.bin", TEST_CONTENT, AUTH_HEADERS)

            resp = c.get(
                f"{base}/api/v2/files/stream",
                params={"path": "/auth-range/suffix.bin"},
                headers={**AUTH_HEADERS, "Range": "bytes=-500"},
            )
            assert resp.status_code == 206
            assert resp.content == TEST_CONTENT[-500:]
            assert "bytes 9740-10239/10240" in resp.headers.get("content-range", "")

    def test_admin_full_download_with_accept_ranges(self, auth_server: dict) -> None:
        """Admin downloads full file — response includes Accept-Ranges."""
        base = auth_server["base_url"]
        with httpx.Client(timeout=30, trust_env=False) as c:
            _write_file(c, base, "/auth-range/full.bin", TEST_CONTENT, AUTH_HEADERS)

            resp = c.get(
                f"{base}/api/v2/files/stream",
                params={"path": "/auth-range/full.bin"},
                headers=AUTH_HEADERS,
            )
            assert resp.status_code == 200
            assert resp.content == TEST_CONTENT
            assert resp.headers.get("accept-ranges") == "bytes"

    def test_admin_unsatisfiable_returns_416(self, auth_server: dict) -> None:
        """Admin sends out-of-bounds Range — 416 with correct Content-Range."""
        base = auth_server["base_url"]
        small = b"tiny" * 10  # 40 bytes
        with httpx.Client(timeout=30, trust_env=False) as c:
            _write_file(c, base, "/auth-range/small.bin", small, AUTH_HEADERS)

            resp = c.get(
                f"{base}/api/v2/files/stream",
                params={"path": "/auth-range/small.bin"},
                headers={**AUTH_HEADERS, "Range": "bytes=100-200"},
            )
            assert resp.status_code == 416
            assert "bytes */40" in resp.headers.get("content-range", "")


class TestUnauthenticatedDenied:
    """Unauthenticated range requests are denied when permissions are enabled.

    Note: V2 endpoints fall through to anonymous OperationContext (no 401),
    so the permission system denies access with 500 "Path not found".
    The NFS RPC endpoint enforces auth at the dependency level (401).
    """

    def test_stream_without_auth_denied(self, auth_server: dict) -> None:
        """Streaming without API key should be denied by permissions."""
        base = auth_server["base_url"]
        with httpx.Client(timeout=30, trust_env=False) as c:
            # Write with admin key first
            _write_file(c, base, "/auth-range/denied.bin", b"secret data", AUTH_HEADERS)

            # Try to stream without auth — permission system denies anonymous
            resp = c.get(
                f"{base}/api/v2/files/stream",
                params={"path": "/auth-range/denied.bin"},
                headers={"Range": "bytes=0-5"},
            )
            # V2 get_context falls through to anonymous → permission denied
            assert resp.status_code in (401, 403, 404, 500)

    def test_nfs_write_without_auth_returns_401(self, auth_server: dict) -> None:
        """NFS RPC write without API key returns 401 (enforced at dependency)."""
        base = auth_server["base_url"]
        with httpx.Client(timeout=30, trust_env=False) as c:
            resp = c.post(
                f"{base}/api/nfs/write",
                json={"params": {"path": "/auth-range/nope.bin", "content": "nope"}},
            )
            assert resp.status_code == 401
