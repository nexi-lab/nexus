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


class AssertingRunner:
    def __init__(self, fs: "StreamFs") -> None:
        self._fs = fs

    async def call(self, work):
        self._fs.in_runner = True
        try:
            return await work()
        finally:
            self._fs.in_runner = False


class StreamRegistry(RecordingRegistry):
    def __init__(self, fs: "StreamFs") -> None:
        super().__init__()
        self._fs = fs

    def runner_for(self, zone_id: str) -> object:
        self.requested.append(zone_id)
        return AssertingRunner(self._fs)


class StreamMeta:
    size = 5
    content_id = "etag"
    mime_type = "text/plain"


class StreamFs:
    def __init__(self) -> None:
        self.in_runner = False
        self.sys_read_inside_runner = False
        self.read_range_inside_runner = False

    def sys_stat(self, path: str, *, context: object) -> StreamMeta:
        return StreamMeta()

    def sys_read(self, path: str, *, context: object) -> bytes:
        self.sys_read_inside_runner = self.in_runner
        return b"hello"

    def read_range(self, path: str, start: int, end: int, *, context: object) -> bytes:
        self.read_range_inside_runner = self.in_runner
        return b"hello"[start : end + 1]


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


def test_async_files_batch_read_rejects_mixed_zone_request_before_runner() -> None:
    registry = RecordingRegistry()
    fs = MagicMock()
    app = FastAPI()
    app.include_router(create_async_files_router(nexus_fs=fs, get_zone_registry=lambda: registry))
    app.dependency_overrides[get_auth_result] = _auth

    with TestClient(app) as client:
        response = client.post(
            "/batch/read",
            json={"paths": ["/zone/eng/a.txt", "/zone/legal/b.txt"]},
        )

    assert response.status_code == 400
    assert response.json()["detail"] == "Mixed-zone operations must be submitted per zone"
    assert registry.requested == []


def test_async_files_stream_reads_full_content_inside_zone_runner() -> None:
    fs = StreamFs()
    registry = StreamRegistry(fs)
    app = FastAPI()
    app.include_router(create_async_files_router(nexus_fs=fs, get_zone_registry=lambda: registry))
    app.dependency_overrides[get_auth_result] = _auth

    with TestClient(app) as client:
        response = client.get("/stream", params={"path": "/docs/a.txt"})

    assert response.status_code == 200
    assert response.content == b"hello"
    assert registry.requested == ["eng"]
    assert fs.sys_read_inside_runner is True


def test_async_files_stream_reads_range_inside_zone_runner() -> None:
    fs = StreamFs()
    registry = StreamRegistry(fs)
    app = FastAPI()
    app.include_router(create_async_files_router(nexus_fs=fs, get_zone_registry=lambda: registry))
    app.dependency_overrides[get_auth_result] = _auth

    with TestClient(app) as client:
        response = client.get(
            "/stream",
            params={"path": "/docs/a.txt"},
            headers={"Range": "bytes=1-3"},
        )

    assert response.status_code == 206
    assert response.content == b"ell"
    assert registry.requested == ["eng"]
    assert fs.read_range_inside_runner is True
