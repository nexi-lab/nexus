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
from nexus.server.api.v2.routers.rebac import (
    _normalize_file_object_id,
)
from nexus.server.api.v2.routers.rebac import (
    router as rebac_router,
)
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
        relation="read",
        subject_type="user",
        subject_id="alice",
        object_type="approvals",
        object_id="global",
        zone_id=None,
    )


def test_get_tuples_no_filters_admin(fake_rebac_manager: MagicMock) -> None:
    """No filter arguments → list everything (all filter kwargs None)."""
    auth = _FakeAuthProvider(admin_tokens={"admin-tok": {"is_admin": True, "subject_id": "admin"}})
    app = _make_app(rebac_manager=fake_rebac_manager, auth_provider=auth)
    with _client(app) as client:
        resp = client.get(
            "/api/v2/rebac/tuples",
            headers={"Authorization": "Bearer admin-tok"},
        )
    assert resp.status_code == 200, resp.text
    fake_rebac_manager.rebac_list_tuples.assert_called_once_with(
        relation=None,
        subject_type=None,
        subject_id=None,
        object_type=None,
        object_id=None,
        zone_id=None,
    )


def test_get_tuples_subject_id_alone_filters(fake_rebac_manager: MagicMock) -> None:
    """Issue #4242: ``?subject_id=admin`` alone must apply the filter, not 400.

    Operators debugging a permission denial want to grep by subject_id
    regardless of subject_type — the previous 400 was unhelpful and the
    unfiltered list becomes useless on deployments with many tuples.
    """
    auth = _FakeAuthProvider(admin_tokens={"admin-tok": {"is_admin": True, "subject_id": "admin"}})
    app = _make_app(rebac_manager=fake_rebac_manager, auth_provider=auth)
    with _client(app) as client:
        resp = client.get(
            "/api/v2/rebac/tuples",
            params={"subject_id": "admin"},
            headers={"Authorization": "Bearer admin-tok"},
        )
    assert resp.status_code == 200, resp.text
    fake_rebac_manager.rebac_list_tuples.assert_called_once_with(
        relation=None,
        subject_type=None,
        subject_id="admin",
        object_type=None,
        object_id=None,
        zone_id=None,
    )


def test_get_tuples_object_id_alone_filters(fake_rebac_manager: MagicMock) -> None:
    """Issue #4242: ``?object_id=/x/y.md`` alone is also honored."""
    auth = _FakeAuthProvider(admin_tokens={"admin-tok": {"is_admin": True, "subject_id": "admin"}})
    app = _make_app(rebac_manager=fake_rebac_manager, auth_provider=auth)
    with _client(app) as client:
        resp = client.get(
            "/api/v2/rebac/tuples",
            params={"object_id": "/x/y.md"},
            headers={"Authorization": "Bearer admin-tok"},
        )
    assert resp.status_code == 200, resp.text
    fake_rebac_manager.rebac_list_tuples.assert_called_once_with(
        relation=None,
        subject_type=None,
        subject_id=None,
        object_type=None,
        object_id="/x/y.md",
        zone_id=None,
    )


def test_get_tuples_zone_id_filter(fake_rebac_manager: MagicMock) -> None:
    """``?zone_id=root`` narrows results to a single zone."""
    auth = _FakeAuthProvider(admin_tokens={"admin-tok": {"is_admin": True, "subject_id": "admin"}})
    app = _make_app(rebac_manager=fake_rebac_manager, auth_provider=auth)
    with _client(app) as client:
        resp = client.get(
            "/api/v2/rebac/tuples",
            params={"zone_id": "root"},
            headers={"Authorization": "Bearer admin-tok"},
        )
    assert resp.status_code == 200, resp.text
    fake_rebac_manager.rebac_list_tuples.assert_called_once_with(
        relation=None,
        subject_type=None,
        subject_id=None,
        object_type=None,
        object_id=None,
        zone_id="root",
    )


