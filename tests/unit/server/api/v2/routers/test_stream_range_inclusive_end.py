"""``GET /files/stream`` with a ``Range`` header must return exactly the
bytes it advertises.

``build_range_response`` passes the RFC 9110 range with an INCLUSIVE end
(``bytes=2-6`` → ``end=6``, ``Content-Length: 5``) to the route's generator,
while ``NexusFS.read_range`` takes an EXCLUSIVE end.  The route used to pass
``end`` straight through, so every 206 body was one byte short of its
``Content-Length`` (uvicorn logged "Response content shorter than
Content-Length"; the Railway edge dropped the body entirely).
"""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("fastapi")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

CONTENT = b"0123456789abcdef"

_AUTH: dict[str, Any] = {
    "authenticated": True,
    "is_admin": True,
    "subject_type": "user",
    "subject_id": "admin",
    "zone_id": None,
    "inherit_permissions": True,
}


class _RangeFS:
    def __init__(self) -> None:
        self.read_range_calls: list[tuple[int, int]] = []

    def sys_stat(self, path: str, **_: Any) -> dict[str, Any]:
        return {
            "path": path,
            "content_id": "cid-1",
            "version": 1,
            "size": len(CONTENT),
            "mime_type": "application/octet-stream",
            "entry_type": 0,
        }

    def sys_read(self, path: str, **_: Any) -> bytes:
        return CONTENT

    def read_range(self, path: str, start: int, end: int, **_: Any) -> bytes:
        # Exclusive end, like NexusFS.read_range.
        self.read_range_calls.append((start, end))
        return CONTENT[start:end]


def _client(fs: _RangeFS) -> TestClient:
    from nexus.server.api.v2.routers.async_files import create_async_files_router
    from nexus.server.dependencies import get_auth_result, require_auth

    app = FastAPI()
    app.state.search_daemon = None
    app.dependency_overrides[get_auth_result] = lambda: _AUTH
    app.dependency_overrides[require_auth] = lambda: _AUTH
    app.include_router(create_async_files_router(nexus_fs=fs), prefix="/api/v2/files")
    return TestClient(app)


@pytest.mark.parametrize(
    ("range_header", "expected"),
    [
        ("bytes=0-9", CONTENT[0:10]),
        ("bytes=2-6", CONTENT[2:7]),
        ("bytes=10-", CONTENT[10:]),
        ("bytes=-4", CONTENT[-4:]),
        ("bytes=15-15", CONTENT[15:16]),
    ],
)
def test_range_body_matches_content_length(range_header: str, expected: bytes) -> None:
    fs = _RangeFS()
    with _client(fs) as client:
        response = client.get(
            "/api/v2/files/stream",
            params={"path": "/d/a.bin"},
            headers={"Range": range_header},
        )
    assert response.status_code == 206, response.text
    assert response.content == expected
    assert int(response.headers["Content-Length"]) == len(expected)
    # The route must have asked read_range for the inclusive end + 1.
    assert fs.read_range_calls, "read_range was never called"
    start, end_exclusive = fs.read_range_calls[-1]
    assert end_exclusive - start == len(expected)


def test_full_read_without_range_is_unchanged() -> None:
    fs = _RangeFS()
    with _client(fs) as client:
        response = client.get("/api/v2/files/stream", params={"path": "/d/a.bin"})
    assert response.status_code == 200, response.text
    assert response.content == CONTENT
    assert fs.read_range_calls == []
