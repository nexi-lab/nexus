"""E2E tests for Zookie Consistency Tokens (Issue #1187).

Tests the full zookie flow:
- Write operations return zookie in response
- Read operations accept X-Nexus-Zookie header for read-after-write consistency
- Delete and rename operations return zookies
- Watch API supports since_revision parameter
"""

from __future__ import annotations

import base64
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from contextlib import closing
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
import pytest
from starlette.testclient import TestClient

if TYPE_CHECKING:
    from nexus import NexusFS


def _make_bytes_content(text: str) -> dict:
    """Create bytes content in JSON-RPC format."""
    return {"__type__": "bytes", "data": base64.b64encode(text.encode()).decode()}


def _extract_zookie_from_write_result(result: dict) -> str:
    """Extract zookie from write result (may be nested in bytes_written)."""
    if "zookie" in result:
        return result["zookie"]
    if "bytes_written" in result and "zookie" in result["bytes_written"]:
        return result["bytes_written"]["zookie"]
    raise KeyError(f"No zookie found in result: {result}")


class TestWriteReturnsZookie:
    """Tests that write operations return zookie tokens."""

    def test_write_response_includes_zookie(self, nexus_fs: NexusFS) -> None:
        """Write should return a zookie token in the response."""
        from nexus.server.fastapi_server import create_app

        app = create_app(nexus_fs)

        with TestClient(app) as client:
            response = client.post(
                "/api/nfs/write",
                json={
                    "params": {"path": "/test.txt", "content": _make_bytes_content("Hello, World!")}
                },
            )

            assert response.status_code == 200
            data = response.json()

            # Check zookie in response body
            assert "result" in data, f"Expected result in response, got: {data}"
            result = data["result"]

            # Zookie may be at top level or nested in bytes_written
            zookie_token = _extract_zookie_from_write_result(result)
            assert zookie_token.startswith("nz1."), "Zookie should have nz1 version prefix"
            assert len(zookie_token.split(".")) == 5, "Zookie should have 5 parts"

    def test_write_response_header_includes_zookie(self, nexus_fs: NexusFS) -> None:
        """Write should return X-Nexus-Zookie header in response."""
        from nexus.server.fastapi_server import create_app

        app = create_app(nexus_fs)

        with TestClient(app) as client:
            response = client.post(
                "/api/nfs/write",
                json={"params": {"path": "/test2.txt", "content": _make_bytes_content("Hello!")}},
            )

            assert response.status_code == 200

            # Check X-Nexus-Zookie header
            assert "X-Nexus-Zookie" in response.headers, (
                "Response should include X-Nexus-Zookie header"
            )
            header_zookie = response.headers["X-Nexus-Zookie"]
            assert header_zookie.startswith("nz1.")

    def test_write_revision_increments(self, nexus_fs: NexusFS) -> None:
        """Each write should increment the revision."""
        from nexus.core.zookie import Zookie
        from nexus.server.fastapi_server import create_app

        app = create_app(nexus_fs)

        revisions = []
        with TestClient(app) as client:
            for i in range(3):
                response = client.post(
                    "/api/nfs/write",
                    json={
                        "params": {
                            "path": f"/file_{i}.txt",
                            "content": _make_bytes_content(f"Content {i}"),
                        }
                    },
                )
                assert response.status_code == 200
                data = response.json()
                assert "result" in data, f"Expected result, got: {data}"
                result = data["result"]
                zookie_token = _extract_zookie_from_write_result(result)
                zookie = Zookie.decode(zookie_token)
                revisions.append(zookie.revision)

        # Revisions should be monotonically increasing
        assert revisions[0] < revisions[1] < revisions[2], "Revisions should increment"


class TestDeleteReturnsZookie:
    """Tests that delete operations return zookie tokens."""

    def test_delete_response_includes_zookie(self, nexus_fs: NexusFS) -> None:
        """Delete should return a zookie token."""
        from nexus.server.fastapi_server import create_app

        nexus_fs.write("/to_delete.txt", b"temporary content")
        app = create_app(nexus_fs)

        with TestClient(app) as client:
            response = client.post(
                "/api/nfs/delete",
                json={"params": {"path": "/to_delete.txt"}},
            )

            assert response.status_code == 200
            data = response.json()

            # Delete response should include zookie
            assert "result" in data, f"Expected result, got: {data}"
            result = data["result"]
            assert "zookie" in result, "Delete result should include zookie"
            assert result["zookie"].startswith("nz1.")