def test_get_tuples_subject_namespace_alone_filters(
    fake_rebac_manager: MagicMock,
) -> None:
    """Issue #4242: previously a partial subject filter returned 400.
    Now it is honored — ``?subject_namespace=user`` lists all
    user-type subjects.
    """
    auth = _FakeAuthProvider(admin_tokens={"admin-tok": {"is_admin": True, "subject_id": "admin"}})
    app = _make_app(rebac_manager=fake_rebac_manager, auth_provider=auth)
    with _client(app) as client:
        resp = client.get(
            "/api/v2/rebac/tuples",
            params={"subject_namespace": "user"},
            headers={"Authorization": "Bearer admin-tok"},
        )
    assert resp.status_code == 200, resp.text
    fake_rebac_manager.rebac_list_tuples.assert_called_once_with(
        relation=None,
        subject_type="user",
        subject_id=None,
        object_type=None,
        object_id=None,
        zone_id=None,
    )


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
    # Round-10 review (codex HIGH): the lookup must pass
    # subject_relation + zone_id to the manager so a parallel
    # userset-as-subject tuple sharing the same (subject, relation,
    # object, zone) is NOT incidentally deleted.
    call_kwargs = fake_rebac_manager.rebac_list_tuples.call_args.kwargs
    assert call_kwargs["subject_relation"] is None, (
        "DELETE without subject_relation in body must request "
        "direct-only deletion (codex round-10 HIGH)"
    )
    assert call_kwargs["zone_id"] == "root"


def test_delete_tuple_with_subject_relation_targets_userset(
    fake_rebac_manager: MagicMock,
) -> None:
    """Round-10 review: when body.subject_relation is set, the lookup
    targets EXACTLY that userset-as-subject shape — a sibling direct
    tuple sharing (subject, relation, object, zone) must NOT be
    incidentally deleted."""
    auth = _FakeAuthProvider(admin_tokens={"admin-tok": {"is_admin": True, "subject_id": "admin"}})
    app = _make_app(rebac_manager=fake_rebac_manager, auth_provider=auth)
    body = {**_VALID_BODY, "subject_relation": "member"}
    with _client(app) as client:
        resp = client.request(
            "DELETE",
            "/api/v2/rebac/tuples",
            json=body,
            headers={"Authorization": "Bearer admin-tok"},
        )
    assert resp.status_code == 200, resp.text
    call_kwargs = fake_rebac_manager.rebac_list_tuples.call_args.kwargs
    assert call_kwargs["subject_relation"] == "member"
    assert call_kwargs["zone_id"] == "root"


def test_delete_tuple_zone_filter_at_manager(fake_rebac_manager: MagicMock) -> None:
    """Round-10: zone filtering now happens at the manager via the
    ``zone_id=`` kwarg, not as a post-filter on the router side. The
    test verifies the router DOES pass zone_id through so the manager
    can do its SQL-level filtering. Empty manager response → deleted=0.
    """
    # Manager returns empty (simulates SQL-level zone filter excluding
    # the only candidate match).
    fake_rebac_manager.rebac_list_tuples.return_value = []
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
    # Verify the manager was given the zone filter.
    call_kwargs = fake_rebac_manager.rebac_list_tuples.call_args.kwargs
    assert call_kwargs["zone_id"] == "root"


def test_delete_tuple_no_auth_returns_401(fake_rebac_manager: MagicMock) -> None:
    auth = _FakeAuthProvider(admin_tokens={})
    app = _make_app(rebac_manager=fake_rebac_manager, auth_provider=auth)
    with _client(app) as client:
        resp = client.request("DELETE", "/api/v2/rebac/tuples", json=_VALID_BODY)
    assert resp.status_code == 401, resp.text


# ---------------------------------------------------------------------------
# Issue #4239: wildcard / glob object_id normalization
# ---------------------------------------------------------------------------


