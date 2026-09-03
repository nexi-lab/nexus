"""v2 files router: revision on mutations, X-Nexus-Min-Revision on reads (#4737)."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from nexus.lib.zone_revision import (
    MIN_REVISION_HEADER,
    REVISION_HEADER,
    REVISION_TIMEOUT_HEADER,
    reset_zone_revision_cache,
)
from nexus.server.api.v2.routers.async_files import create_async_files_router
from nexus.server.dependencies import get_auth_result

# What this node's sys_stat currently sees for the anchor path.
_LOCAL_GEN = 10


@pytest.fixture(autouse=True)
def _fresh_cache() -> None:
    reset_zone_revision_cache()


@pytest.fixture()
def mock_fs() -> MagicMock:
    fs = MagicMock()
    fs.write.return_value = {
        "content_id": "abc",
        "version": 2,
        "gen": 2,
        "size": 5,
        "modified_at": None,
        "revision": "/a.txt@2",
    }
    fs.write_batch.return_value = [
        {
            "content_id": "abc",
            "version": 1,
            "gen": 1,
            "size": 1,
            "modified_at": None,
            "revision": "/a.txt@1",
        },
    ]
    fs.sys_unlink.return_value = {"revision": None}
    fs.sys_rename = AsyncMock(return_value={"revision": "root@11"})
    fs.read.return_value = b"hello"
    fs.sys_stat.return_value = {
        "path": "/a.txt",
        "size": 5,
        "content_id": "abc",
        "version": 2,
        "gen": _LOCAL_GEN,
        "is_directory": False,
    }
    fs.sys_readdir.return_value = []
    fs.access.return_value = True
    return fs


@pytest.fixture()
def client(mock_fs: MagicMock) -> TestClient:
    app = FastAPI()
    app.include_router(create_async_files_router(nexus_fs=mock_fs))
    app.dependency_overrides[get_auth_result] = lambda: {
        "authenticated": True,
        "user_id": "test-user",
        "groups": [],
        "zone_id": "root",
        "is_admin": False,
    }
    return TestClient(app)


# ---------------------------------------------------------------------------
# Mutations carry the revision
# ---------------------------------------------------------------------------


def test_write_returns_revision_in_body_and_header(client: TestClient) -> None:
    resp = client.post("/write", json={"path": "/a.txt", "content": "hello"})

    assert resp.status_code == 200, resp.text
    assert resp.json()["revision"] == "/a.txt@2"
    assert resp.headers[REVISION_HEADER] == "/a.txt@2"


def test_write_without_revision_reports_null(client: TestClient, mock_fs: MagicMock) -> None:
    mock_fs.write.return_value = {
        "content_id": "abc",
        "version": 1,
        "gen": 1,
        "size": 5,
        "modified_at": None,
    }

    resp = client.post("/write", json={"path": "/a.txt", "content": "hello"})

    assert resp.status_code == 200, resp.text
    assert resp.json()["revision"] is None
    assert REVISION_HEADER not in resp.headers


def test_batch_write_returns_per_item_revision(client: TestClient) -> None:
    resp = client.post(
        "/batch/write",
        json={"files": [{"path": "/a.txt", "content_base64": "aGk="}]},
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["results"][0]["revision"] == "/a.txt@1"


def test_delete_without_kernel_stamp_reports_null(client: TestClient) -> None:
    resp = client.delete("/delete", params={"path": "/a.txt"})

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"deleted": True, "path": "/a.txt", "revision": None}
    assert REVISION_HEADER not in resp.headers


def test_rename_passes_kernel_stamped_revision_through(client: TestClient) -> None:
    resp = client.post("/rename", json={"source": "/a.txt", "destination": "/b.txt"})

    assert resp.status_code == 200, resp.text
    assert resp.json()["revision"] == "root@11"
    assert resp.headers[REVISION_HEADER] == "root@11"


# ---------------------------------------------------------------------------
# Reads honour the fence
# ---------------------------------------------------------------------------


def test_read_without_fence_does_not_stat(client: TestClient, mock_fs: MagicMock) -> None:
    resp = client.get("/read", params={"path": "/a.txt"})

    assert resp.status_code == 200, resp.text
    assert REVISION_HEADER not in resp.headers
    mock_fs.sys_stat.assert_not_called()


def test_read_with_satisfied_fence_stamps_observed_revision(
    client: TestClient, mock_fs: MagicMock
) -> None:
    resp = client.get("/read", params={"path": "/a.txt"}, headers={MIN_REVISION_HEADER: "/a.txt@7"})

    assert resp.status_code == 200, resp.text
    assert resp.json()["content"] == "hello"
    assert resp.headers[REVISION_HEADER] == f"/a.txt@{_LOCAL_GEN}"
    # The fence stat carried the caller's context (permission hooks apply).
    _path, kwargs = mock_fs.sys_stat.call_args.args[0], mock_fs.sys_stat.call_args.kwargs
    assert _path == "/a.txt" and kwargs["context"] is not None


def test_read_behind_follower_returns_412_not_stale_data(
    client: TestClient, mock_fs: MagicMock
) -> None:
    resp = client.get(
        "/read",
        params={"path": "/a.txt"},
        headers={MIN_REVISION_HEADER: "/a.txt@50", REVISION_TIMEOUT_HEADER: "0"},
    )

    assert resp.status_code == 412, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "revision_not_applied"
    assert detail["current_revision"] == f"/a.txt@{_LOCAL_GEN}"
    assert resp.headers[REVISION_HEADER] == f"/a.txt@{_LOCAL_GEN}"
    mock_fs.read.assert_not_called()


def test_read_of_path_this_node_has_not_seen_is_412(client: TestClient, mock_fs: MagicMock) -> None:
    mock_fs.sys_stat.return_value = None

    resp = client.get(
        "/read",
        params={"path": "/new.txt"},
        headers={MIN_REVISION_HEADER: "/new.txt@1", REVISION_TIMEOUT_HEADER: "0"},
    )

    assert resp.status_code == 412, resp.text
    assert resp.json()["detail"]["current_revision"] == "/new.txt@0"
    mock_fs.read.assert_not_called()


@pytest.mark.parametrize(
    ("method", "route", "params"),
    [
        ("get", "/metadata", {"path": "/a.txt"}),
        ("get", "/exists", {"path": "/a.txt"}),
        ("get", "/list", {"path": "/"}),
        ("get", "/glob", {"pattern": "*.txt", "path": "/"}),
        ("get", "/grep", {"pattern": "hello", "path": "/"}),
    ],
)
def test_metadata_listing_routes_stamp_and_fence(
    client: TestClient, method: str, route: str, params: dict[str, str]
) -> None:
    ok = client.request(method, route, params=params, headers={MIN_REVISION_HEADER: "/a.txt@3"})
    assert ok.status_code == 200, ok.text
    assert ok.headers[REVISION_HEADER] == f"/a.txt@{_LOCAL_GEN}"

    behind = client.request(
        method,
        route,
        params={**params, "min_revision": "/a.txt@99", "revision_timeout_ms": "0"},
    )
    assert behind.status_code == 412, behind.text
    assert behind.json()["detail"]["current_revision"] == f"/a.txt@{_LOCAL_GEN}"


def test_batch_read_routes_honour_fence(client: TestClient, mock_fs: MagicMock) -> None:
    mock_fs.read_bulk.return_value = {"/a.txt": b"hello"}
    mock_fs.read_batch.return_value = [
        {"path": "/a.txt", "content": b"hello", "content_id": "abc", "version": 1, "size": 5}
    ]

    ok = client.post(
        "/batch-read", json={"paths": ["/a.txt"]}, headers={MIN_REVISION_HEADER: "/a.txt@1"}
    )
    assert ok.status_code == 200, ok.text
    assert ok.headers[REVISION_HEADER] == f"/a.txt@{_LOCAL_GEN}"

    ok_v2 = client.post(
        "/batch/read", json={"paths": ["/a.txt"]}, headers={MIN_REVISION_HEADER: "/a.txt@1"}
    )
    assert ok_v2.status_code == 200, ok_v2.text
    assert ok_v2.headers[REVISION_HEADER] == f"/a.txt@{_LOCAL_GEN}"

    behind = client.post(
        "/batch/read",
        json={"paths": ["/a.txt"]},
        headers={MIN_REVISION_HEADER: "/a.txt@99", REVISION_TIMEOUT_HEADER: "0"},
    )
    assert behind.status_code == 412, behind.text


def test_zone_token_on_pinned_kernel_is_501(client: TestClient, mock_fs: MagicMock) -> None:
    class PinnedKernel:
        def _call(self, method: str, params: dict[str, Any]) -> Any:
            raise RuntimeError(f"RPC error [-32603]: unknown Call method: {method}")

    mock_fs._kernel = PinnedKernel()

    resp = client.get("/read", params={"path": "/a.txt"}, headers={MIN_REVISION_HEADER: "root@1"})

    assert resp.status_code == 501, resp.text
    assert resp.json()["detail"]["error"] == "zone_revision_unavailable"
    mock_fs.read.assert_not_called()