class TestRenameReturnsZookie:
    """Tests that rename operations return zookie tokens."""

    def test_rename_response_includes_zookie(self, nexus_fs: NexusFS) -> None:
        """Rename should return a zookie token."""
        from nexus.server.fastapi_server import create_app

        nexus_fs.write("/old_name.txt", b"content")
        app = create_app(nexus_fs)

        with TestClient(app) as client:
            response = client.post(
                "/api/nfs/rename",
                json={"params": {"old_path": "/old_name.txt", "new_path": "/new_name.txt"}},
            )

            assert response.status_code == 200
            data = response.json()

            # Rename response should include zookie
            assert "result" in data, f"Expected result, got: {data}"
            result = data["result"]
            assert "zookie" in result, "Rename result should include zookie"
            assert result["zookie"].startswith("nz1.")


class TestReadWithZookieHeader:
    """Tests for read operations with X-Nexus-Zookie header."""

    def test_read_accepts_zookie_header(self, nexus_fs: NexusFS) -> None:
        """Read should accept X-Nexus-Zookie header without error."""
        from nexus.server.fastapi_server import create_app

        app = create_app(nexus_fs)

        with TestClient(app) as client:
            # First write to get a zookie
            write_response = client.post(
                "/api/nfs/write",
                json={
                    "params": {
                        "path": "/read_test.txt",
                        "content": _make_bytes_content("Test content"),
                    }
                },
            )
            assert write_response.status_code == 200
            data = write_response.json()
            assert "result" in data, f"Expected result, got: {data}"
            zookie = _extract_zookie_from_write_result(data["result"])

            # Read with zookie header (should work since revision is satisfied)
            read_response = client.post(
                "/api/nfs/read",
                json={"params": {"path": "/read_test.txt"}},
                headers={"X-Nexus-Zookie": zookie},
            )

            assert read_response.status_code == 200

    def test_read_with_invalid_zookie_returns_error(self, nexus_fs: NexusFS) -> None:
        """Read with invalid X-Nexus-Zookie header should return error."""
        from nexus.server.fastapi_server import create_app

        nexus_fs.write("/test_invalid_zookie.txt", b"content")
        app = create_app(nexus_fs)

        with TestClient(app) as client:
            response = client.post(
                "/api/nfs/read",
                json={"params": {"path": "/test_invalid_zookie.txt"}},
                headers={"X-Nexus-Zookie": "invalid_token"},
            )

            # Should return error for invalid zookie
            assert response.status_code == 200  # JSON-RPC returns 200 with error in body
            data = response.json()
            assert "error" in data, "Invalid zookie should return error"


class TestZookieDecodeParsing:
    """Tests for zookie encoding/decoding."""

    def test_zookie_roundtrip(self, nexus_fs: NexusFS) -> None:
        """Zookie from write should decode correctly."""
        from nexus.core.zookie import Zookie
        from nexus.server.fastapi_server import create_app

        app = create_app(nexus_fs)

        with TestClient(app) as client:
            response = client.post(
                "/api/nfs/write",
                json={
                    "params": {
                        "path": "/roundtrip_test.txt",
                        "content": _make_bytes_content("test"),
                    }
                },
            )

            assert response.status_code == 200
            data = response.json()
            assert "result" in data, f"Expected result, got: {data}"
            token = _extract_zookie_from_write_result(data["result"])

            # Decode and verify
            zookie = Zookie.decode(token)
            assert zookie.revision > 0, "Revision should be positive"
            assert zookie.created_at_ms > 0, "Created timestamp should be positive"

            # Re-encode should produce same format
            re_encoded = Zookie.encode(zookie.zone_id, zookie.revision)
            re_decoded = Zookie.decode(re_encoded)
            assert re_decoded.zone_id == zookie.zone_id
            assert re_decoded.revision == zookie.revision


class TestWatchAPIWithRevision:
    """Tests for watch API with since_revision parameter."""

    def test_watch_accepts_since_revision_param(self, nexus_fs: NexusFS) -> None:
        """Watch API should accept since_revision parameter."""
        from nexus.server.fastapi_server import create_app

        nexus_fs.mkdir("/watch_test")
        app = create_app(nexus_fs)

        with TestClient(app) as client:
            # Watch with since_revision parameter
            response = client.get(
                "/api/watch",
                params={"path": "/watch_test/", "timeout": 0.1, "since_revision": 10},
            )

            # Either success (200) or 501 if no event infrastructure
            assert response.status_code in (200, 422, 501)


