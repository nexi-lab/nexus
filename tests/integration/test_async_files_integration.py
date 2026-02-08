"""Integration tests for async files with full FastAPI server.

Tests the complete server stack using `create_app` with real SQLite database.
This verifies:
- AsyncNexusFS initialization via server lifespan
- Router registration via lazy getter pattern
- All 9 endpoints working through the full HTTP stack
- Real database operations (not mocked)

Issue #940: Full async migration for MetadataStore and NexusFS.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import create_async_engine

from nexus.core.async_nexus_fs import AsyncNexusFS
from nexus.storage.models import (
    DirectoryEntryModel,
    FilePathModel,
    VersionHistoryModel,
)

# === Fixtures ===


@pytest_asyncio.fixture
async def client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncGenerator[AsyncClient, None]:
    """Create full FastAPI app via create_app with real AsyncNexusFS.

    This tests the real server initialization path:
    1. create_app() creates the app with routes (including lazy getter)
    2. We initialize AsyncNexusFS with SQLite and inject into _app_state
       (simulating what lifespan does with a real database)
    3. The lazy getter connects the router to the fs at request time
    4. All requests go through the full FastAPI stack (middleware, auth, etc.)
    """
    # Set env vars
    monkeypatch.setenv("NEXUS_ENFORCE_PERMISSIONS", "false")
    monkeypatch.setenv("NEXUS_SEARCH_DAEMON", "false")

    # Create SQLite database with required tables
    db_file = tmp_path / "integration_test.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_file}", echo=False)

    async with engine.begin() as conn:
        for table in [
            FilePathModel.__table__,
            DirectoryEntryModel.__table__,
            VersionHistoryModel.__table__,
        ]:
            await conn.run_sync(lambda c, t=table: t.create(c, checkfirst=True))

    # Initialize real AsyncNexusFS with SQLite
    backend_root = tmp_path / "backend"
    async_fs = AsyncNexusFS(
        backend_root=backend_root,
        engine=engine,
        tenant_id="integration-test",
        enforce_permissions=False,
    )
    await async_fs.initialize()

    # Create a minimal mock NexusFS (required by create_app signature)
    mock_nexus_fs = MagicMock()
    mock_nexus_fs._event_bus = None
    mock_nexus_fs._coordination_client = None

    # Import and create the real app via create_app
    from nexus.server.fastapi_server import _app_state, create_app

    database_url = f"sqlite:///{db_file}"
    app = create_app(
        nexus_fs=mock_nexus_fs,
        database_url=database_url,
    )

    # Inject real AsyncNexusFS into _app_state
    # (this is what lifespan() does during server startup)
    _app_state.async_nexus_fs = async_fs

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        yield client

    # Cleanup
    await async_fs.close()
    _app_state.async_nexus_fs = None
    await engine.dispose()


# === E2E Tests: Full FastAPI Server Stack ===


@pytest.mark.asyncio
async def test_server_write_and_read(client: AsyncClient) -> None:
    """Test write + read through full FastAPI server stack."""
    # Write
    write_resp = await client.post(
        "/api/v2/files/write",
        json={"path": "/e2e/hello.txt", "content": "Hello from E2E!"},
    )
    assert write_resp.status_code == 200, f"Write failed: {write_resp.text}"
    data = write_resp.json()
    assert data["version"] == 1
    assert data["size"] == len("Hello from E2E!")
    assert "etag" in data

    # Read
    read_resp = await client.get(
        "/api/v2/files/read",
        params={"path": "/e2e/hello.txt"},
    )
    assert read_resp.status_code == 200, f"Read failed: {read_resp.text}"
    assert read_resp.json()["content"] == "Hello from E2E!"


@pytest.mark.asyncio
async def test_server_delete(client: AsyncClient) -> None:
    """Test delete through full server stack."""
    await client.post(
        "/api/v2/files/write",
        json={"path": "/deleteme.txt", "content": "bye"},
    )

    del_resp = await client.delete(
        "/api/v2/files/delete",
        params={"path": "/deleteme.txt"},
    )
    assert del_resp.status_code == 200
    assert del_resp.json()["deleted"] is True

    # Confirm gone
    exists_resp = await client.get(
        "/api/v2/files/exists",
        params={"path": "/deleteme.txt"},
    )
    assert exists_resp.json()["exists"] is False


@pytest.mark.asyncio
async def test_server_mkdir_and_list(client: AsyncClient) -> None:
    """Test mkdir + list through full server stack."""
    # Create dir
    mkdir_resp = await client.post(
        "/api/v2/files/mkdir",
        json={"path": "/mydir", "parents": True},
    )
    assert mkdir_resp.status_code == 200

    # Write files in it
    for name in ["alpha.txt", "beta.txt", "gamma.txt"]:
        resp = await client.post(
            "/api/v2/files/write",
            json={"path": f"/mydir/{name}", "content": f"content-{name}"},
        )
        assert resp.status_code == 200, f"Write {name} failed: {resp.text}"

    # List
    list_resp = await client.get(
        "/api/v2/files/list",
        params={"path": "/mydir"},
    )
    assert list_resp.status_code == 200
    items = list_resp.json()["items"]
    assert len(items) == 3
    assert "alpha.txt" in items
    assert "beta.txt" in items
    assert "gamma.txt" in items


@pytest.mark.asyncio
async def test_server_metadata(client: AsyncClient) -> None:
    """Test metadata endpoint through full server stack."""
    await client.post(
        "/api/v2/files/write",
        json={"path": "/meta.txt", "content": "metadata test"},
    )

    meta_resp = await client.get(
        "/api/v2/files/metadata",
        params={"path": "/meta.txt"},
    )
    assert meta_resp.status_code == 200
    data = meta_resp.json()
    assert data["path"] == "/meta.txt"
    assert data["size"] == len("metadata test")
    assert data["version"] == 1
    assert data["is_directory"] is False


@pytest.mark.asyncio
async def test_server_batch_read(client: AsyncClient) -> None:
    """Test batch-read endpoint through full server stack."""
    paths = ["/batch/a.txt", "/batch/b.txt"]
    for p in paths:
        await client.post(
            "/api/v2/files/write",
            json={"path": p, "content": f"batch-{p}"},
        )

    batch_resp = await client.post(
        "/api/v2/files/batch-read",
        json={"paths": paths},
    )
    assert batch_resp.status_code == 200
    data = batch_resp.json()
    for p in paths:
        assert data[p]["content"] == f"batch-{p}"


@pytest.mark.asyncio
async def test_server_stream(client: AsyncClient) -> None:
    """Test stream endpoint through full server stack."""
    content = "S" * 5000
    await client.post(
        "/api/v2/files/write",
        json={"path": "/stream.txt", "content": content},
    )

    stream_resp = await client.get(
        "/api/v2/files/stream",
        params={"path": "/stream.txt"},
    )
    assert stream_resp.status_code == 200
    assert stream_resp.content.decode() == content


@pytest.mark.asyncio
async def test_server_etag_304(client: AsyncClient) -> None:
    """Test ETag-based 304 caching through full server stack."""
    write_resp = await client.post(
        "/api/v2/files/write",
        json={"path": "/etag.txt", "content": "cache me"},
    )
    etag = write_resp.json()["etag"]

    # Should return 304 with matching etag
    cached_resp = await client.get(
        "/api/v2/files/read",
        params={"path": "/etag.txt"},
        headers={"If-None-Match": f'"{etag}"'},
    )
    assert cached_resp.status_code == 304


@pytest.mark.asyncio
async def test_server_version_bumps(client: AsyncClient) -> None:
    """Test version incrementing through full server stack."""
    for i in range(1, 4):
        resp = await client.post(
            "/api/v2/files/write",
            json={"path": "/versioned.txt", "content": f"v{i}"},
        )
        assert resp.status_code == 200
        assert resp.json()["version"] == i

    read_resp = await client.get(
        "/api/v2/files/read",
        params={"path": "/versioned.txt"},
    )
    assert read_resp.json()["content"] == "v3"


@pytest.mark.asyncio
async def test_server_404_on_missing(client: AsyncClient) -> None:
    """Test 404 for nonexistent files through full server stack."""
    read_resp = await client.get(
        "/api/v2/files/read",
        params={"path": "/nonexistent.txt"},
    )
    assert read_resp.status_code == 404

    del_resp = await client.delete(
        "/api/v2/files/delete",
        params={"path": "/nonexistent.txt"},
    )
    assert del_resp.status_code == 404

    meta_resp = await client.get(
        "/api/v2/files/metadata",
        params={"path": "/nonexistent.txt"},
    )
    assert meta_resp.status_code == 404
