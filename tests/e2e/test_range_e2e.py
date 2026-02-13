"""E2E tests for HTTP Range request support (Issue #790).

Tests Range requests against a real nexus server with actual file persistence.
Server started with --api-key (sk- prefix) which auto-enables permissions.
Admin API key bypasses permission checks, validating that Range requests work
through the full auth + permission pipeline.

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
SERVER_STARTUP_TIMEOUT = 90
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
    client: httpx.Client,
    base_url: str,
    path: str,
    content: bytes,
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
# Server fixture: auth-enabled with --api-key (permissions auto-enabled)
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
    env = {
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

    cli_args = [
        "serve",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--data-dir",
        data_dir,
        "--api-key",
        API_KEY,
    ]
    args_str = ", ".join(f"'{a}'" for a in cli_args)
    proc = subprocess.Popen(
        [PYTHON, "-c", f"from nexus.cli import main; main([{args_str}])"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        preexec_fn=os.setsid if sys.platform != "win32" else None,
    )

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
# Range request tests (auth + permissions enabled)
# =============================================================================


class TestRangeFirstHalf:
    """Basic range request: first 5KB of a 10KB file."""

    def test_range_first_half(self, auth_server: dict) -> None:
        base = auth_server["base_url"]
        with httpx.Client(timeout=30, trust_env=False) as c:
            _write_file(c, base, "/range-e2e/test1.bin", TEST_CONTENT, AUTH_HEADERS)

            resp = c.get(
                f"{base}/api/v2/files/stream",
                params={"path": "/range-e2e/test1.bin"},
                headers={**AUTH_HEADERS, "Range": "bytes=0-4999"},
            )
            assert resp.status_code == 206
            assert resp.content == TEST_CONTENT[:5000]
            assert "bytes 0-4999/10240" in resp.headers.get("content-range", "")
            assert resp.headers.get("accept-ranges") == "bytes"


class TestResumeDownload:
    """Simulate download resumption: request bytes from offset to end."""

    def test_range_from_offset(self, auth_server: dict) -> None:
        base = auth_server["base_url"]
        with httpx.Client(timeout=30, trust_env=False) as c:
            _write_file(c, base, "/range-e2e/test2.bin", TEST_CONTENT, AUTH_HEADERS)

            resp = c.get(
                f"{base}/api/v2/files/stream",
                params={"path": "/range-e2e/test2.bin"},
                headers={**AUTH_HEADERS, "Range": "bytes=5000-"},
            )
            assert resp.status_code == 206
            assert resp.content == TEST_CONTENT[5000:]
            assert "bytes 5000-10239/10240" in resp.headers.get("content-range", "")


class TestSuffixRange:
    """Suffix range: last N bytes of file."""

    def test_suffix_range(self, auth_server: dict) -> None:
        base = auth_server["base_url"]
        with httpx.Client(timeout=30, trust_env=False) as c:
            _write_file(c, base, "/range-e2e/suffix.bin", TEST_CONTENT, AUTH_HEADERS)

            resp = c.get(
                f"{base}/api/v2/files/stream",
                params={"path": "/range-e2e/suffix.bin"},
                headers={**AUTH_HEADERS, "Range": "bytes=-500"},
            )
            assert resp.status_code == 206
            assert resp.content == TEST_CONTENT[-500:]
            assert "bytes 9740-10239/10240" in resp.headers.get("content-range", "")


class TestNoRange:
    """Without Range header, get full content with Accept-Ranges."""

    def test_full_download_has_accept_ranges(self, auth_server: dict) -> None:
        base = auth_server["base_url"]
        with httpx.Client(timeout=30, trust_env=False) as c:
            _write_file(c, base, "/range-e2e/full.bin", TEST_CONTENT, AUTH_HEADERS)

            resp = c.get(
                f"{base}/api/v2/files/stream",
                params={"path": "/range-e2e/full.bin"},
                headers=AUTH_HEADERS,
            )
            assert resp.status_code == 200
            assert resp.content == TEST_CONTENT
            assert resp.headers.get("accept-ranges") == "bytes"


class TestUnsatisfiableRange:
    """Range beyond file size returns 416."""

    def test_range_past_eof(self, auth_server: dict) -> None:
        small_content = b"X" * 100
        base = auth_server["base_url"]

        with httpx.Client(timeout=30, trust_env=False) as c:
            _write_file(c, base, "/range-e2e/small.bin", small_content, AUTH_HEADERS)

            resp = c.get(
                f"{base}/api/v2/files/stream",
                params={"path": "/range-e2e/small.bin"},
                headers={**AUTH_HEADERS, "Range": "bytes=200-300"},
            )
            assert resp.status_code == 416
            assert "bytes */100" in resp.headers.get("content-range", "")


class TestUnauthenticatedDenied:
    """Unauthenticated requests are denied when auth is required."""

    def test_stream_without_auth_denied(self, auth_server: dict) -> None:
        """Streaming without API key should be denied by permissions."""
        base = auth_server["base_url"]
        with httpx.Client(timeout=30, trust_env=False) as c:
            _write_file(c, base, "/range-e2e/denied.bin", b"secret data", AUTH_HEADERS)

            resp = c.get(
                f"{base}/api/v2/files/stream",
                params={"path": "/range-e2e/denied.bin"},
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
                json={"params": {"path": "/range-e2e/nope.bin", "content": "nope"}},
            )
            assert resp.status_code == 401