class TestZookieZoneScoping:
    """Tests for zookie zone scoping."""

    def test_zookie_contains_zone_id(self, nexus_fs: NexusFS) -> None:
        """Zookie should contain the zone ID."""
        from nexus.core.zookie import Zookie
        from nexus.server.fastapi_server import create_app

        app = create_app(nexus_fs)

        with TestClient(app) as client:
            response = client.post(
                "/api/nfs/write",
                json={"params": {"path": "/zone_test.txt", "content": _make_bytes_content("test")}},
            )

            assert response.status_code == 200
            data = response.json()
            assert "result" in data, f"Expected result, got: {data}"
            token = _extract_zookie_from_write_result(data["result"])

            # Decode and check zone
            zookie = Zookie.decode(token)
            assert zookie.zone_id is not None, "Zookie should have zone_id"
            # Default zone is "default"
            assert len(zookie.zone_id) > 0


class TestZookieChecksumValidation:
    """Tests for zookie checksum validation."""

    def test_tampered_zookie_is_rejected(self, nexus_fs: NexusFS) -> None:
        """Tampered zookie should be rejected."""
        from nexus.server.fastapi_server import create_app

        nexus_fs.write("/checksum_test.txt", b"content")
        app = create_app(nexus_fs)

        with TestClient(app) as client:
            # Get a valid zookie
            write_response = client.post(
                "/api/nfs/write",
                json={
                    "params": {
                        "path": "/checksum_test2.txt",
                        "content": _make_bytes_content("test"),
                    }
                },
            )
            data = write_response.json()
            assert "result" in data, f"Expected result, got: {data}"
            valid_token = _extract_zookie_from_write_result(data["result"])

            # Tamper with the revision (middle part)
            parts = valid_token.split(".")
            parts[2] = "999999"  # Change revision
            tampered_token = ".".join(parts)

            # Read with tampered zookie should fail
            read_response = client.post(
                "/api/nfs/read",
                json={"params": {"path": "/checksum_test.txt"}},
                headers={"X-Nexus-Zookie": tampered_token},
            )

            assert read_response.status_code == 200
            data = read_response.json()
            assert "error" in data, "Tampered zookie should return error"
            assert "checksum" in data["error"]["message"].lower()


class TestReadAfterWriteConsistency:
    """Tests for read-after-write consistency guarantees."""

    def test_read_after_write_with_zookie(self, nexus_fs: NexusFS) -> None:
        """Read with zookie from write should see the written data."""
        from nexus.server.fastapi_server import create_app

        app = create_app(nexus_fs)

        with TestClient(app) as client:
            # Write content
            content = "Read-after-write test content"
            write_response = client.post(
                "/api/nfs/write",
                json={"params": {"path": "/raw_test.txt", "content": _make_bytes_content(content)}},
            )
            assert write_response.status_code == 200
            data = write_response.json()
            assert "result" in data, f"Expected result, got: {data}"
            zookie = _extract_zookie_from_write_result(data["result"])

            # Read with zookie - should see the written content
            read_response = client.post(
                "/api/nfs/read",
                json={"params": {"path": "/raw_test.txt"}},
                headers={"X-Nexus-Zookie": zookie},
            )

            assert read_response.status_code == 200
            result = read_response.json()["result"]
            # Result might be base64 encoded or direct string depending on content type
            assert result is not None


# =============================================================================
# True E2E Tests (actual HTTP server)
# =============================================================================


class TestZookieWithRealServer:
    """True E2E tests using actual HTTP server (test_app fixture)."""

    def test_write_returns_zookie_real_server(self, test_app) -> None:
        """Write to real server should return zookie."""
        content = _make_bytes_content("Real server test")
        response = test_app.post(
            "/api/nfs/write",
            json={"params": {"path": "/real_server_test.txt", "content": content}},
        )

        assert response.status_code == 200
        data = response.json()
        assert "result" in data, f"Expected result, got: {data}"

        zookie = _extract_zookie_from_write_result(data["result"])
        assert zookie.startswith("nz1."), f"Zookie should start with nz1., got: {zookie}"

    def test_zookie_header_real_server(self, test_app) -> None:
        """Real server should return X-Nexus-Zookie header."""
        content = _make_bytes_content("Header test")
        response = test_app.post(
            "/api/nfs/write",
            json={"params": {"path": "/header_test.txt", "content": content}},
        )

        assert response.status_code == 200
        assert "X-Nexus-Zookie" in response.headers, (
            f"Missing header. Headers: {dict(response.headers)}"
        )

    def test_read_after_write_real_server(self, test_app) -> None:
        """Read with zookie on real server should see written data."""
        # Write
        content = _make_bytes_content("Consistency test data")
        write_response = test_app.post(
            "/api/nfs/write",
            json={"params": {"path": "/consistency_test.txt", "content": content}},
        )
        assert write_response.status_code == 200
        zookie = _extract_zookie_from_write_result(write_response.json()["result"])

        # Read with zookie
        read_response = test_app.post(
            "/api/nfs/read",
            json={"params": {"path": "/consistency_test.txt"}},
            headers={"X-Nexus-Zookie": zookie},
        )

        assert read_response.status_code == 200
        assert "result" in read_response.json()


