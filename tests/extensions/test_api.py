"""Tests for the /api/v2/extensions HTTP endpoint."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from nexus.extensions.store import reset_store
from nexus.server.api.core.extensions import router as extensions_router
from nexus.server.dependencies import require_admin, require_auth


@pytest.fixture(autouse=True)
def _fresh_store():
    reset_store()
    yield
    reset_store()


def _seed(monkeypatch, manifests):
    from nexus.extensions import store as store_mod

    fake = store_mod.ManifestStore()
    for m in manifests:
        fake._register(m, source="test")
    monkeypatch.setattr(store_mod, "_STORE", fake)


def _client(*, admin: bool = True, authed: bool = True) -> TestClient:
    """Build a client with the auth dependency stubbed.

    admin=True (default) — passes both require_auth and require_admin.
    admin=False, authed=True — passes auth but fails admin (expect 403).
    authed=False — leaves the real dependencies, expect 401 on every request.
    """
    app = FastAPI()
    app.include_router(extensions_router)
    if authed:
        fake_auth = {"authenticated": True, "is_admin": admin}
        app.dependency_overrides[require_auth] = lambda: fake_auth
        if admin:
            app.dependency_overrides[require_admin] = lambda: fake_auth
    return TestClient(app)


def test_list_returns_all(monkeypatch, all_manifests):
    _seed(monkeypatch, all_manifests)
    resp = _client().get("/api/v2/extensions")
    assert resp.status_code == 200
    names = {m["name"] for m in resp.json()}
    assert names == {"hn", "search", "koi"}


def test_list_filtered_by_kind(monkeypatch, all_manifests):
    _seed(monkeypatch, all_manifests)
    resp = _client().get("/api/v2/extensions", params={"kind": "connector"})
    assert resp.status_code == 200
    payload = resp.json()
    assert [m["name"] for m in payload] == ["hn"]


def test_list_rejects_unknown_kind(monkeypatch, all_manifests):
    _seed(monkeypatch, all_manifests)
    resp = _client().get("/api/v2/extensions", params={"kind": "nonsense"})
    assert resp.status_code == 400


def test_list_available_only(monkeypatch):
    from nexus.extensions.manifest import PluginManifest

    _seed(
        monkeypatch,
        [
            PluginManifest(name="ok", module="m", factory="F", import_probes=("sys",)),
            PluginManifest(
                name="missing",
                module="m",
                factory="F",
                import_probes=("nonexistent_xyz",),
            ),
        ],
    )
    resp = _client().get("/api/v2/extensions", params={"available_only": "true"})
    assert resp.status_code == 200
    names = {m["name"] for m in resp.json()}
    assert names == {"ok"}


def test_kinds_endpoint():
    resp = _client().get("/api/v2/extensions/kinds")
    assert resp.status_code == 200
    assert set(resp.json()) == {"connector", "brick", "plugin"}


def test_info_returns_manifest(monkeypatch, all_manifests):
    _seed(monkeypatch, all_manifests)
    resp = _client().get("/api/v2/extensions/connector/hn")
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "hn"
    assert body["kind"] == "connector"


def test_info_unknown_returns_404(monkeypatch, all_manifests):
    _seed(monkeypatch, all_manifests)
    resp = _client().get("/api/v2/extensions/connector/ghost")
    assert resp.status_code == 404


def test_check_endpoint(monkeypatch):
    from nexus.extensions.manifest import PluginManifest

    _seed(
        monkeypatch,
        [PluginManifest(name="ok", module="m", factory="F", import_probes=("sys",))],
    )
    resp = _client().get("/api/v2/extensions/plugin/ok/check")
    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is True
    assert body["name"] == "ok"
    assert body["kind"] == "plugin"


def test_unauthenticated_is_rejected(monkeypatch, all_manifests):
    """Unauthenticated callers must not be able to enumerate extensions.

    The exact code (401 vs 403) depends on whether the test app's
    get_auth_result returns None or a non-admin default, but either way
    the request must be blocked.
    """
    _seed(monkeypatch, all_manifests)
    resp = _client(authed=False).get("/api/v2/extensions")
    assert resp.status_code in {401, 403}, (
        f"unauthenticated request must be rejected, got {resp.status_code}"
    )


def test_non_admin_returns_403(monkeypatch, all_manifests):
    """Authenticated non-admin callers must not be able to enumerate extensions."""
    _seed(monkeypatch, all_manifests)
    resp = _client(admin=False).get("/api/v2/extensions")
    assert resp.status_code == 403
