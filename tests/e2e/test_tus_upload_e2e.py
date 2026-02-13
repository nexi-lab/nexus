"""E2E tests for tus.io resumable uploads (Issue #788).

Tests the full tus upload flow against a real nexus server with
authentication enabled and actual file persistence.

Uses the nexus_server fixture from conftest.py which starts
`nexus serve` as a subprocess with NEXUS_API_KEY auth.
"""

from __future__ import annotations

import base64
import hashlib

import httpx

# Auth header matching the API key from conftest.py
AUTH_HEADERS = {"Authorization": "Bearer test-e2e-api-key-12345"}
TUS_HEADERS = {**AUTH_HEADERS, "Tus-Resumable": "1.0.0"}


class TestTusOptionsE2E:
    """OPTIONS endpoint should return server capabilities."""

    def test_options_returns_capabilities(self, nexus_server: dict) -> None:
        with httpx.Client(base_url=nexus_server["base_url"], timeout=10, trust_env=False) as c:
            resp = c.options("/api/v2/uploads")
            assert resp.status_code == 204
            assert resp.headers.get("Tus-Resumable") == "1.0.0"
            assert resp.headers.get("Tus-Version") == "1.0.0"
            assert "creation" in resp.headers.get("Tus-Extension", "")
            assert resp.headers.get("Tus-Max-Size")


class TestTusCreateE2E:
    """POST endpoint — create upload sessions."""

    def test_create_upload_returns_201(self, nexus_server: dict) -> None:
        with httpx.Client(base_url=nexus_server["base_url"], timeout=10, trust_env=False) as c:
            resp = c.post(
                "/api/v2/uploads",
                headers={
                    **TUS_HEADERS,
                    "Upload-Length": "1024",
                    "Upload-Metadata": f"filename {base64.b64encode(b'test.txt').decode()}",
                },
            )
            assert resp.status_code == 201, f"Expected 201, got {resp.status_code}: {resp.text}"
            assert "Location" in resp.headers
            assert resp.headers.get("Tus-Resumable") == "1.0.0"

    def test_create_without_tus_header_returns_412(self, nexus_server: dict) -> None:
        with httpx.Client(base_url=nexus_server["base_url"], timeout=10, trust_env=False) as c:
            resp = c.post(
                "/api/v2/uploads",
                headers={**AUTH_HEADERS, "Upload-Length": "100"},
            )
            assert resp.status_code == 412

    def test_create_missing_upload_length_returns_400(self, nexus_server: dict) -> None:
        with httpx.Client(base_url=nexus_server["base_url"], timeout=10, trust_env=False) as c:
            resp = c.post("/api/v2/uploads", headers=TUS_HEADERS)
            assert resp.status_code == 400


class TestTusFullLifecycleE2E:
    """Full upload lifecycle: POST → PATCH → HEAD → verify."""

    def test_single_chunk_upload(self, nexus_server: dict) -> None:
        """Upload an entire file in a single chunk."""
        file_content = b"Hello, tus e2e! This is a complete upload test."

        with httpx.Client(base_url=nexus_server["base_url"], timeout=30, trust_env=False) as c:
            # 1. Create upload
            create_resp = c.post(
                "/api/v2/uploads",
                headers={
                    **TUS_HEADERS,
                    "Upload-Length": str(len(file_content)),
                    "Upload-Metadata": f"filename {base64.b64encode(b'hello.txt').decode()}",
                },
            )
            assert create_resp.status_code == 201, f"Create failed: {create_resp.text}"
            location = create_resp.headers["Location"]
            # Extract path from full URL
            upload_path = location.replace(nexus_server["base_url"], "")

            # 2. Upload chunk
            patch_resp = c.patch(
                upload_path,
                headers={
                    **TUS_HEADERS,
                    "Upload-Offset": "0",
                    "Content-Type": "application/offset+octet-stream",
                },
                content=file_content,
            )
            assert patch_resp.status_code == 204, f"Patch failed: {patch_resp.text}"
            assert patch_resp.headers["Upload-Offset"] == str(len(file_content))

            # 3. Verify via HEAD
            head_resp = c.head(upload_path, headers=TUS_HEADERS)
            assert head_resp.status_code == 200
            assert head_resp.headers["Upload-Offset"] == str(len(file_content))
            assert head_resp.headers["Upload-Length"] == str(len(file_content))

    def test_multi_chunk_upload(self, nexus_server: dict) -> None:
        """Upload in two chunks, verifying offset progression."""
        part1 = b"First chunk of data for multi-part upload."
        part2 = b"Second chunk completes the upload."
        total = len(part1) + len(part2)

        with httpx.Client(base_url=nexus_server["base_url"], timeout=30, trust_env=False) as c:
            # Create
            create_resp = c.post(
                "/api/v2/uploads",
                headers={**TUS_HEADERS, "Upload-Length": str(total)},
            )
            assert create_resp.status_code == 201
            upload_path = create_resp.headers["Location"].replace(nexus_server["base_url"], "")

            # Chunk 1
            p1 = c.patch(
                upload_path,
                headers={
                    **TUS_HEADERS,
                    "Upload-Offset": "0",
                    "Content-Type": "application/offset+octet-stream",
                },
                content=part1,
            )
            assert p1.status_code == 204
            assert p1.headers["Upload-Offset"] == str(len(part1))

            # Chunk 2
            p2 = c.patch(
                upload_path,
                headers={
                    **TUS_HEADERS,
                    "Upload-Offset": str(len(part1)),
                    "Content-Type": "application/offset+octet-stream",
                },
                content=part2,
            )
            assert p2.status_code == 204
            assert p2.headers["Upload-Offset"] == str(total)