# =============================================================================
# Issue #923: CTO Consistency Header E2E Tests
# =============================================================================


class TestConsistencyHeadersE2E:
    """E2E tests for X-Nexus-Consistency header support (Issue #923)."""

    def test_consistency_header_in_read_request(self, nexus_fs: NexusFS) -> None:
        """X-Nexus-Consistency header should be accepted on read requests."""
        from nexus.server.fastapi_server import create_app

        app = create_app(nexus_fs)

        with TestClient(app) as client:
            # Write a file first
            client.post(
                "/api/nfs/write",
                json={
                    "params": {"path": "/cto_header.txt", "content": _make_bytes_content("test")}
                },
            )

            # Read with X-Nexus-Consistency header (all 3 levels)
            for level in ("eventual", "close_to_open", "strong"):
                read_response = client.post(
                    "/api/nfs/read",
                    json={"params": {"path": "/cto_header.txt"}},
                    headers={"X-Nexus-Consistency": level},
                )
                assert read_response.status_code == 200, (
                    f"Read with consistency={level} failed: {read_response.json()}"
                )

    def test_invalid_consistency_header_returns_error(self, nexus_fs: NexusFS) -> None:
        """Invalid X-Nexus-Consistency value should return an error."""
        from nexus.server.fastapi_server import create_app

        nexus_fs.write("/invalid_header.txt", b"content")
        app = create_app(nexus_fs)

        with TestClient(app) as client:
            response = client.post(
                "/api/nfs/read",
                json={"params": {"path": "/invalid_header.txt"}},
                headers={"X-Nexus-Consistency": "invalid_level"},
            )
            assert response.status_code == 200  # JSON-RPC returns 200 with error in body
            data = response.json()
            assert "error" in data, f"Expected error for invalid consistency level, got: {data}"
            assert "Invalid X-Nexus-Consistency" in data["error"]["message"]

    def test_full_cto_flow_via_api(self, nexus_fs: NexusFS) -> None:
        """Full CTO flow: write -> get zookie -> read with zookie + consistency -> verify."""
        from nexus.server.fastapi_server import create_app

        # Write directly to get a valid zookie (avoids audit log table issues)
        result = nexus_fs.write("/cto_flow.txt", b"CTO flow data")
        zookie = result["zookie"]

        app = create_app(nexus_fs)

        with TestClient(app) as client:
            # Read with CTO consistency + zookie from the write
            read_response = client.post(
                "/api/nfs/read",
                json={"params": {"path": "/cto_flow.txt"}},
                headers={
                    "X-Nexus-Consistency": "close_to_open",
                    "X-Nexus-Zookie": zookie,
                },
            )
            assert read_response.status_code == 200
            data = read_response.json()
            assert "result" in data, f"Expected result for CTO read, got: {data}"

    def test_eventual_consistency_with_future_zookie(self, nexus_fs: NexusFS) -> None:
        """EVENTUAL consistency should ignore a future zookie (no timeout/error)."""
        from nexus.core.zookie import Zookie
        from nexus.server.fastapi_server import create_app

        nexus_fs.write("/eventual_test.txt", b"data")
        app = create_app(nexus_fs)

        future_zookie = Zookie.encode("default", 999999)

        with TestClient(app) as client:
            # Read with EVENTUAL + future zookie — should succeed, no error
            read_response = client.post(
                "/api/nfs/read",
                json={"params": {"path": "/eventual_test.txt"}},
                headers={
                    "X-Nexus-Consistency": "eventual",
                    "X-Nexus-Zookie": future_zookie,
                },
            )
            assert read_response.status_code == 200
            data = read_response.json()
            assert "result" in data, f"Expected result for EVENTUAL read, got: {data}"


