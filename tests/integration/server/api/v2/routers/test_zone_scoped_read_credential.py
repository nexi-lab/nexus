"""Zone-scoped /files/read executes with the node credential (3A).

The request-credentials middleware binds the HTTP caller's bearer for
user-file RPCs, but the kernel's API-key plane only knows the node key — a
user bearer forwarded on a zone-scoped read is rejected as ``invalid
credentials``. Python-side authz (key validity, zone binding, verified
delegation) still runs first; only the RPC credential changes. Root-zone
reads keep the ambient bearer exactly as before.
"""

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from nexus.lib.request_credentials import request_api_key
from nexus.server.api.v2.routers.async_files import create_async_files_router
from nexus.server.dependencies import get_auth_result
from nexus.server.middleware.request_credentials import RequestCredentialsMiddleware


@pytest.fixture()
def seen_credentials() -> list[str | None]:
    """RPC-time ambient credential values observed by fs.read."""
    return []


@pytest.fixture()
def capture_fs(seen_credentials: list[str | None]) -> Any:
    """Mock NexusFS whose read records request_api_key at call time."""

    class _Capture:
        def read(self, *args: Any, **kwargs: Any) -> bytes:
            seen_credentials.append(request_api_key.get())
            return b"zone content"

    return _Capture()


def _make_client(fs: Any, auth_result: dict[str, Any]) -> TestClient:
    app = FastAPI()
    app.include_router(create_async_files_router(nexus_fs=fs))
    app.add_middleware(RequestCredentialsMiddleware)
    app.dependency_overrides[get_auth_result] = lambda: auth_result
    return TestClient(app)


class TestZoneScopedReadCredential:
    def test_zone_scoped_read_swallows_the_ambient_bearer(
        self, capture_fs: Any, seen_credentials: list[str | None]
    ) -> None:
        client = _make_client(
            capture_fs,
            {
                "authenticated": True,
                "user_id": "test-user",
                "groups": [],
                "zone_id": "z1",
                "is_admin": False,
            },
        )
        resp = client.get(
            "/read",
            params={"path": "/zone/z1/a.txt", "zone": "z1"},
            headers={"Authorization": "Bearer sk-user-abc"},
        )
        assert resp.status_code == 200, resp.text
        # RPC 凭证为 channel node 凭证（contextvar 为 None 时 _credential 回落
        # self._auth_token），绝非转发 user bearer
        assert seen_credentials == [None]

    def test_root_zone_read_keeps_the_ambient_bearer(
        self, capture_fs: Any, seen_credentials: list[str | None]
    ) -> None:
        client = _make_client(
            capture_fs,
            {
                "authenticated": True,
                "user_id": "test-user",
                "groups": [],
                "zone_id": "root",
                "is_admin": False,
            },
        )
        resp = client.get(
            "/read",
            params={"path": "/a.txt"},
            headers={"Authorization": "Bearer sk-user-abc"},
        )
        assert resp.status_code == 200, resp.text
        assert seen_credentials == ["sk-user-abc"]
