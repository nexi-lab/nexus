"""E2E tests for HTTP Range request support (Issue #790).

Tests Range requests against a real nexus server with authentication
enabled and actual file persistence.

Uses the nexus_server fixture from conftest.py which starts
`nexus serve` as a subprocess with NEXUS_API_KEY auth.

Run with: .venv/bin/python3.12 -m pytest tests/e2e/test_range_e2e.py -v -p no:xdist -o "addopts="
"""

from __future__ import annotations

import base64

import httpx

AUTH_HEADERS = {"Authorization": "Bearer test-e2e-api-key-12345"}

# 10KB test content with a repeating pattern for easy verification
TEST_CONTENT = bytes(range(256)) * 40  # 10240 bytes


def _write_file(client: httpx.Client, path: str, content: bytes) -> dict:
    """Upload a file via the v2 API and return write response data."""
    resp = client.post(
        "/api/v2/files/write",
        json={
            "path": path,
            "content": base64.b64encode(content).decode(),
            "encoding": "base64",
        },
        headers=AUTH_HEADERS,
    )
    assert resp.status_code == 200, f"Write failed: {resp.text}"
    return resp.json()


class TestBasicRange:
    """Basic range request: first 5KB of a 10KB file."""

    def test_range_first_half(self, nexus_server: dict) -> None:
        with httpx.Client(
            base_url=nexus_server["base_url"], timeout=30, trust_env=False
        ) as c:
            _write_file(c, "/range-e2e/test1.bin", TEST_CONTENT)

            resp = c.get(
                "/api/v2/files/stream",
                params={"path": "/range-e2e/test1.bin"},
                headers={**AUTH_HEADERS, "Range": "bytes=0-4999"},
            )
            assert resp.status_code == 206
            assert resp.content == TEST_CONTENT[:5000]
            assert "bytes 0-4999/10240" in resp.headers.get("content-range", "")
            assert resp.headers.get("accept-ranges") == "bytes"


class TestResumeDownload:
    """Simulate download resumption: request bytes from offset to end."""

    def test_range_from_offset(self, nexus_server: dict) -> None:
        with httpx.Client(
            base_url=nexus_server["base_url"], timeout=30, trust_env=False
        ) as c:
            _write_file(c, "/range-e2e/test2.bin", TEST_CONTENT)

            resp = c.get(
                "/api/v2/files/stream",
                params={"path": "/range-e2e/test2.bin"},
                headers={**AUTH_HEADERS, "Range": "bytes=5000-"},
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

    def test_full_download_has_accept_ranges(self, nexus_server: dict) -> None:
        with httpx.Client(
            base_url=nexus_server["base_url"], timeout=30, trust_env=False
        ) as c:
            _write_file(c, "/range-e2e/test3.bin", TEST_CONTENT)

            resp = c.get(
                "/api/v2/files/stream",
                params={"path": "/range-e2e/test3.bin"},
                headers=AUTH_HEADERS,
            )
            assert resp.status_code == 200
            assert resp.content == TEST_CONTENT
            assert resp.headers.get("accept-ranges") == "bytes"


class TestUnsatisfiableRange:
    """Range beyond file size returns 416."""

    def test_range_past_eof(self, nexus_server: dict) -> None:
        small_content = b"X" * 100

        with httpx.Client(
            base_url=nexus_server["base_url"], timeout=30, trust_env=False
        ) as c:
            _write_file(c, "/range-e2e/small.bin", small_content)

            resp = c.get(
                "/api/v2/files/stream",
                params={"path": "/range-e2e/small.bin"},
                headers={**AUTH_HEADERS, "Range": "bytes=200-300"},
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

    def test_nfs_write_without_auth_returns_401(self, nexus_server: dict) -> None:
        """NFS RPC write without API key returns 401 (enforced at dependency)."""
        with httpx.Client(
            base_url=nexus_server["base_url"], timeout=30, trust_env=False
        ) as c:
            resp = c.post(
                "/api/nfs/write",
                json={"params": {"path": "/range-e2e/nope.bin", "content": "nope"}},
            )
            assert resp.status_code == 401
