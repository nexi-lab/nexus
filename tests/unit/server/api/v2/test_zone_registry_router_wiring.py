from unittest.mock import MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from nexus.server.api.v2.routers.async_files import create_async_files_router
from nexus.server.api.v2.routers.batch import create_batch_router
from nexus.server.dependencies import get_auth_result


class RecordingRegistry:
    def __init__(self) -> None:
        self.requested: list[str] = []

    def runner_for(self, zone_id: str) -> object:
        self.requested.append(zone_id)
        return InlineRunner()


class InlineRunner:
    async def call(self, work):
        return await work()


def _auth() -> dict[str, object]:
    return {
        "authenticated": True,
        "subject_id": "alice",
        "subject_type": "user",
        "zone_id": "eng",
        "zone_perms": [["eng", "rw"]],
        "is_admin": False,
    }


def test_async_files_router_uses_zone_registry_getter_for_list() -> None:
    registry = RecordingRegistry()
    fs = MagicMock()
    fs.sys_readdir.return_value = []
    app = FastAPI()
    app.include_router(create_async_files_router(nexus_fs=fs, get_zone_registry=lambda: registry))
    app.dependency_overrides[get_auth_result] = _auth

    with TestClient(app) as client:
        response = client.get("/list", params={"path": "/docs"})

    assert response.status_code == 200
    assert registry.requested == ["eng"]


def test_batch_router_uses_zone_registry_getter_for_path_operation() -> None:
    registry = RecordingRegistry()
    fs = MagicMock()
    fs.read.return_value = b"hello"
    app = FastAPI()
    app.include_router(
        create_batch_router(
            nexus_fs=fs,
            get_zone_registry=lambda: registry,
            get_context_override=lambda: MagicMock(zone_id="eng", groups=[], is_admin=False),
        ),
        prefix="/api/v2",
    )

    with TestClient(app) as client:
        response = client.post(
            "/api/v2/batch",
            json={"operations": [{"op": "read", "path": "/docs/a.txt"}]},
        )

    assert response.status_code == 200
    assert registry.requested == ["eng"]
