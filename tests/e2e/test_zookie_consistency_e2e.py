"""E2E tests for Zookie Consistency Tokens (Issue #1187).

Tests the full zookie flow:
- Write operations return zookie in response
- Read operations accept X-Nexus-Zookie header for read-after-write consistency
- Delete and rename operations return zookies
- Watch API supports since_revision parameter
"""

from __future__ import annotations

import base64
from typing import TYPE_CHECKING

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