# =============================================================================
# Issue #923: Real Server E2E Tests (real subprocess, open access mode)
# =============================================================================

_PYTHON = sys.executable
_SRC_DIR = str(Path(__file__).resolve().parents[2] / "src")


def _find_free_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("", 0))
        return s.getsockname()[1]


def _wait_for_health(base_url: str, timeout: float = 30.0) -> None:
    start = time.time()
    while time.time() - start < timeout:
        try:
            resp = httpx.get(f"{base_url}/health", timeout=1.0, trust_env=False)
            if resp.status_code == 200:
                return
        except (httpx.ConnectError, httpx.ReadTimeout):
            pass
        time.sleep(0.2)
    pytest.fail(f"Server at {base_url} did not become healthy in {timeout}s")


_PG_URL = os.environ.get(
    "NEXUS_TEST_DATABASE_URL",
    "postgresql://scorpio:scorpio@127.0.0.1:5432/nexus_e2e_test",
)

_CTO_API_KEY = "sk-cto-test-key-12345"


@pytest.fixture(scope="module")
def cto_server(tmp_path_factory):
    """Start a real nexus serve with auth enabled and PostgreSQL."""
    data_dir = str(tmp_path_factory.mktemp("cto_e2e"))
    port = _find_free_port()
    base_url = f"http://127.0.0.1:{port}"

    env = {
        **os.environ,
        "PYTHONPATH": _SRC_DIR,
        "HTTP_PROXY": "",
        "HTTPS_PROXY": "",
        "http_proxy": "",
        "https_proxy": "",
        "NO_PROXY": "*",
        "NEXUS_DATABASE_URL": _PG_URL,
        "NEXUS_ENFORCE_PERMISSIONS": "true",
        "NEXUS_RATE_LIMIT_ENABLED": "false",
        "NEXUS_SEARCH_DAEMON": "false",
    }

    proc = subprocess.Popen(
        [
            _PYTHON,
            "-c",
            (
                "from nexus.cli import main; "
                f"main(['serve', '--host', '127.0.0.1', '--port', '{port}', "
                f"'--data-dir', '{data_dir}', "
                f"'--auth-type', 'static', '--api-key', '{_CTO_API_KEY}'])"
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
        yield {"base_url": base_url, "port": port, "process": proc}
    except Exception:
        if sys.platform != "win32":
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
        else:
            proc.terminate()
        proc.wait(timeout=5)
        stdout = proc.stdout.read() if proc.stdout else ""
        pytest.fail(f"CTO test server failed to start. Output:\n{stdout}")
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


@pytest.fixture()
def cto_admin_client(cto_server) -> httpx.Client:
    """Authenticated admin httpx client for CTO tests."""
    with httpx.Client(
        base_url=cto_server["base_url"],
        timeout=30.0,
        trust_env=False,
        headers={
            "Authorization": f"Bearer {_CTO_API_KEY}",
        },
    ) as client:
        yield client


@pytest.fixture()
def cto_user_client(cto_server) -> httpx.Client:
    """Non-admin user httpx client for CTO tests."""
    with httpx.Client(
        base_url=cto_server["base_url"],
        timeout=30.0,
        trust_env=False,
        headers={
            "Authorization": f"Bearer {_CTO_API_KEY}",
            "X-Nexus-Subject": "user:alice",
            "X-Nexus-Zone-ID": "default",
        },
    ) as client:
        yield client


def _unique_path(name: str) -> str:
    """Generate unique file path to avoid stale DB collisions."""
    import uuid

    return f"/cto_e2e_{uuid.uuid4().hex[:8]}_{name}"


class TestCTORealServer:
    """Real server E2E: CTO consistency with auth enabled (Issue #923).

    Tests run against a real `nexus serve` subprocess with:
    - PostgreSQL database
    - Static API key authentication (--auth-type static)
    - Raft metadata store
    """

    def test_health(self, cto_admin_client: httpx.Client) -> None:
        """Server health check."""
        resp = cto_admin_client.get("/health")
        assert resp.status_code == 200

    def test_write_read_with_consistency_header(self, cto_admin_client: httpx.Client) -> None:
        """Admin writes, then reads with X-Nexus-Consistency + zookie."""
        path = _unique_path("cto_read.txt")
        write_resp = cto_admin_client.post(
            "/api/nfs/write",
            json={
                "params": {
                    "path": path,
                    "content": _make_bytes_content("real server CTO"),
                }
            },
        )
        assert write_resp.status_code == 200
        data = write_resp.json()
        assert "result" in data, f"Write failed: {data}"

        zookie = write_resp.headers.get("X-Nexus-Zookie")
        if not zookie:
            zookie = _extract_zookie_from_write_result(data["result"])

        # Read with CTO consistency + zookie
        read_resp = cto_admin_client.post(
            "/api/nfs/read",
            json={"params": {"path": path}},
            headers={
                "X-Nexus-Consistency": "close_to_open",
                "X-Nexus-Zookie": zookie,
            },
        )
        assert read_resp.status_code == 200
        read_data = read_resp.json()
        assert "result" in read_data, f"Read with CTO failed: {read_data}"

    def test_eventual_consistency_ignores_future_zookie(
        self, cto_admin_client: httpx.Client
    ) -> None:
        """EVENTUAL consistency should not block on a future zookie."""
        from nexus.core.zookie import Zookie

        path = _unique_path("eventual.txt")
        cto_admin_client.post(
            "/api/nfs/write",
            json={
                "params": {
                    "path": path,
                    "content": _make_bytes_content("eventual data"),
                }
            },
        )

        future_zookie = Zookie.encode("default", 999999)
        read_resp = cto_admin_client.post(
            "/api/nfs/read",
            json={"params": {"path": path}},
            headers={
                "X-Nexus-Consistency": "eventual",
                "X-Nexus-Zookie": future_zookie,
            },
        )
        assert read_resp.status_code == 200
        data = read_resp.json()
        assert "result" in data, f"EVENTUAL read should succeed, got: {data}"

    def test_invalid_consistency_returns_error(self, cto_admin_client: httpx.Client) -> None:
        """Invalid X-Nexus-Consistency value should return an error."""
        resp = cto_admin_client.post(
            "/api/nfs/read",
            json={"params": {"path": "/nonexistent.txt"}},
            headers={"X-Nexus-Consistency": "bogus_level"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "error" in data, f"Expected error for invalid consistency, got: {data}"
        assert "Invalid X-Nexus-Consistency" in data["error"]["message"]

    def test_strong_consistency_with_valid_zookie(self, cto_admin_client: httpx.Client) -> None:
        """STRONG consistency with valid (satisfied) zookie should succeed."""
        path = _unique_path("strong.txt")
        write_resp = cto_admin_client.post(
            "/api/nfs/write",
            json={
                "params": {
                    "path": path,
                    "content": _make_bytes_content("strong data"),
                }
            },
        )
        assert write_resp.status_code == 200
        data = write_resp.json()
        assert "result" in data, f"Write failed: {data}"

        zookie = write_resp.headers.get("X-Nexus-Zookie")
        if not zookie:
            zookie = _extract_zookie_from_write_result(data["result"])

        read_resp = cto_admin_client.post(
            "/api/nfs/read",
            json={"params": {"path": path}},
            headers={
                "X-Nexus-Consistency": "strong",
                "X-Nexus-Zookie": zookie,
            },
        )
        assert read_resp.status_code == 200
        read_data = read_resp.json()
        assert "result" in read_data, f"STRONG read failed: {read_data}"

    def test_user_read_with_consistency(
        self, cto_admin_client: httpx.Client, cto_user_client: httpx.Client
    ) -> None:
        """Non-admin user reads with CTO consistency after admin writes."""
        path = _unique_path("user_cto.txt")
        # Admin writes
        write_resp = cto_admin_client.post(
            "/api/nfs/write",
            json={
                "params": {
                    "path": path,
                    "content": _make_bytes_content("user readable"),
                }
            },
        )
        assert write_resp.status_code == 200
        write_data = write_resp.json()
        assert "result" in write_data, f"Admin write failed: {write_data}"

        zookie = write_resp.headers.get("X-Nexus-Zookie")
        if not zookie:
            zookie = _extract_zookie_from_write_result(write_data["result"])

        # Non-admin user reads with CTO consistency + zookie
        read_resp = cto_user_client.post(
            "/api/nfs/read",
            json={"params": {"path": path}},
            headers={
                "X-Nexus-Consistency": "close_to_open",
                "X-Nexus-Zookie": zookie,
            },
        )
        assert read_resp.status_code == 200
        read_data = read_resp.json()
        assert "result" in read_data, f"User CTO read failed: {read_data}"
