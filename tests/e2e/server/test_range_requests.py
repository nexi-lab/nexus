"""Integration tests for HTTP Range request support (Issue #790).

Tests the full HTTP layer using httpx.AsyncClient against the real FastAPI
app with AsyncNexusFS, verifying Range headers, 200/206/416 responses,
Content-Range, Accept-Ranges, If-Range, and edge cases.

Requires LocalRaft (Rust extension). Skipped if unavailable.
Run with: .venv/bin/python3.12 -m pytest tests/integration/test_range_requests.py -v
"""

from __future__ import annotations

import base64
from collections.abc import AsyncGenerator
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

# Skip entire module if Raft is not available
try:
    from nexus.storage.raft_metadata_store import RaftMetadataStore

    RaftMetadataStore.embedded("/tmp/_raft_test_probe").close()
    _raft_available = True
except Exception:
    _raft_available = False

pytestmark = pytest.mark.skipif(not _raft_available, reason="LocalRaft not available")

from nexus.core.async_nexus_fs import AsyncNexusFS  # noqa: E402

# =============================================================================
# Fixtures
# =============================================================================

TEST_CONTENT = b"ABCDEFGHIJ" * 100  # 1000 bytes


@pytest_asyncio.fixture
async def client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncGenerator[AsyncClient, None]:
    """Create full FastAPI app with real AsyncNexusFS for integration testing."""
    monkeypatch.setenv("NEXUS_ENFORCE_PERMISSIONS", "false")
    monkeypatch.setenv("NEXUS_SEARCH_DAEMON", "false")

    metadata_store = RaftMetadataStore.embedded(str(tmp_path / "raft"))

    async_fs = AsyncNexusFS(
        backend_root=tmp_path / "backend",
        metadata_store=metadata_store,
        tenant_id="range-test",
        enforce_permissions=False,
    )
    await async_fs.initialize()

    mock_nexus_fs = MagicMock()
    mock_nexus_fs._event_bus = None
    mock_nexus_fs._coordination_client = None

    from nexus.server.fastapi_server import _fastapi_app, create_app

    app = create_app(
        nexus_fs=mock_nexus_fs,
        database_url="sqlite:///:memory:",
    )
    _fastapi_app.state.async_nexus_fs = async_fs

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as c:
        yield c

    await async_fs.close()
    _fastapi_app.state.async_nexus_fs = None
    metadata_store.close()


async def _write_file(client: AsyncClient, path: str, content: bytes) -> dict:
    """Helper: write a file via the API and return response data."""
    resp = await client.post(
        "/api/v2/files/write",
        json={
            "path": path,
            "content": base64.b64encode(content).decode(),
            "encoding": "base64",
        },
    )
    assert resp.status_code == 200, f"Write failed: {resp.text}"
    return resp.json()


# =============================================================================
# Tests
# =============================================================================


@pytest.mark.asyncio
async def test_range_first_500_bytes(client: AsyncClient) -> None:
    """Range: bytes=0-499 → 206 with first 500 bytes."""
    await _write_file(client, "/range/test.bin", TEST_CONTENT)

    resp = await client.get(
        "/api/v2/files/stream",
        params={"path": "/range/test.bin"},
        headers={"Range": "bytes=0-499"},
    )
    assert resp.status_code == 206
    assert resp.content == TEST_CONTENT[:500]
    assert resp.headers["content-range"] == "bytes 0-499/1000"
    assert len(resp.content) == 500
    assert resp.headers["accept-ranges"] == "bytes"


@pytest.mark.asyncio
async def test_range_from_offset(client: AsyncClient) -> None:
    """Range: bytes=500- → 206 with last 500 bytes."""
    await _write_file(client, "/range/test2.bin", TEST_CONTENT)

    resp = await client.get(
        "/api/v2/files/stream",
        params={"path": "/range/test2.bin"},
        headers={"Range": "bytes=500-"},
    )
    assert resp.status_code == 206
    assert resp.content == TEST_CONTENT[500:]
    assert resp.headers["content-range"] == "bytes 500-999/1000"
    assert len(resp.content) == 500


@pytest.mark.asyncio
async def test_range_suffix(client: AsyncClient) -> None:
    """Range: bytes=-100 → 206 with last 100 bytes."""
    await _write_file(client, "/range/test3.bin", TEST_CONTENT)

    resp = await client.get(
        "/api/v2/files/stream",
        params={"path": "/range/test3.bin"},
        headers={"Range": "bytes=-100"},
    )
    assert resp.status_code == 206
    assert resp.content == TEST_CONTENT[-100:]
    assert resp.headers["content-range"] == "bytes 900-999/1000"


@pytest.mark.asyncio
async def test_no_range_returns_200_with_accept_ranges(client: AsyncClient) -> None:
    """No Range header → 200 + full content + Accept-Ranges: bytes."""
    await _write_file(client, "/range/test4.bin", TEST_CONTENT)

    resp = await client.get(
        "/api/v2/files/stream",
        params={"path": "/range/test4.bin"},
    )
    assert resp.status_code == 200
    assert resp.content == TEST_CONTENT
    assert resp.headers["accept-ranges"] == "bytes"
    assert len(resp.content) == 1000


@pytest.mark.asyncio
async def test_unsatisfiable_range_returns_416(client: AsyncClient) -> None:
    """Range beyond file size → 416 with Content-Range: bytes */1000."""
    await _write_file(client, "/range/test5.bin", TEST_CONTENT)

    resp = await client.get(
        "/api/v2/files/stream",
        params={"path": "/range/test5.bin"},
        headers={"Range": "bytes=2000-3000"},
    )
    assert resp.status_code == 416
    assert resp.headers["content-range"] == "bytes */1000"


@pytest.mark.asyncio
async def test_if_range_matching_etag(client: AsyncClient) -> None:
    """If-Range with correct ETag → 206."""
    write_data = await _write_file(client, "/range/test6.bin", TEST_CONTENT)
    etag = write_data["etag"]

    resp = await client.get(
        "/api/v2/files/stream",
        params={"path": "/range/test6.bin"},
        headers={"Range": "bytes=0-99", "If-Range": f'"{etag}"'},
    )
    assert resp.status_code == 206
    assert resp.content == TEST_CONTENT[:100]


@pytest.mark.asyncio
async def test_if_range_wrong_etag(client: AsyncClient) -> None:
    """If-Range with wrong ETag → 200 (full content)."""
    await _write_file(client, "/range/test7.bin", TEST_CONTENT)

    resp = await client.get(
        "/api/v2/files/stream",
        params={"path": "/range/test7.bin"},
        headers={"Range": "bytes=0-99", "If-Range": '"wrong-etag"'},
    )
    assert resp.status_code == 200
    assert resp.content == TEST_CONTENT


@pytest.mark.asyncio
async def test_single_byte_range(client: AsyncClient) -> None:
    """Range: bytes=0-0 → 206 with single byte."""
    await _write_file(client, "/range/test8.bin", TEST_CONTENT)

    resp = await client.get(
        "/api/v2/files/stream",
        params={"path": "/range/test8.bin"},
        headers={"Range": "bytes=0-0"},
    )
    assert resp.status_code == 206
    assert resp.content == TEST_CONTENT[:1]
    assert resp.headers["content-range"] == "bytes 0-0/1000"
    assert len(resp.content) == 1