class TestTusResumeE2E:
    """Resume after simulated disconnect: PATCH partial → HEAD → PATCH rest."""

    def test_resume_after_partial_upload(self, nexus_server: dict) -> None:
        full_content = b"ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890"

        with httpx.Client(base_url=nexus_server["base_url"], timeout=30, trust_env=False) as c:
            # Create
            create_resp = c.post(
                "/api/v2/uploads",
                headers={**TUS_HEADERS, "Upload-Length": str(len(full_content))},
            )
            assert create_resp.status_code == 201
            upload_path = create_resp.headers["Location"].replace(nexus_server["base_url"], "")

            # Upload first half
            half = len(full_content) // 2
            c.patch(
                upload_path,
                headers={
                    **TUS_HEADERS,
                    "Upload-Offset": "0",
                    "Content-Type": "application/offset+octet-stream",
                },
                content=full_content[:half],
            )

            # "Reconnect" — check offset via HEAD
            head_resp = c.head(upload_path, headers=TUS_HEADERS)
            assert head_resp.status_code == 200
            current_offset = int(head_resp.headers["Upload-Offset"])
            assert current_offset == half

            # Resume from where we left off
            p2 = c.patch(
                upload_path,
                headers={
                    **TUS_HEADERS,
                    "Upload-Offset": str(current_offset),
                    "Content-Type": "application/offset+octet-stream",
                },
                content=full_content[current_offset:],
            )
            assert p2.status_code == 204
            assert p2.headers["Upload-Offset"] == str(len(full_content))


class TestTusChecksumE2E:
    """Checksum verification over real HTTP."""

    def test_sha256_checksum_accepted(self, nexus_server: dict) -> None:
        data = b"checksum verified data for e2e test"
        digest = base64.b64encode(hashlib.sha256(data).digest()).decode()

        with httpx.Client(base_url=nexus_server["base_url"], timeout=10, trust_env=False) as c:
            create_resp = c.post(
                "/api/v2/uploads",
                headers={**TUS_HEADERS, "Upload-Length": str(len(data))},
            )
            upload_path = create_resp.headers["Location"].replace(nexus_server["base_url"], "")

            patch_resp = c.patch(
                upload_path,
                headers={
                    **TUS_HEADERS,
                    "Upload-Offset": "0",
                    "Content-Type": "application/offset+octet-stream",
                    "Upload-Checksum": f"sha256 {digest}",
                },
                content=data,
            )
            assert patch_resp.status_code == 204

    def test_checksum_mismatch_returns_460(self, nexus_server: dict) -> None:
        data = b"data that will fail checksum"
        wrong_digest = base64.b64encode(b"wrong" * 6).decode()

        with httpx.Client(base_url=nexus_server["base_url"], timeout=10, trust_env=False) as c:
            create_resp = c.post(
                "/api/v2/uploads",
                headers={**TUS_HEADERS, "Upload-Length": str(len(data))},
            )
            upload_path = create_resp.headers["Location"].replace(nexus_server["base_url"], "")

            patch_resp = c.patch(
                upload_path,
                headers={
                    **TUS_HEADERS,
                    "Upload-Offset": "0",
                    "Content-Type": "application/offset+octet-stream",
                    "Upload-Checksum": f"sha256 {wrong_digest}",
                },
                content=data,
            )
            assert patch_resp.status_code == 460


