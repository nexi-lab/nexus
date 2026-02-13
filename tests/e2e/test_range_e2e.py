"""E2E tests for HTTP Range request support (Issue #790).

Tests Range requests against a real nexus server with actual file persistence.

Starts its own `nexus serve` process with NEXUS_ENFORCE_PERMISSIONS=false
so both sync NexusFS and AsyncNexusFS allow unauthenticated access.
Files are written via /api/v2/files/write and streamed via /api/v2/files/stream.

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
from pathlib import Path

import httpx
import pytest

# =============================================================================
# Server fixture (custom, with permissions disabled)
# =============================================================================

PYTHON = sys.executable
SERVER_STARTUP_TIMEOUT = 30


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


@pytest.fixture(scope="module")
def range_server():
    """Start nexus serve with permissions disabled for range request E2E tests."""
    port = _find_free_port()
    data_dir = tempfile.mkdtemp(prefix="nexus_range_e2e_")
    backend_root = os.path.join(data_dir, "backend")
    os.makedirs(backend_root, exist_ok=True)

    base_url = f"http://127.0.0.1:{port}"

    env = {
        **os.environ,
        # Clear proxies for localhost
        "HTTP_PROXY": "",
        "HTTPS_PROXY": "",
        "http_proxy": "",
        "https_proxy": "",
        "NO_PROXY": "*",
        # Source code on PYTHONPATH
        "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src"),
        # SQLite (no PostgreSQL needed for range tests)
        "NEXUS_DATABASE_URL": f"sqlite:///{data_dir}/range_e2e.db",
        # AsyncNexusFS settings
        "NEXUS_BACKEND_ROOT": backend_root,
        "NEXUS_TENANT_ID": "range-e2e",
        # Permissions disabled — no ReBAC setup needed
        "NEXUS_ENFORCE_PERMISSIONS": "false",
        "NEXUS_ENFORCE_ZONE_ISOLATION": "false",
        # Disable search daemon
        "NEXUS_SEARCH_DAEMON": "false",
        # Disable rate limiting
        "NEXUS_RATE_LIMIT_ENABLED": "false",
    }

    proc = subprocess.Popen(
        [
            PYTHON,
            "-c",
            (
                "from nexus.cli import main; "
                f"main(['serve', '--host', '127.0.0.1', '--port', '{port}', "
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
        yield {"base_url": base_url, "port": port, "data_dir": data_dir}
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


# =============================================================================
# Helpers
# =============================================================================

# 10KB test content with a repeating pattern for easy verification
TEST_CONTENT = bytes(range(256)) * 40  # 10240 bytes

# Identity headers for open-access mode
USER_HEADERS = {
    "X-Nexus-Subject": "user:range_tester",
    "X-Nexus-Zone-ID": "range-e2e",
}


def _write_file(client: httpx.Client, base_url: str, path: str, content: bytes) -> dict:
    """Upload a file via the v2 API and return write response data."""
    resp = client.post(
        f"{base_url}/api/v2/files/write",
        json={
            "path": path,
            "content": base64.b64encode(content).decode(),
            "encoding": "base64",
        },
        headers=USER_HEADERS,
    )
    assert resp.status_code == 200, f"Write failed: {resp.text}"
    return resp.json()


# =============================================================================
# Tests
# =============================================================================


class TestBasicRange:
    """Basic range request: first 5KB of a 10KB file."""

    def test_range_first_half(self, range_server: dict) -> None:
        base = range_server["base_url"]
        with httpx.Client(timeout=30, trust_env=False) as c:
            _write_file(c, base, "/range-e2e/test1.bin", TEST_CONTENT)

            resp = c.get(
                f"{base}/api/v2/files/stream",
                params={"path": "/range-e2e/test1.bin"},
                headers={**USER_HEADERS, "Range": "bytes=0-4999"},
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
            _write_file(c, base, "/range-e2e/test2.bin", TEST_CONTENT)

            resp = c.get(
                f"{base}/api/v2/files/stream",
                params={"path": "/range-e2e/test2.bin"},
                headers={**USER_HEADERS, "Range": "bytes=5000-"},
            )
            assert resp.status_code == 206
            assert resp.content == TEST_CONTENT[5000:]
            assert "bytes 5000-10239/10240" in resp.headers.get("content-range", "")


class TestSuffixRange:
    """Suffix range: last N bytes of file."""

    def test_suffix_range(self, nexus_server: dict) -> None:
        with httpx.Client(
            base_url=nexus_server["base_url"], timeout=30, trust_env=False
        ) as c:
            _write_file(c, "/range-e2e/suffix.bin", TEST_CONTENT)

            resp = c.get(
                "/api/v2/files/stream",
                params={"path": "/range-e2e/suffix.bin"},
                headers={**AUTH_HEADERS, "Range": "bytes=-500"},
            )
            assert resp.status_code == 206
            assert resp.content == TEST_CONTENT[-500:]
            assert "bytes 9740-10239/10240" in resp.headers.get("content-range", "")


class TestNoRange:
    """Without Range header, get full content with Accept-Ranges."""

    def test_full_download_has_accept_ranges(self, range_server: dict) -> None:
        base = range_server["base_url"]
        with httpx.Client(timeout=30, trust_env=False) as c:
            _write_file(c, base, "/range-e2e/test3.bin", TEST_CONTENT)

            resp = c.get(
                f"{base}/api/v2/files/stream",
                params={"path": "/range-e2e/test3.bin"},
                headers=USER_HEADERS,
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
            _write_file(c, base, "/range-e2e/small.bin", small_content)

            resp = c.get(
                f"{base}/api/v2/files/stream",
                params={"path": "/range-e2e/small.bin"},
                headers={**USER_HEADERS, "Range": "bytes=200-300"},
            )
            assert resp.status_code == 416
            assert "bytes */100" in resp.headers.get("content-range", "")


class TestUnauthenticatedDenied:
    """Unauthenticated requests are denied when auth is required."""

    def test_stream_without_auth_denied(self, nexus_server: dict) -> None:
        """Streaming without API key should be denied by permissions."""
        with httpx.Client(
            base_url=nexus_server["base_url"], timeout=30, trust_env=False
        ) as c:
            _write_file(c, "/range-e2e/denied.bin", b"secret data")

            resp = c.get(
                "/api/v2/files/stream",
                params={"path": "/range-e2e/denied.bin"},
                headers={"Range": "bytes=0-5"},
            )
            # V2 get_context falls through to anonymous → permission denied
            assert resp.status_code in (401, 403, 404, 500)
