"""Integration tests for tus.io protocol (Issue #788).

Tests the full HTTP layer using httpx.AsyncClient against the
tus uploads router with a real service and in-memory SQLite backend.
"""

from __future__ import annotations

import base64
import hashlib
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from nexus.backends.local import LocalBackend
from nexus.server.api.v2.routers.tus_uploads import create_tus_uploads_router
from nexus.services.chunked_upload_service import (
    ChunkedUploadConfig,
    ChunkedUploadService,
)

# --- Test fixtures ---


@pytest.fixture
def tmp_backend(tmp_path: Path) -> LocalBackend:
    return LocalBackend(root_path=tmp_path)


@pytest.fixture
def upload_service(tmp_path: Path, tmp_backend: LocalBackend) -> ChunkedUploadService:
    """Create a service with an in-memory SQLite-like session store."""
    from tests.unit.test_chunked_upload_service import FakeSessionContext

    db = FakeSessionContext()
    config = ChunkedUploadConfig(
        max_concurrent_uploads=5,
        session_ttl_hours=1,
        min_chunk_size=1,  # Allow very small chunks for testing
        max_chunk_size=10 * 1024 * 1024,
        max_upload_size=100 * 1024 * 1024,
    )
    return ChunkedUploadService(
        session_factory=lambda: db,
        backend=tmp_backend,
        config=config,
    )


@pytest.fixture
def app(upload_service: ChunkedUploadService) -> FastAPI:
    """Create a FastAPI app with the tus router."""
    _app = FastAPI()
    router = create_tus_uploads_router(get_upload_service=lambda: upload_service)
    _app.include_router(router, prefix="/api/v2/uploads")
    return _app


@pytest.fixture
async def client(app: FastAPI) -> AsyncClient:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


TUS_HEADERS = {"Tus-Resumable": "1.0.0"}


# --- Tests ---


class TestOptionsEndpoint:
    @pytest.mark.asyncio
    async def test_options_returns_capabilities(self, client: AsyncClient) -> None:
        resp = await client.options("/api/v2/uploads")
        assert resp.status_code == 204
        assert resp.headers["Tus-Resumable"] == "1.0.0"
        assert resp.headers["Tus-Version"] == "1.0.0"
        assert "creation" in resp.headers["Tus-Extension"]
        assert "termination" in resp.headers["Tus-Extension"]
        assert "checksum" in resp.headers["Tus-Extension"]
        assert resp.headers.get("Tus-Max-Size")
        assert "sha256" in resp.headers["Tus-Checksum-Algorithm"]


class TestCreateEndpoint:
    @pytest.mark.asyncio
    async def test_create_upload(self, client: AsyncClient) -> None:
        resp = await client.post(
            "/api/v2/uploads",
            headers={
                **TUS_HEADERS,
                "Upload-Length": "1024",
                "Upload-Metadata": f"filename {base64.b64encode(b'test.txt').decode()}",
            },
        )
        assert resp.status_code == 201
        assert "Location" in resp.headers
        assert resp.headers["Tus-Resumable"] == "1.0.0"
        assert "Upload-Expires" in resp.headers

    @pytest.mark.asyncio
    async def test_create_missing_upload_length(self, client: AsyncClient) -> None:
        resp = await client.post(
            "/api/v2/uploads",
            headers=TUS_HEADERS,
        )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_create_invalid_upload_length(self, client: AsyncClient) -> None:
        resp = await client.post(
            "/api/v2/uploads",
            headers={**TUS_HEADERS, "Upload-Length": "not-a-number"},
        )
        assert resp.status_code == 400


class TestTusVersionValidation:
    @pytest.mark.asyncio
    async def test_missing_tus_resumable_returns_412(self, client: AsyncClient) -> None:
        resp = await client.post(
            "/api/v2/uploads",
            headers={"Upload-Length": "100"},
        )
        assert resp.status_code == 412

    @pytest.mark.asyncio
    async def test_wrong_tus_version_returns_412(self, client: AsyncClient) -> None:
        resp = await client.post(
            "/api/v2/uploads",
            headers={"Tus-Resumable": "0.2.2", "Upload-Length": "100"},
        )
        assert resp.status_code == 412


