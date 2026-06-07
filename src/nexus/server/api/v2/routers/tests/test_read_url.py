"""Tests for GET /api/v2/files/read-url — the signed-URL read path.

The endpoint does a ReBAC gate (sys_stat) and, for S3/R2 mounts, returns a
short-TTL presigned URL so the client fetches bytes directly from object
storage. nexus stays out of the byte path.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from nexus.server.api.v2.routers.async_files import create_async_files_router
from nexus.server.dependencies import get_auth_result


class _SignableBackend:
    """Stand-in S3/R2 backend exposing generate_signed_url (the SIGNED_URL cap)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, int, str]] = []

    def generate_signed_url(
        self,
        path: str,
        expires_in: int = 3600,
        method: str = "GET",
        context: object = None,  # noqa: ARG002
    ) -> dict[str, object]:
        self.calls.append((path, expires_in, method))
        return {
            "url": f"https://acct.r2.cloudflarestorage.com/bucket/ws/{path}?X-Amz-Signature=deadbeef",
            "expires_in": expires_in,
            "method": method,
        }


class _PlainBackend:
    """A non-S3 backend with NO generate_signed_url (e.g. local)."""


class _GcsLikeBackend:
    """A signing backend whose generate_signed_url has NO ``method`` param (like
    PathGCSBackend). read-url must 409 it, not call it into a TypeError -> 500."""

    def generate_signed_url(
        self,
        path: str,  # noqa: ARG002
        expires_in: int = 3600,  # noqa: ARG002
        context=None,  # noqa: ARG002
    ) -> dict[str, object]:
        raise AssertionError("read-url must not call a method-less (non-S3) signer")


_DEFAULT_STAT = {"content_id": "abc123", "size": 12, "is_directory": False}


def _make_client(mounts: dict[str, object], stat_result: object = _DEFAULT_STAT) -> TestClient:
    fs = MagicMock()
    fs.sys_stat.return_value = stat_result
    # The endpoint resolves the owning mount via this dict.
    fs._mounted_backend_instances = mounts

    app = FastAPI()
    app.include_router(create_async_files_router(nexus_fs=fs))
    app.dependency_overrides[get_auth_result] = lambda: {
        "authenticated": True,
        "user_id": "alice",
        "groups": [],
        "zone_id": "root",
        "is_admin": True,
    }
    return TestClient(app)


@pytest.fixture()
def s3_backend() -> _SignableBackend:
    return _SignableBackend()


def test_read_url_s3_mount_returns_presigned_url(s3_backend: _SignableBackend) -> None:
    """An S3/R2 mount → ReBAC gate passes → presigned URL returned, no bytes."""
    client = _make_client({"/workspace": s3_backend})

    resp = client.get("/read-url", params={"path": "/workspace/demo/file.md", "ttl": 120})

    assert resp.status_code == 200
    body = resp.json()
    assert body["url"].startswith("https://acct.r2.cloudflarestorage.com/")
    assert body["expires_in"] == 120
    assert body["content_id"] == "abc123"
    assert body["path"] == "/workspace/demo/file.md"
    # Path handed to the backend is mount-relative (mount prefix stripped).
    assert s3_backend.calls == [("demo/file.md", 120, "GET")]


def test_read_url_longest_mount_prefix_wins() -> None:
    """With nested mounts, the longest matching prefix owns the path."""
    inner = _SignableBackend()
    outer = _SignableBackend()
    client = _make_client({"/workspace": outer, "/workspace/sub": inner})

    resp = client.get("/read-url", params={"path": "/workspace/sub/x.txt"})

    assert resp.status_code == 200
    assert inner.calls and not outer.calls
    assert inner.calls[0][0] == "x.txt"


def test_read_url_non_s3_mount_returns_409() -> None:
    """A backend without generate_signed_url → 409 (caller falls back to /read)."""
    client = _make_client({"/local": _PlainBackend()})

    resp = client.get("/read-url", params={"path": "/local/file.txt"})

    assert resp.status_code == 409


def test_read_url_no_mount_returns_409() -> None:
    """No owning mount at all → 409, never a 500."""
    client = _make_client({})

    resp = client.get("/read-url", params={"path": "/nowhere/file.txt"})

    assert resp.status_code == 409


def test_read_url_missing_path_returns_422() -> None:
    """Missing required 'path' query param → FastAPI 422."""
    client = _make_client({"/workspace": _SignableBackend()})

    resp = client.get("/read-url")

    assert resp.status_code == 422


def test_read_url_ttl_bounds_enforced(s3_backend: _SignableBackend) -> None:
    """ttl is bounded (5..3600); out-of-range → 422."""
    client = _make_client({"/workspace": s3_backend})

    assert client.get("/read-url", params={"path": "/workspace/f", "ttl": 1}).status_code == 422
    assert client.get("/read-url", params={"path": "/workspace/f", "ttl": 99999}).status_code == 422


def test_read_url_stat_none_returns_404_without_signing(s3_backend: _SignableBackend) -> None:
    """sys_stat → None (path not resolvable) must 404 BEFORE any signing — never
    mint a bearer URL for a key the VFS didn't prove exists."""
    client = _make_client({"/workspace": s3_backend}, stat_result=None)

    resp = client.get("/read-url", params={"path": "/workspace/missing.txt"})

    assert resp.status_code == 404
    assert s3_backend.calls == []  # signer must not have been called


def test_read_url_method_less_signer_returns_409() -> None:
    """A signer without a ``method`` param (GCS-style) → 409 fallback, not a 500
    from calling it with an unexpected method= kwarg."""
    client = _make_client({"/workspace": _GcsLikeBackend()})

    resp = client.get("/read-url", params={"path": "/workspace/file.txt"})

    assert resp.status_code == 409


def test_read_url_directory_returns_409_without_signing(s3_backend: _SignableBackend) -> None:
    """sys_stat for a directory/mount root → 409 (regular files only); never
    sign the empty/prefix key of a non-file entry."""
    client = _make_client(
        {"/workspace": s3_backend},
        stat_result={"content_id": None, "size": 0, "is_directory": True},
    )

    resp = client.get("/read-url", params={"path": "/workspace/somedir"})

    assert resp.status_code == 409
    assert s3_backend.calls == []  # never signed a directory


def test_read_url_mount_root_entry_type_returns_409(s3_backend: _SignableBackend) -> None:
    """A DT_MOUNT stat (entry_type=2) that reports is_directory=False must STILL
    409 — checking is_directory alone would let a mount root through and sign its
    prefix key."""
    client = _make_client(
        {"/workspace": s3_backend},
        stat_result={"entry_type": 2, "is_directory": False, "content_id": None},
    )

    resp = client.get("/read-url", params={"path": "/workspace"})

    assert resp.status_code == 409
    assert s3_backend.calls == []  # never signed a mount root
