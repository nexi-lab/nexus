"""Unit tests for async FastAPI router (Phase 4).

Tests async API endpoints that use AsyncNexusFS for file operations.
Uses httpx AsyncClient for testing FastAPI endpoints.
"""

from collections.abc import AsyncGenerator
from pathlib import Path

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

# Import will fail until we implement the router
from nexus.server.api.v2.routers.async_files import create_async_files_router

# === Fixtures ===


@pytest_asyncio.fixture
async def engine() -> AsyncGenerator[AsyncEngine, None]:
    """Create async engine using SQLite in-memory for isolated tests."""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        echo=False,
    )

    # Create tables from models (fresh schema)
    from nexus.storage.models import (
        DirectoryEntryModel,
        FilePathModel,
        VersionHistoryModel,
    )

    async with engine.begin() as conn:
        tables = [
            FilePathModel.__table__,
            DirectoryEntryModel.__table__,
            VersionHistoryModel.__table__,
        ]
        for table in tables:
            await conn.run_sync(lambda sync_conn, t=table: t.create(sync_conn, checkfirst=True))

    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def app(tmp_path: Path, engine: AsyncEngine) -> AsyncGenerator[FastAPI, None]:
    """Create FastAPI app with async files router."""
    from nexus.core.async_nexus_fs import AsyncNexusFS

    # Create AsyncNexusFS
    async_fs = AsyncNexusFS(
        backend_root=tmp_path / "backend",
        engine=engine,
        tenant_id="test-tenant",
    )
    await async_fs.initialize()

    # Create FastAPI app with async router
    app = FastAPI()
    router = create_async_files_router(async_fs)
    app.include_router(router, prefix="/api/v2/files")

    yield app

    await async_fs.close()


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncGenerator[AsyncClient, None]:
    """Create async HTTP client for testing."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        yield client


# === Write Endpoint Tests ===


@pytest.mark.asyncio
async def test_write_file(client: AsyncClient) -> None:
    """Test writing a file via API."""
    response = await client.post(
        "/api/v2/files/write",
        json={
            "path": "/test/hello.txt",
            "content": "Hello, World!",
        },
    )

    assert response.status_code == 200
    data = response.json()
    assert "etag" in data
    assert "version" in data
    assert data["size"] == len("Hello, World!")


@pytest.mark.asyncio
async def test_write_binary_content(client: AsyncClient) -> None:
    """Test writing binary content (base64 encoded)."""
    import base64

    binary_content = bytes(range(256))
    encoded = base64.b64encode(binary_content).decode("ascii")

    response = await client.post(
        "/api/v2/files/write",
        json={
            "path": "/test/binary.bin",
            "content": encoded,
            "encoding": "base64",
        },
    )

    assert response.status_code == 200
    data = response.json()
    assert data["size"] == 256


@pytest.mark.asyncio
async def test_write_with_if_match_success(client: AsyncClient) -> None:
    """Test optimistic concurrency control with matching etag."""
    # Write initial content
    response1 = await client.post(
        "/api/v2/files/write",
        json={"path": "/test/occ.txt", "content": "Version 1"},
    )
    assert response1.status_code == 200
    etag = response1.json()["etag"]

    # Write with matching etag
    response2 = await client.post(
        "/api/v2/files/write",
        json={
            "path": "/test/occ.txt",
            "content": "Version 2",
            "if_match": etag,
        },
    )
    assert response2.status_code == 200
    assert response2.json()["version"] == 2


@pytest.mark.asyncio
async def test_write_with_if_match_conflict(client: AsyncClient) -> None:
    """Test optimistic concurrency control with mismatched etag."""
    # Write initial content
    await client.post(
        "/api/v2/files/write",
        json={"path": "/test/occ_conflict.txt", "content": "Version 1"},
    )

    # Write with wrong etag
    response = await client.post(
        "/api/v2/files/write",
        json={
            "path": "/test/occ_conflict.txt",
            "content": "Version 2",
            "if_match": "wrong-etag",
        },
    )
    assert response.status_code == 409  # Conflict


@pytest.mark.asyncio
async def test_write_with_if_none_match(client: AsyncClient) -> None:
    """Test create-only mode."""
    # First write should succeed
    response1 = await client.post(
        "/api/v2/files/write",
        json={
            "path": "/test/create_only.txt",
            "content": "New file",
            "if_none_match": True,
        },
    )
    assert response1.status_code == 200

    # Second write should fail
    response2 = await client.post(
        "/api/v2/files/write",
        json={
            "path": "/test/create_only.txt",
            "content": "Should fail",
            "if_none_match": True,
        },
    )
    assert response2.status_code == 409  # Conflict (file exists)


# === Read Endpoint Tests ===


@pytest.mark.asyncio
async def test_read_file(client: AsyncClient) -> None:
    """Test reading a file via API."""
    # Write a file first
    await client.post(
        "/api/v2/files/write",
        json={"path": "/test/read.txt", "content": "Read me!"},
    )

    # Read it back
    response = await client.get("/api/v2/files/read", params={"path": "/test/read.txt"})

    assert response.status_code == 200
    data = response.json()
    assert data["content"] == "Read me!"


@pytest.mark.asyncio
async def test_read_file_with_metadata(client: AsyncClient) -> None:
    """Test reading file with metadata."""
    await client.post(
        "/api/v2/files/write",
        json={"path": "/test/meta.txt", "content": "With metadata"},
    )

    response = await client.get(
        "/api/v2/files/read",
        params={"path": "/test/meta.txt", "include_metadata": "true"},
    )

    assert response.status_code == 200
    data = response.json()
    assert "content" in data
    assert "etag" in data
    assert "version" in data
    assert "modified_at" in data


@pytest.mark.asyncio
async def test_read_nonexistent_file(client: AsyncClient) -> None:
    """Test reading non-existent file returns 404."""
    response = await client.get(
        "/api/v2/files/read",
        params={"path": "/does/not/exist.txt"},
    )

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_read_with_etag_caching(client: AsyncClient) -> None:
    """Test ETag-based caching (304 Not Modified)."""
    # Write a file
    write_response = await client.post(
        "/api/v2/files/write",
        json={"path": "/test/cached.txt", "content": "Cached content"},
    )
    etag = write_response.json()["etag"]

    # First read - should return content
    response1 = await client.get(
        "/api/v2/files/read",
        params={"path": "/test/cached.txt"},
    )
    assert response1.status_code == 200

    # Second read with If-None-Match - should return 304
    response2 = await client.get(
        "/api/v2/files/read",
        params={"path": "/test/cached.txt"},
        headers={"If-None-Match": f'"{etag}"'},
    )
    assert response2.status_code == 304


# === Delete Endpoint Tests ===


@pytest.mark.asyncio
async def test_delete_file(client: AsyncClient) -> None:
    """Test deleting a file via API."""
    # Write a file
    await client.post(
        "/api/v2/files/write",
        json={"path": "/test/delete.txt", "content": "Delete me"},
    )

    # Delete it
    response = await client.delete(
        "/api/v2/files/delete",
        params={"path": "/test/delete.txt"},
    )

    assert response.status_code == 200
    assert response.json()["deleted"] is True

    # Verify it's gone
    read_response = await client.get(
        "/api/v2/files/read",
        params={"path": "/test/delete.txt"},
    )
    assert read_response.status_code == 404


@pytest.mark.asyncio
async def test_delete_nonexistent_file(client: AsyncClient) -> None:
    """Test deleting non-existent file returns 404."""
    response = await client.delete(
        "/api/v2/files/delete",
        params={"path": "/does/not/exist.txt"},
    )

    assert response.status_code == 404


# === Exists Endpoint Tests ===


@pytest.mark.asyncio
async def test_exists(client: AsyncClient) -> None:
    """Test checking file existence via API."""
    # File doesn't exist
    response1 = await client.get(
        "/api/v2/files/exists",
        params={"path": "/test/exists.txt"},
    )
    assert response1.status_code == 200
    assert response1.json()["exists"] is False

    # Write file
    await client.post(
        "/api/v2/files/write",
        json={"path": "/test/exists.txt", "content": "I exist!"},
    )

    # Now it exists
    response2 = await client.get(
        "/api/v2/files/exists",
        params={"path": "/test/exists.txt"},
    )
    assert response2.status_code == 200
    assert response2.json()["exists"] is True


# === List Directory Endpoint Tests ===


@pytest.mark.asyncio
async def test_list_directory(client: AsyncClient) -> None:
    """Test listing directory contents via API."""
    # Create some files
    await client.post(
        "/api/v2/files/write",
        json={"path": "/list_test/file1.txt", "content": "File 1"},
    )
    await client.post(
        "/api/v2/files/write",
        json={"path": "/list_test/file2.txt", "content": "File 2"},
    )

    # List directory
    response = await client.get(
        "/api/v2/files/list",
        params={"path": "/list_test"},
    )

    assert response.status_code == 200
    data = response.json()
    assert "items" in data
    items = data["items"]
    assert "file1.txt" in items
    assert "file2.txt" in items


@pytest.mark.asyncio
async def test_list_empty_directory(client: AsyncClient) -> None:
    """Test listing empty directory."""
    # Create empty directory
    await client.post(
        "/api/v2/files/mkdir",
        json={"path": "/empty_dir"},
    )

    response = await client.get(
        "/api/v2/files/list",
        params={"path": "/empty_dir"},
    )

    assert response.status_code == 200
    assert response.json()["items"] == []


# === Mkdir Endpoint Tests ===


@pytest.mark.asyncio
async def test_mkdir(client: AsyncClient) -> None:
    """Test creating directory via API."""
    response = await client.post(
        "/api/v2/files/mkdir",
        json={"path": "/new_dir", "parents": True},
    )

    assert response.status_code == 200

    # Verify it exists
    exists_response = await client.get(
        "/api/v2/files/exists",
        params={"path": "/new_dir"},
    )
    assert exists_response.json()["exists"] is True


@pytest.mark.asyncio
async def test_mkdir_nested(client: AsyncClient) -> None:
    """Test creating nested directories."""
    response = await client.post(
        "/api/v2/files/mkdir",
        json={"path": "/a/b/c/d", "parents": True},
    )

    assert response.status_code == 200

    # All levels should exist
    for path in ["/a", "/a/b", "/a/b/c", "/a/b/c/d"]:
        exists_response = await client.get(
            "/api/v2/files/exists",
            params={"path": path},
        )
        assert exists_response.json()["exists"] is True


# === Metadata Endpoint Tests ===


@pytest.mark.asyncio
async def test_get_metadata(client: AsyncClient) -> None:
    """Test getting file metadata via API."""
    await client.post(
        "/api/v2/files/write",
        json={"path": "/test/meta.txt", "content": "Metadata test"},
    )

    response = await client.get(
        "/api/v2/files/metadata",
        params={"path": "/test/meta.txt"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["path"] == "/test/meta.txt"
    assert data["size"] == len("Metadata test")
    assert "etag" in data
    assert "version" in data


@pytest.mark.asyncio
async def test_get_metadata_nonexistent(client: AsyncClient) -> None:
    """Test getting metadata for non-existent file."""
    response = await client.get(
        "/api/v2/files/metadata",
        params={"path": "/does/not/exist.txt"},
    )

    assert response.status_code == 404


# === Batch Read Endpoint Tests ===


@pytest.mark.asyncio
async def test_batch_read(client: AsyncClient) -> None:
    """Test batch reading multiple files."""
    # Write some files
    await client.post(
        "/api/v2/files/write",
        json={"path": "/batch/file1.txt", "content": "Content 1"},
    )
    await client.post(
        "/api/v2/files/write",
        json={"path": "/batch/file2.txt", "content": "Content 2"},
    )

    # Batch read
    response = await client.post(
        "/api/v2/files/batch-read",
        json={"paths": ["/batch/file1.txt", "/batch/file2.txt"]},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["/batch/file1.txt"]["content"] == "Content 1"
    assert data["/batch/file2.txt"]["content"] == "Content 2"


@pytest.mark.asyncio
async def test_batch_read_with_missing(client: AsyncClient) -> None:
    """Test batch read with some missing files."""
    await client.post(
        "/api/v2/files/write",
        json={"path": "/batch/exists.txt", "content": "I exist"},
    )

    response = await client.post(
        "/api/v2/files/batch-read",
        json={"paths": ["/batch/exists.txt", "/batch/missing.txt"]},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["/batch/exists.txt"]["content"] == "I exist"
    assert data["/batch/missing.txt"] is None


# === Streaming Endpoint Tests ===


@pytest.mark.asyncio
async def test_stream_read(client: AsyncClient) -> None:
    """Test streaming read for large files."""
    # Write a larger file
    content = "X" * 1000
    await client.post(
        "/api/v2/files/write",
        json={"path": "/stream/large.txt", "content": content},
    )

    # Stream read
    response = await client.get(
        "/api/v2/files/stream",
        params={"path": "/stream/large.txt"},
    )

    assert response.status_code == 200
    # Check content type indicates streaming
    assert "application/octet-stream" in response.headers.get("content-type", "")


# === Error Handling Tests ===


@pytest.mark.asyncio
async def test_invalid_path(client: AsyncClient) -> None:
    """Test invalid path returns 400."""
    response = await client.post(
        "/api/v2/files/write",
        json={"path": "relative/path", "content": "Invalid"},
    )

    assert response.status_code == 400


@pytest.mark.asyncio
async def test_missing_required_param(client: AsyncClient) -> None:
    """Test missing required parameter returns 422."""
    response = await client.post(
        "/api/v2/files/write",
        json={"content": "No path provided"},
    )

    assert response.status_code == 422  # Validation error
