"""Tests for paginated /list endpoint (Issue #3102, Decision 2A)."""

import base64
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from nexus.core.pagination import PaginatedResult
from nexus.server.api.v2.routers.async_files import create_async_files_router
from nexus.server.dependencies import get_auth_result

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_entry(path: str, size: int = 100, content_id: str = "etag1", entry_type: int = 0) -> dict:
    """Build a details dict as returned by sys_readdir(details=True)."""
    return {"path": path, "size": size, "content_id": content_id, "entry_type": entry_type}


def _encode_cursor(path: str) -> str:
    return base64.b64encode(path.encode()).decode()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_fs() -> MagicMock:
    """Create a mock NexusFS with async methods."""
    fs = MagicMock()
    # sys_readdir is async — must return a coroutine
    fs.sys_readdir = AsyncMock()
    return fs


@pytest.fixture()
def client(mock_fs: MagicMock) -> TestClient:
    """Create a TestClient with mock FS and bypassed auth."""
    app = FastAPI()
    router = create_async_files_router(nexus_fs=mock_fs)
    app.include_router(router)

    app.dependency_overrides[get_auth_result] = lambda: {
        "authenticated": True,
        "user_id": "test-user",
        "groups": [],
        "zone_id": "root",
        "is_admin": False,
    }

    return TestClient(app)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestListPagination:
    """Cursor-based pagination on GET /list."""

    def test_list_without_limit_returns_all_items(
        self, client: TestClient, mock_fs: MagicMock
    ) -> None:
        """Without limit, sys_readdir returns a plain list (backward compat)."""
        mock_fs.sys_readdir.return_value = [
            _make_entry("/data/a.txt", size=10, content_id="e1"),
            _make_entry("/data/b.txt", size=20, content_id="e2"),
            _make_entry("/data/sub/", size=0, content_id="e3", entry_type=1),
        ]

        resp = client.get("/list", params={"path": "/data"})

        assert resp.status_code == 200
        body = resp.json()
        assert len(body["items"]) == 3
        assert body["has_more"] is False
        assert body["next_cursor"] is None
        # Verify names derived from path
        assert body["items"][0]["name"] == "a.txt"
        assert body["items"][2]["name"] == "sub"
        assert body["items"][2]["isDirectory"] is True

    def test_list_with_limit_returns_first_page(
        self, client: TestClient, mock_fs: MagicMock
    ) -> None:
        """With limit=2, returns first 2 items, has_more=True, next_cursor set."""
        next_cursor_path = "/data/b.txt"
        mock_fs.sys_readdir.return_value = PaginatedResult(
            items=[
                _make_entry("/data/a.txt", size=10, content_id="e1"),
                _make_entry("/data/b.txt", size=20, content_id="e2"),
            ],
            next_cursor=next_cursor_path,
            has_more=True,
        )

        resp = client.get("/list", params={"path": "/data", "limit": 2})

        assert resp.status_code == 200
        body = resp.json()
        assert len(body["items"]) == 2
        assert body["has_more"] is True
        assert body["next_cursor"] == _encode_cursor(next_cursor_path)
        # Verify sys_readdir called with limit and cursor=None
        call_kwargs = mock_fs.sys_readdir.call_args[1]
        assert call_kwargs["limit"] == 2
        assert call_kwargs["cursor"] is None

    def test_list_with_limit_and_cursor_returns_next_page(
        self, client: TestClient, mock_fs: MagicMock
    ) -> None:
        """Passing cursor from previous response fetches the next page."""
        cursor_path = "/data/b.txt"
        encoded_cursor = _encode_cursor(cursor_path)

        mock_fs.sys_readdir.return_value = PaginatedResult(
            items=[
                _make_entry("/data/c.txt", size=30, content_id="e3"),
                _make_entry("/data/d.txt", size=40, content_id="e4"),
            ],
            next_cursor="/data/d.txt",
            has_more=True,
        )

        resp = client.get(
            "/list",
            params={"path": "/data", "limit": 2, "cursor": encoded_cursor},
        )

        assert resp.status_code == 200
        body = resp.json()
        assert len(body["items"]) == 2
        assert body["items"][0]["name"] == "c.txt"
        # Verify decoded cursor was forwarded to sys_readdir
        call_kwargs = mock_fs.sys_readdir.call_args[1]
        assert call_kwargs["cursor"] == cursor_path

    def test_last_page_has_no_more(self, client: TestClient, mock_fs: MagicMock) -> None:
        """Last page: has_more=False, next_cursor=None."""
        mock_fs.sys_readdir.return_value = PaginatedResult(
            items=[
                _make_entry("/data/e.txt", size=50, content_id="e5"),
            ],
            next_cursor=None,
            has_more=False,
        )

        resp = client.get("/list", params={"path": "/data", "limit": 2})

        assert resp.status_code == 200
        body = resp.json()
        assert len(body["items"]) == 1
        assert body["has_more"] is False
        assert body["next_cursor"] is None

    def test_empty_directory(self, client: TestClient, mock_fs: MagicMock) -> None:
        """Empty directory returns empty items, has_more=False."""
        mock_fs.sys_readdir.return_value = PaginatedResult(
            items=[],
            next_cursor=None,
            has_more=False,
        )

        resp = client.get("/list", params={"path": "/empty", "limit": 10})

        assert resp.status_code == 200
        body = resp.json()
        assert body["items"] == []
        assert body["has_more"] is False
        assert body["next_cursor"] is None

    def test_invalid_cursor_returns_400(self, client: TestClient, mock_fs: MagicMock) -> None:
        """A cursor that is not valid base64 returns 400."""
        resp = client.get(
            "/list",
            params={"path": "/data", "limit": 10, "cursor": "%%%not-base64%%%"},
        )

        assert resp.status_code == 400
        assert "Invalid cursor" in resp.json()["detail"]
        # sys_readdir should never be called
        mock_fs.sys_readdir.assert_not_called()

    def test_limit_exceeds_max_returns_422(self, client: TestClient) -> None:
        """limit=1001 exceeds the max (1000) and triggers validation error."""
        resp = client.get("/list", params={"path": "/data", "limit": 1001})

        assert resp.status_code == 422
