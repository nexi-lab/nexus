"""Tests for GET /metadata on the async files router."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from nexus.server.api.v2.routers.async_files import create_async_files_router
from nexus.server.dependencies import get_auth_result, require_auth


def _auth() -> dict[str, Any]:
    return {
        "authenticated": True,
        "subject_type": "user",
        "subject_id": "admin",
        "user_id": "admin",
        "zone_id": "root",
        "zone_set": ["root"],
        "zone_perms": [["root", "x"]],
        "is_admin": True,
        "groups": [],
    }


def _build_client(mock_fs: MagicMock) -> TestClient:
    app = FastAPI()
    app.include_router(create_async_files_router(nexus_fs=mock_fs))
    auth = _auth()
    app.dependency_overrides[get_auth_result] = lambda: auth
    app.dependency_overrides[require_auth] = lambda: auth
    return TestClient(app)


def test_metadata_accepts_sys_stat_dict() -> None:
    """sys_stat returns dicts from the Rust-backed NexusFS path."""
    mock_fs = MagicMock()
    mock_fs.sys_stat = MagicMock(
        return_value={
            "path": "/",
            "size": 0,
            "etag": None,
            "version": 1,
            "is_directory": True,
            "created_at": None,
            "modified_at": None,
        }
    )
    client = _build_client(mock_fs)

    resp = client.get("/metadata", params={"path": "/"})

    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "path": "/",
        "size": 0,
        "etag": None,
        "version": 1,
        "is_directory": True,
        "created_at": None,
        "modified_at": None,
    }