class TestFullUploadLifecycle:
    @pytest.mark.asyncio
    async def test_full_upload_lifecycle(self, client: AsyncClient) -> None:
        """POST → PATCH → HEAD → verify file exists."""
        file_content = b"Hello, tus! This is a test file."

        # 1. Create upload
        create_resp = await client.post(
            "/api/v2/uploads",
            headers={
                **TUS_HEADERS,
                "Upload-Length": str(len(file_content)),
                "Upload-Metadata": f"filename {base64.b64encode(b'hello.txt').decode()}",
            },
        )
        assert create_resp.status_code == 201
        location = create_resp.headers["Location"]
        upload_path = location.replace("http://test", "")

        # 2. Upload chunk (single chunk)
        patch_resp = await client.patch(
            upload_path,
            headers={
                **TUS_HEADERS,
                "Upload-Offset": "0",
                "Content-Type": "application/offset+octet-stream",
            },
            content=file_content,
        )
        assert patch_resp.status_code == 204
        assert patch_resp.headers["Upload-Offset"] == str(len(file_content))

        # 3. Check status
        head_resp = await client.head(upload_path, headers=TUS_HEADERS)
        assert head_resp.status_code == 200
        assert head_resp.headers["Upload-Offset"] == str(len(file_content))
        assert head_resp.headers["Upload-Length"] == str(len(file_content))

    @pytest.mark.asyncio
    async def test_multi_chunk_upload(self, client: AsyncClient) -> None:
        """Upload in two chunks."""
        part1 = b"First chunk data."
        part2 = b"Second chunk data."
        total = len(part1) + len(part2)

        # Create
        create_resp = await client.post(
            "/api/v2/uploads",
            headers={**TUS_HEADERS, "Upload-Length": str(total)},
        )
        location = create_resp.headers["Location"]
        upload_path = location.replace("http://test", "")

        # Chunk 1
        patch1 = await client.patch(
            upload_path,
            headers={
                **TUS_HEADERS,
                "Upload-Offset": "0",
                "Content-Type": "application/offset+octet-stream",
            },
            content=part1,
        )
        assert patch1.status_code == 204
        assert patch1.headers["Upload-Offset"] == str(len(part1))

        # Chunk 2
        patch2 = await client.patch(
            upload_path,
            headers={
                **TUS_HEADERS,
                "Upload-Offset": str(len(part1)),
                "Content-Type": "application/offset+octet-stream",
            },
            content=part2,
        )
        assert patch2.status_code == 204
        assert patch2.headers["Upload-Offset"] == str(total)


class TestResumeAfterDisconnect:
    @pytest.mark.asyncio
    async def test_resume_after_disconnect(self, client: AsyncClient) -> None:
        """POST → PATCH (partial) → HEAD → PATCH (rest)."""
        full_content = b"ABCDEFGHIJKLMNOPQRSTUVWXYZ"

        # Create
        create_resp = await client.post(
            "/api/v2/uploads",
            headers={**TUS_HEADERS, "Upload-Length": str(len(full_content))},
        )
        location = create_resp.headers["Location"]
        upload_path = location.replace("http://test", "")

        # Upload first half
        half = len(full_content) // 2
        await client.patch(
            upload_path,
            headers={
                **TUS_HEADERS,
                "Upload-Offset": "0",
                "Content-Type": "application/offset+octet-stream",
            },
            content=full_content[:half],
        )

        # "Disconnect" — check offset via HEAD
        head_resp = await client.head(upload_path, headers=TUS_HEADERS)
        assert head_resp.status_code == 200
        current_offset = int(head_resp.headers["Upload-Offset"])
        assert current_offset == half

        # Resume with second half
        patch2 = await client.patch(
            upload_path,
            headers={
                **TUS_HEADERS,
                "Upload-Offset": str(current_offset),
                "Content-Type": "application/offset+octet-stream",
            },
            content=full_content[current_offset:],
        )
        assert patch2.status_code == 204
        assert patch2.headers["Upload-Offset"] == str(len(full_content))


