"""Tests for ?zone= query-param override on /read (#3785)."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from nexus.server.api.v2.routers.async_files import create_async_files_router
from nexus.server.dependencies import get_auth_result, require_auth


def _auth(zone_set: list[str], zone_id: str = "eng") -> dict[str, Any]:
    return {
        "authenticated": True,
        "subject_type": "user",
        "subject_id": "alice",
        "user_id": "alice",
        "zone_id": zone_id,
        "zone_set": zone_set,
        "is_admin": False,
        "groups": [],
    }


@pytest.fixture()
def mock_fs() -> MagicMock:
    fs = MagicMock()
    fs.sys_stat = MagicMock(return_value=None)
    fs.service = MagicMock(return_value=None)
    return fs


def _build_client(mock_fs: MagicMock, auth: dict[str, Any]) -> tuple[TestClient, list[Any]]:
    """Wire mock_fs into a router-mounted app; capture context passed to fs.read."""
    captured: list[Any] = []

    def _capturing_read(path: str, return_metadata: bool = False, context: Any = None) -> str:
        captured.append(context)
        return "hello-world"

    mock_fs.read = MagicMock(side_effect=_capturing_read)

    app = FastAPI()
    router = create_async_files_router(nexus_fs=mock_fs)
    app.include_router(router)
    app.dependency_overrides[get_auth_result] = lambda: auth
    app.dependency_overrides[require_auth] = lambda: auth
    return TestClient(app), captured


def test_read_file_zone_param_in_set_overrides_context(mock_fs: MagicMock) -> None:
    """?zone=ops uses ops as zone_id when ops is in token's zone_set."""
    client, captured = _build_client(mock_fs, _auth(zone_set=["eng", "ops"]))

    resp = client.get("/read", params={"path": "/x.txt", "zone": "ops"})

    assert resp.status_code == 200, resp.text
    assert len(captured) == 1
    assert captured[0].zone_id == "ops"


def test_read_file_zone_param_outside_set_returns_403(mock_fs: MagicMock) -> None:
    """?zone=legal with token zone_set=[eng] -> 403 from _gate_zone."""
    client, captured = _build_client(mock_fs, _auth(zone_set=["eng"]))

    resp = client.get("/read", params={"path": "/x.txt", "zone": "legal"})

    assert resp.status_code == 403, resp.text
    detail = resp.json()["detail"]
    assert "legal" in detail
    assert "allow-list" in detail.lower()
    assert captured == []


def test_read_file_no_zone_param_uses_context_default(mock_fs: MagicMock) -> None:
    """No ?zone= -> unchanged: fs.read is called with the original context."""
    client, captured = _build_client(mock_fs, _auth(zone_set=["eng"]))

    resp = client.get("/read", params={"path": "/x.txt"})

    assert resp.status_code == 200, resp.text
    assert len(captured) == 1
    assert captured[0].zone_id == "eng"