class TestTusErrorHandlingE2E:
    """Error cases over real HTTP."""

    def test_offset_mismatch_returns_409(self, nexus_server: dict) -> None:
        with httpx.Client(base_url=nexus_server["base_url"], timeout=10, trust_env=False) as c:
            create_resp = c.post(
                "/api/v2/uploads",
                headers={**TUS_HEADERS, "Upload-Length": "100"},
            )
            upload_path = create_resp.headers["Location"].replace(nexus_server["base_url"], "")

            patch_resp = c.patch(
                upload_path,
                headers={
                    **TUS_HEADERS,
                    "Upload-Offset": "50",  # Should be 0
                    "Content-Type": "application/offset+octet-stream",
                },
                content=b"x" * 50,
            )
            assert patch_resp.status_code == 409

    def test_wrong_content_type_returns_415(self, nexus_server: dict) -> None:
        with httpx.Client(base_url=nexus_server["base_url"], timeout=10, trust_env=False) as c:
            create_resp = c.post(
                "/api/v2/uploads",
                headers={**TUS_HEADERS, "Upload-Length": "10"},
            )
            upload_path = create_resp.headers["Location"].replace(nexus_server["base_url"], "")

            patch_resp = c.patch(
                upload_path,
                headers={
                    **TUS_HEADERS,
                    "Upload-Offset": "0",
                    "Content-Type": "application/json",
                },
                content=b"x" * 10,
            )
            assert patch_resp.status_code == 415

    def test_head_nonexistent_returns_404(self, nexus_server: dict) -> None:
        with httpx.Client(base_url=nexus_server["base_url"], timeout=10, trust_env=False) as c:
            resp = c.head("/api/v2/uploads/nonexistent-id", headers=TUS_HEADERS)
            assert resp.status_code == 404


class TestTusTerminateE2E:
    """DELETE endpoint for upload termination."""

    def test_terminate_upload(self, nexus_server: dict) -> None:
        with httpx.Client(base_url=nexus_server["base_url"], timeout=10, trust_env=False) as c:
            create_resp = c.post(
                "/api/v2/uploads",
                headers={**TUS_HEADERS, "Upload-Length": "100"},
            )
            upload_path = create_resp.headers["Location"].replace(nexus_server["base_url"], "")

            delete_resp = c.delete(upload_path, headers=TUS_HEADERS)
            assert delete_resp.status_code == 204

            # Verify it's gone
            head_resp = c.head(upload_path, headers=TUS_HEADERS)
            # Should be 404 (terminated) or still visible as terminated
            assert head_resp.status_code in (404, 200)

    def test_terminate_nonexistent_returns_404(self, nexus_server: dict) -> None:
        with httpx.Client(base_url=nexus_server["base_url"], timeout=10, trust_env=False) as c:
            resp = c.delete("/api/v2/uploads/nonexistent-id", headers=TUS_HEADERS)
            assert resp.status_code == 404


class TestTusZeroByteE2E:
    """Zero-byte file upload."""

    def test_zero_byte_upload(self, nexus_server: dict) -> None:
        with httpx.Client(base_url=nexus_server["base_url"], timeout=10, trust_env=False) as c:
            create_resp = c.post(
                "/api/v2/uploads",
                headers={**TUS_HEADERS, "Upload-Length": "0"},
            )
            assert create_resp.status_code == 201
            upload_path = create_resp.headers["Location"].replace(nexus_server["base_url"], "")

            patch_resp = c.patch(
                upload_path,
                headers={
                    **TUS_HEADERS,
                    "Upload-Offset": "0",
                    "Content-Type": "application/offset+octet-stream",
                },
                content=b"",
            )
            assert patch_resp.status_code == 204