class TestNormalizeFileObjectId:
    """Pure helper tests — collapse shell-style globs to a directory path."""

    def test_double_star_collapses(self) -> None:
        assert _normalize_file_object_id("file", "/workspaces/ws1/**") == "/workspaces/ws1"

    def test_single_star_rejected(self) -> None:
        """Round-5 review (codex HIGH): ``/*`` previously collapsed to
        the same directory tuple as ``/**``, silently granting the
        entire subtree even though shell ``/*`` is one-level-only.
        Now raises so the caller returns 400 — operator must pick
        ``/**`` explicitly or list exact paths.
        """
        from nexus.server.api.v2.routers.rebac import _WildcardSemanticError

        with pytest.raises(_WildcardSemanticError, match="single-level glob"):
            _normalize_file_object_id("file", "/workspaces/ws1/*")

    def test_trailing_slash_collapses(self) -> None:
        assert _normalize_file_object_id("file", "/workspaces/ws1/") == "/workspaces/ws1"

    def test_root_double_star(self) -> None:
        assert _normalize_file_object_id("file", "/**") == "/"

    def test_root_single_star_rejected(self) -> None:
        """Round-5 review: same as nested ``/*`` — would silently grant
        every file in every zone."""
        from nexus.server.api.v2.routers.rebac import _WildcardSemanticError

        with pytest.raises(_WildcardSemanticError):
            _normalize_file_object_id("file", "/*")

    def test_exact_path_unchanged(self) -> None:
        assert _normalize_file_object_id("file", "/workspaces/ws1/a.md") == "/workspaces/ws1/a.md"

    def test_mixed_globs_rejected(self) -> None:
        """``/a/**/*`` mixes recursive + single-level — reject since the
        ``/*`` semantic cannot be honored."""
        from nexus.server.api.v2.routers.rebac import _WildcardSemanticError

        with pytest.raises(_WildcardSemanticError):
            _normalize_file_object_id("file", "/a/**/*")

    def test_non_file_namespace_passthrough(self) -> None:
        """Capabilities like ``("approvals", "global*")`` must not be mangled."""
        assert _normalize_file_object_id("approvals", "global*") == "global*"
        assert _normalize_file_object_id("zone", "/z/**") == "/z/**"

    def test_empty_string_passthrough(self) -> None:
        assert _normalize_file_object_id("file", "") == ""


@pytest.fixture
def fake_rebac_manager_file() -> MagicMock:
    """Variant of fake_rebac_manager that returns a file-scoped tuple list."""
    mgr = MagicMock()
    mgr.rebac_write.return_value = SimpleNamespace(
        tuple_id="tuple-file",
        revision=2,
        consistency_token="ct-2",
    )
    mgr.rebac_list_tuples.return_value = [
        {
            "tuple_id": "tuple-file",
            "subject_type": "user",
            "subject_id": "admin",
            "relation": "read",
            "object_type": "file",
            "object_id": "/workspaces/ws1",
            "zone_id": "root",
        },
    ]
    mgr.rebac_delete.return_value = True
    return mgr


def _file_body(object_id: str) -> dict[str, Any]:
    return {
        "subject_namespace": "user",
        "subject_id": "admin",
        "relation": "read",
        "object_namespace": "file",
        "object_id": object_id,
        "zone_id": "root",
    }


@pytest.mark.parametrize(
    ("input_object_id", "expected_stored"),
    [
        ("/workspaces/ws1/**", "/workspaces/ws1"),
        ("/workspaces/ws1/", "/workspaces/ws1"),
        ("/**", "/"),
        ("/workspaces/ws1/a.md", "/workspaces/ws1/a.md"),
    ],
)
def test_post_wildcard_object_id_normalized(
    fake_rebac_manager_file: MagicMock,
    input_object_id: str,
    expected_stored: str,
) -> None:
    """POST collapses RECURSIVE ``/**`` and trailing ``/`` so the existing
    ancestor-walk machinery grants every descendant (Issue #4239).
    Round-5: ``/*`` is rejected — see test_post_single_star_returns_400."""
    auth = _FakeAuthProvider(admin_tokens={"admin-tok": {"is_admin": True, "subject_id": "admin"}})
    app = _make_app(rebac_manager=fake_rebac_manager_file, auth_provider=auth)
    with _client(app) as client:
        resp = client.post(
            "/api/v2/rebac/tuples",
            json=_file_body(input_object_id),
            headers={"Authorization": "Bearer admin-tok"},
        )
    assert resp.status_code == 201, resp.text
    call_kwargs = fake_rebac_manager_file.rebac_write.call_args.kwargs
    assert call_kwargs["object"] == ("file", expected_stored)
    body = resp.json()
    assert body["object_id"] == expected_stored
    assert body["object_id_input"] == input_object_id


