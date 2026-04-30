"""Unit tests for ``/api/v2/rebac/tuples`` router (Issue #3790 follow-up).

Tests run against a FastAPI stub with a mocked ReBACManager — no live
nexus needed. Auth paths covered:

- no Authorization header → 401
- non-admin token → 403
- admin token via auth_provider → 201
- ``NEXUS_APPROVALS_ADMIN_TOKEN`` is NOT a fallback here — regression
  guard so a leaked approvals token cannot write arbitrary ReBAC
  tuples → 403
- pydantic schema violations → 422
- ValueError from rebac_write → 400
- GET / DELETE happy paths
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from nexus.contracts.exceptions import NexusError
from nexus.server.api.v2.routers.rebac import router as rebac_router
from nexus.server.error_handlers import nexus_error_handler


class _FakeAuthProvider:
    """Stand-in for the auth provider on app.state."""

    def __init__(self, *, admin_tokens: dict[str, dict[str, Any]]) -> None:
        self._tokens = admin_tokens

    async def authenticate(self, token: str) -> Any:
        record = self._tokens.get(token)
        if record is None:
            return None
        return SimpleNamespace(
            authenticated=True,
            is_admin=record.get("is_admin", False),
            subject_type=record.get("subject_type", "user"),
            subject_id=record.get("subject_id", "u1"),
            zone_id=record.get("zone_id"),
            metadata={},
            inherit_permissions=True,
            zone_set=(),
            zone_perms=(),
        )


def _make_app(*, rebac_manager: Any, auth_provider: Any = None) -> FastAPI:
    """Build a FastAPI app with the ReBAC router and a stubbed app.state."""
    app = FastAPI()
    app.state.api_key = None
    app.state.auth_provider = auth_provider
    app.state.auth_cache_store = None
    app.state.rebac_manager = rebac_manager
    app.include_router(rebac_router)
    app.add_exception_handler(NexusError, nexus_error_handler)
    return app


def _client(app: FastAPI) -> TestClient:
    """TestClient with a non-loopback client host so open-access mode is
    NOT entered (resolve_auth treats loopback as open-access when no
    api_key/auth_provider is configured)."""
    return TestClient(app, base_url="http://example.com")


@pytest.fixture
def fake_rebac_manager() -> MagicMock:
    mgr = MagicMock()
    mgr.rebac_write.return_value = SimpleNamespace(
        tuple_id="tuple-abc",
        revision=1,
        consistency_token="ct-1",
    )
    mgr.rebac_list_tuples.return_value = [
        {
            "tuple_id": "tuple-abc",
            "subject_type": "user",
            "subject_id": "alice",
            "relation": "read",
            "object_type": "approvals",
            "object_id": "global",
            "zone_id": "root",
        },
    ]
    mgr.rebac_delete.return_value = True
    return mgr


_VALID_BODY: dict[str, Any] = {
    "subject_namespace": "user",
    "subject_id": "alice",
    "relation": "read",
    "object_namespace": "approvals",
    "object_id": "global",
    "zone_id": "root",
}


# ---------------------------------------------------------------------------
# Auth tests — POST
# ---------------------------------------------------------------------------


def test_post_without_auth_returns_401(fake_rebac_manager: MagicMock) -> None:
    auth = _FakeAuthProvider(admin_tokens={})
    app = _make_app(rebac_manager=fake_rebac_manager, auth_provider=auth)
    with _client(app) as client:
        resp = client.post("/api/v2/rebac/tuples", json=_VALID_BODY)
    assert resp.status_code == 401, resp.text
    fake_rebac_manager.rebac_write.assert_not_called()


def test_post_with_non_admin_returns_403(fake_rebac_manager: MagicMock) -> None:
    auth = _FakeAuthProvider(
        admin_tokens={"non-admin-token": {"is_admin": False, "subject_id": "alice"}}
    )
    app = _make_app(rebac_manager=fake_rebac_manager, auth_provider=auth)
    with _client(app) as client:
        resp = client.post(
            "/api/v2/rebac/tuples",
            json=_VALID_BODY,
            headers={"Authorization": "Bearer non-admin-token"},
        )
    assert resp.status_code == 403, resp.text
    fake_rebac_manager.rebac_write.assert_not_called()


def test_post_with_admin_via_auth_provider_succeeds(fake_rebac_manager: MagicMock) -> None:
    auth = _FakeAuthProvider(admin_tokens={"admin-tok": {"is_admin": True, "subject_id": "admin"}})
    app = _make_app(rebac_manager=fake_rebac_manager, auth_provider=auth)
    with _client(app) as client:
        resp = client.post(
            "/api/v2/rebac/tuples",
            json=_VALID_BODY,
            headers={"Authorization": "Bearer admin-tok"},
        )
    assert resp.status_code == 201, resp.text
    fake_rebac_manager.rebac_write.assert_called_once_with(
        subject=("user", "alice"),
        relation="read",
        object=("approvals", "global"),
        zone_id="root",
    )
    body = resp.json()
    assert body["tuple_id"] == "tuple-abc"
    assert body["revision"] == 1
    assert body["consistency_token"] == "ct-1"
    assert body["subject_id"] == "alice"


def test_post_with_approvals_admin_token_is_rejected(
    fake_rebac_manager: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression guard for #3790 follow-up security review:
    ``NEXUS_APPROVALS_ADMIN_TOKEN`` must NOT be admitted as HTTP admin
    here. Otherwise a leaked approvals token would be able to write
    arbitrary ReBAC tuples (e.g. grant itself further capabilities)."""
    monkeypatch.setenv("NEXUS_APPROVALS_ADMIN_TOKEN", "approvals-admin-secret")
    app = _make_app(rebac_manager=fake_rebac_manager, auth_provider=None)
    with _client(app) as client:
        resp = client.post(
            "/api/v2/rebac/tuples",
            json=_VALID_BODY,
            headers={"Authorization": "Bearer approvals-admin-secret"},
        )
    assert resp.status_code == 403, resp.text
    fake_rebac_manager.rebac_write.assert_not_called()


