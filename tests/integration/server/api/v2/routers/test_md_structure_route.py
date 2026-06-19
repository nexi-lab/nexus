"""Tests for GET /md-structure on the async files router."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from nexus.server.api.v2.routers.async_files import create_async_files_router
from nexus.server.dependencies import get_auth_result


def test_md_structure_returns_hook_listing_for_markdown_file() -> None:
    fs = MagicMock()
    fs.access = AsyncMock(return_value=True)
    fs.read = AsyncMock(return_value=b"# Title\n\nBody\n")
    fs.metadata.get.return_value = SimpleNamespace(content_id="content-1")

    hook = MagicMock()
    hook.get_structure_listing.return_value = [
        {"type": "heading", "level": 1, "title": "Title", "line_start": 1, "line_end": 1}
    ]
    fs.service.return_value = hook

    app = FastAPI()
    app.include_router(create_async_files_router(nexus_fs=fs))
    app.dependency_overrides[get_auth_result] = lambda: {
        "authenticated": True,
        "user_id": "test-user",
        "groups": [],
        "zone_id": "root",
        "is_admin": False,
    }

    response = TestClient(app).get("/md-structure", params={"path": "/docs/readme.md"})

    assert response.status_code == 200
    assert response.json() == [
        {"type": "heading", "level": 1, "title": "Title", "line_start": 1, "line_end": 1}
    ]
    fs.access.assert_awaited_once()
    fs.read.assert_awaited_once()
    hook.get_structure_listing.assert_called_once_with(
        "/docs/readme.md",
        content=b"# Title\n\nBody\n",
        content_id="content-1",
    )