def test_post_single_star_returns_400(fake_rebac_manager_file: MagicMock) -> None:
    """Round-5 review (codex HIGH): a ``/*`` object_id must return 400
    rather than silently collapsing to the parent directory tuple
    (which would grant the whole subtree). Operators should pick
    ``/**`` for recursive or list exact paths.
    """
    auth = _FakeAuthProvider(admin_tokens={"admin-tok": {"is_admin": True, "subject_id": "admin"}})
    app = _make_app(rebac_manager=fake_rebac_manager_file, auth_provider=auth)
    with _client(app) as client:
        resp = client.post(
            "/api/v2/rebac/tuples",
            json=_file_body("/workspaces/ws1/*"),
            headers={"Authorization": "Bearer admin-tok"},
        )
    assert resp.status_code == 400, resp.text
    assert "single-level glob" in resp.json()["detail"].lower()
    fake_rebac_manager_file.rebac_write.assert_not_called()


def test_post_non_file_namespace_not_normalized(fake_rebac_manager: MagicMock) -> None:
    """Globs in non-file capability ids must not be mangled."""
    auth = _FakeAuthProvider(admin_tokens={"admin-tok": {"is_admin": True, "subject_id": "admin"}})
    app = _make_app(rebac_manager=fake_rebac_manager, auth_provider=auth)
    body = {**_VALID_BODY, "object_namespace": "approvals", "object_id": "global*"}
    with _client(app) as client:
        resp = client.post(
            "/api/v2/rebac/tuples",
            json=body,
            headers={"Authorization": "Bearer admin-tok"},
        )
    assert resp.status_code == 201, resp.text
    call_kwargs = fake_rebac_manager.rebac_write.call_args.kwargs
    assert call_kwargs["object"] == ("approvals", "global*")


def test_delete_normalizes_to_match_post(fake_rebac_manager_file: MagicMock) -> None:
    """DELETE with ``/workspaces/ws1/**`` deletes the tuple POST wrote at
    ``/workspaces/ws1`` — otherwise operators can't clean up by symmetry."""
    auth = _FakeAuthProvider(admin_tokens={"admin-tok": {"is_admin": True, "subject_id": "admin"}})
    app = _make_app(rebac_manager=fake_rebac_manager_file, auth_provider=auth)
    with _client(app) as client:
        resp = client.request(
            "DELETE",
            "/api/v2/rebac/tuples",
            json=_file_body("/workspaces/ws1/**"),
            headers={"Authorization": "Bearer admin-tok"},
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["deleted"] == 1
    fake_rebac_manager_file.rebac_list_tuples.assert_called_once_with(
        subject=("user", "admin"),
        relation="read",
        object=("file", "/workspaces/ws1"),
        subject_relation=None,
        zone_id="root",
    )


def test_get_normalizes_query_object_id(fake_rebac_manager_file: MagicMock) -> None:
    """GET ?object_id=/workspaces/ws1/** finds the tuple POST stored."""
    auth = _FakeAuthProvider(admin_tokens={"admin-tok": {"is_admin": True, "subject_id": "admin"}})
    app = _make_app(rebac_manager=fake_rebac_manager_file, auth_provider=auth)
    with _client(app) as client:
        resp = client.get(
            "/api/v2/rebac/tuples",
            params={
                "subject_namespace": "user",
                "subject_id": "admin",
                "relation": "read",
                "object_namespace": "file",
                "object_id": "/workspaces/ws1/**",
            },
            headers={"Authorization": "Bearer admin-tok"},
        )
    assert resp.status_code == 200, resp.text
    fake_rebac_manager_file.rebac_list_tuples.assert_called_once_with(
        relation="read",
        subject_type="user",
        subject_id="admin",
        object_type="file",
        object_id="/workspaces/ws1",
        zone_id=None,
    )


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