# ---------------------------------------------------------------------------
# Body validation
# ---------------------------------------------------------------------------


def test_post_missing_relation_returns_422(fake_rebac_manager: MagicMock) -> None:
    auth = _FakeAuthProvider(admin_tokens={"admin-tok": {"is_admin": True, "subject_id": "admin"}})
    app = _make_app(rebac_manager=fake_rebac_manager, auth_provider=auth)
    bad_body = {k: v for k, v in _VALID_BODY.items() if k != "relation"}
    with _client(app) as client:
        resp = client.post(
            "/api/v2/rebac/tuples",
            json=bad_body,
            headers={"Authorization": "Bearer admin-tok"},
        )
    assert resp.status_code == 422, resp.text
    fake_rebac_manager.rebac_write.assert_not_called()


def test_post_value_error_propagates_as_400(fake_rebac_manager: MagicMock) -> None:
    """Validation errors from rebac_write surface as 400."""
    fake_rebac_manager.rebac_write.side_effect = ValueError("invalid relation 'bogus'")
    auth = _FakeAuthProvider(admin_tokens={"admin-tok": {"is_admin": True, "subject_id": "admin"}})
    app = _make_app(rebac_manager=fake_rebac_manager, auth_provider=auth)
    with _client(app) as client:
        resp = client.post(
            "/api/v2/rebac/tuples",
            json=_VALID_BODY,
            headers={"Authorization": "Bearer admin-tok"},
        )
    assert resp.status_code == 400, resp.text
    fake_rebac_manager.rebac_write.assert_called_once()


def test_post_with_subject_relation_passes_3tuple(fake_rebac_manager: MagicMock) -> None:
    """When subject_relation is set, write_tuple should pass a 3-tuple subject."""
    auth = _FakeAuthProvider(admin_tokens={"admin-tok": {"is_admin": True, "subject_id": "admin"}})
    app = _make_app(rebac_manager=fake_rebac_manager, auth_provider=auth)
    body = {**_VALID_BODY, "subject_relation": "member"}
    with _client(app) as client:
        resp = client.post(
            "/api/v2/rebac/tuples",
            json=body,
            headers={"Authorization": "Bearer admin-tok"},
        )
    assert resp.status_code == 201, resp.text
    call_kwargs = fake_rebac_manager.rebac_write.call_args.kwargs
    assert call_kwargs["subject"] == ("user", "alice", "member")


# ---------------------------------------------------------------------------
# GET / DELETE happy paths
# ---------------------------------------------------------------------------