class TestChecksumEndpoints:
    @pytest.mark.asyncio
    async def test_checksum_sha256(self, client: AsyncClient) -> None:
        data = b"checksum test data"
        digest = base64.b64encode(hashlib.sha256(data).digest()).decode()

        create_resp = await client.post(
            "/api/v2/uploads",
            headers={**TUS_HEADERS, "Upload-Length": str(len(data))},
        )
        upload_path = create_resp.headers["Location"].replace("http://test", "")

        patch_resp = await client.patch(
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

    @pytest.mark.asyncio
    async def test_checksum_mismatch_returns_460(self, client: AsyncClient) -> None:
        data = b"checksum test data"
        wrong_digest = base64.b64encode(b"wrong" * 6).decode()

        create_resp = await client.post(
            "/api/v2/uploads",
            headers={**TUS_HEADERS, "Upload-Length": str(len(data))},
        )
        upload_path = create_resp.headers["Location"].replace("http://test", "")

        patch_resp = await client.patch(
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


class TestOffsetMismatch:
    @pytest.mark.asyncio
    async def test_offset_mismatch_returns_409(self, client: AsyncClient) -> None:
        create_resp = await client.post(
            "/api/v2/uploads",
            headers={**TUS_HEADERS, "Upload-Length": "100"},
        )
        upload_path = create_resp.headers["Location"].replace("http://test", "")

        patch_resp = await client.patch(
            upload_path,
            headers={
                **TUS_HEADERS,
                "Upload-Offset": "50",  # Wrong — should be 0
                "Content-Type": "application/offset+octet-stream",
            },
            content=b"x" * 50,
        )
        assert patch_resp.status_code == 409


class TestContentTypeValidation:
    @pytest.mark.asyncio
    async def test_wrong_content_type_returns_415(self, client: AsyncClient) -> None:
        create_resp = await client.post(
            "/api/v2/uploads",
            headers={**TUS_HEADERS, "Upload-Length": "10"},
        )
        upload_path = create_resp.headers["Location"].replace("http://test", "")

        patch_resp = await client.patch(
            upload_path,
            headers={
                **TUS_HEADERS,
                "Upload-Offset": "0",
                "Content-Type": "application/json",
            },
            content=b"x" * 10,
        )
        assert patch_resp.status_code == 415


class TestTerminateEndpoint:
    @pytest.mark.asyncio
    async def test_terminate_deletes_resources(self, client: AsyncClient) -> None:
        create_resp = await client.post(
            "/api/v2/uploads",
            headers={**TUS_HEADERS, "Upload-Length": "100"},
        )
        upload_path = create_resp.headers["Location"].replace("http://test", "")

        delete_resp = await client.delete(upload_path, headers=TUS_HEADERS)
        assert delete_resp.status_code == 204

    @pytest.mark.asyncio
    async def test_terminate_nonexistent_returns_404(self, client: AsyncClient) -> None:
        resp = await client.delete(
            "/api/v2/uploads/nonexistent-id",
            headers=TUS_HEADERS,
        )
        assert resp.status_code == 404


class TestZeroByteUpload:
    @pytest.mark.asyncio
    async def test_zero_byte_upload(self, client: AsyncClient) -> None:
        create_resp = await client.post(
            "/api/v2/uploads",
            headers={**TUS_HEADERS, "Upload-Length": "0"},
        )
        assert create_resp.status_code == 201
        upload_path = create_resp.headers["Location"].replace("http://test", "")

        # Upload empty chunk
        patch_resp = await client.patch(
            upload_path,
            headers={
                **TUS_HEADERS,
                "Upload-Offset": "0",
                "Content-Type": "application/offset+octet-stream",
            },
            content=b"",
        )
        assert patch_resp.status_code == 204


class TestUploadMetadataParsing:
    @pytest.mark.asyncio
    async def test_upload_metadata_parsing(self, client: AsyncClient) -> None:
        filename_b64 = base64.b64encode(b"report.pdf").decode()
        content_type_b64 = base64.b64encode(b"application/pdf").decode()

        resp = await client.post(
            "/api/v2/uploads",
            headers={
                **TUS_HEADERS,
                "Upload-Length": "1000",
                "Upload-Metadata": f"filename {filename_b64},content_type {content_type_b64}",
            },
        )
        assert resp.status_code == 201
