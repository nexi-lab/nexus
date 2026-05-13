from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from nexus.server.api.core.debug import router


def test_vfs_initialize_endpoint_returns_capabilities() -> None:
    app = FastAPI()
    app.include_router(router)
    app.state.api_key = None
    app.state.auth_provider = None
    app.state.auth_cache_store = None
    app.state.nexus_fs = SimpleNamespace(
        _kernel=SimpleNamespace(get_mount_points=lambda: ["/root"])
    )
    app.state.exposed_methods = {"grep": object(), "glob": object()}

    client = TestClient(app)
    response = client.get("/api/vfs/initialize")

    assert response.status_code == 200
    body = response.json()
    assert body["server_name"] == "nexus"
    assert body["capabilities"]["commands"]["grep"]["supported"] is True
    assert "/" in body["capabilities"]["backends"]