def test_get_tuples_admin(fake_rebac_manager: MagicMock) -> None:
    auth = _FakeAuthProvider(admin_tokens={"admin-tok": {"is_admin": True, "subject_id": "admin"}})
    app = _make_app(rebac_manager=fake_rebac_manager, auth_provider=auth)
    with _client(app) as client:
        resp = client.get(
            "/api/v2/rebac/tuples",
            params={
                "subject_namespace": "user",
                "subject_id": "alice",
                "relation": "read",
                "object_namespace": "approvals",
                "object_id": "global",
            },
            headers={"Authorization": "Bearer admin-tok"},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["count"] == 1
    assert body["tuples"][0]["subject_id"] == "alice"
    fake_rebac_manager.rebac_list_tuples.assert_called_once_with(
        subject=("user", "alice"),
        relation="read",
        object=("approvals", "global"),
    )


def test_get_tuples_no_filters_admin(fake_rebac_manager: MagicMock) -> None:
    """No filter arguments → list everything (subject=None, object=None)."""
    auth = _FakeAuthProvider(admin_tokens={"admin-tok": {"is_admin": True, "subject_id": "admin"}})
    app = _make_app(rebac_manager=fake_rebac_manager, auth_provider=auth)
    with _client(app) as client:
        resp = client.get(
            "/api/v2/rebac/tuples",
            headers={"Authorization": "Bearer admin-tok"},
        )
    assert resp.status_code == 200, resp.text
    fake_rebac_manager.rebac_list_tuples.assert_called_once_with(
        subject=None, relation=None, object=None
    )


def test_get_tuples_partial_subject_returns_400(fake_rebac_manager: MagicMock) -> None:
    auth = _FakeAuthProvider(admin_tokens={"admin-tok": {"is_admin": True, "subject_id": "admin"}})
    app = _make_app(rebac_manager=fake_rebac_manager, auth_provider=auth)
    with _client(app) as client:
        resp = client.get(
            "/api/v2/rebac/tuples",
            params={"subject_namespace": "user"},
            headers={"Authorization": "Bearer admin-tok"},
        )
    assert resp.status_code == 400, resp.text
    fake_rebac_manager.rebac_list_tuples.assert_not_called()


def test_get_tuples_no_auth_returns_401(fake_rebac_manager: MagicMock) -> None:
    auth = _FakeAuthProvider(admin_tokens={})
    app = _make_app(rebac_manager=fake_rebac_manager, auth_provider=auth)
    with _client(app) as client:
        resp = client.get("/api/v2/rebac/tuples")
    assert resp.status_code == 401, resp.text


def test_delete_tuple_admin(fake_rebac_manager: MagicMock) -> None:
    auth = _FakeAuthProvider(admin_tokens={"admin-tok": {"is_admin": True, "subject_id": "admin"}})
    app = _make_app(rebac_manager=fake_rebac_manager, auth_provider=auth)
    with _client(app) as client:
        resp = client.request(
            "DELETE",
            "/api/v2/rebac/tuples",
            json=_VALID_BODY,
            headers={"Authorization": "Bearer admin-tok"},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["deleted"] == 1
    fake_rebac_manager.rebac_delete.assert_called_once_with("tuple-abc")


def test_delete_tuple_zone_filter_skips_other_zone(fake_rebac_manager: MagicMock) -> None:
    """If the matched tuple is in a different zone, don't delete it."""
    fake_rebac_manager.rebac_list_tuples.return_value = [
        {
            "tuple_id": "tuple-other-zone",
            "subject_type": "user",
            "subject_id": "alice",
            "relation": "read",
            "object_type": "approvals",
            "object_id": "global",
            "zone_id": "other-zone",
        },
    ]
    auth = _FakeAuthProvider(admin_tokens={"admin-tok": {"is_admin": True, "subject_id": "admin"}})
    app = _make_app(rebac_manager=fake_rebac_manager, auth_provider=auth)
    with _client(app) as client:
        resp = client.request(
            "DELETE",
            "/api/v2/rebac/tuples",
            json=_VALID_BODY,
            headers={"Authorization": "Bearer admin-tok"},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["deleted"] == 0
    fake_rebac_manager.rebac_delete.assert_not_called()


def test_delete_tuple_no_auth_returns_401(fake_rebac_manager: MagicMock) -> None:
    auth = _FakeAuthProvider(admin_tokens={})
    app = _make_app(rebac_manager=fake_rebac_manager, auth_provider=auth)
    with _client(app) as client:
        resp = client.request("DELETE", "/api/v2/rebac/tuples", json=_VALID_BODY)
    assert resp.status_code == 401, resp.text


def test_post_when_rebac_manager_unavailable_returns_503() -> None:
    """If app.state.rebac_manager is None, return 503."""
    app = FastAPI()
    app.state.api_key = None
    app.state.auth_provider = _FakeAuthProvider(
        admin_tokens={"admin-tok": {"is_admin": True, "subject_id": "admin"}}
    )
    app.state.auth_cache_store = None
    app.state.rebac_manager = None
    app.include_router(rebac_router)
    with _client(app) as client:
        resp = client.post(
            "/api/v2/rebac/tuples",
            json=_VALID_BODY,
            headers={"Authorization": "Bearer admin-tok"},
        )
    assert resp.status_code == 503, resp.text
