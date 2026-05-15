from __future__ import annotations

from types import SimpleNamespace

import pytest

from nexus.contracts.exceptions import NexusPermissionError
from nexus.server.rpc.handlers import hub_admin


def test_hub_admin_list_requires_admin_before_operation(monkeypatch):
    called = False

    def fake_list(*args, **kwargs):
        nonlocal called
        called = True
        return {"tokens": []}

    monkeypatch.setattr(hub_admin.admin_ops, "list_hub_tokens", fake_list)

    with pytest.raises(NexusPermissionError):
        hub_admin.handle_hub_admin_token_list(
            SimpleNamespace(session_factory=lambda: None),
            SimpleNamespace(show_revoked=False),
            SimpleNamespace(is_admin=False, user_id="bob"),
        )

    assert called is False


def test_hub_admin_create_delegates_to_shared_ops(monkeypatch):
    calls = []

    def session_factory():
        return None

    def fake_create(session_factory, **kwargs):
        calls.append((session_factory, kwargs))
        return {"key_id": "nk_1", "token": "sk-1"}

    monkeypatch.setattr(hub_admin.admin_ops, "create_hub_token", fake_create)
    auth_provider = SimpleNamespace(session_factory=session_factory)

    result = hub_admin.handle_hub_admin_token_create(
        auth_provider,
        SimpleNamespace(
            name="ci",
            zones="eng:rw",
            zones_glob=None,
            admin=True,
            expires="7d",
            user_id="u1",
        ),
        SimpleNamespace(is_admin=True, user_id="admin"),
    )

    assert result == {"key_id": "nk_1", "token": "sk-1"}
    assert calls == [
        (
            session_factory,
            {
                "name": "ci",
                "zones_csv": "eng:rw",
                "zones_glob": None,
                "is_admin": True,
                "expires": "7d",
                "user_id": "u1",
            },
        )
    ]


def test_hub_admin_revoke_delegates_to_shared_ops(monkeypatch):
    calls = []

    def session_factory():
        return None

    def fake_revoke(session_factory, **kwargs):
        calls.append((session_factory, kwargs))
        return {
            "key_id": "nk_1",
            "name": "old",
            "message": "revoked old (nk_1). Effective within 60s (auth cache TTL).",
        }

    monkeypatch.setattr(hub_admin.admin_ops, "revoke_hub_token", fake_revoke)

    result = hub_admin.handle_hub_admin_token_revoke(
        SimpleNamespace(session_factory=session_factory),
        SimpleNamespace(identifier="old"),
        SimpleNamespace(is_admin=True, user_id="admin"),
    )

    assert result["key_id"] == "nk_1"
    assert calls == [(session_factory, {"identifier": "old"})]


def test_hub_admin_list_uses_env_db_when_static_auth_has_no_session_factory(monkeypatch):
    calls = []

    def fake_list(session_factory, **kwargs):
        calls.append((session_factory, kwargs))
        return {"tokens": []}

    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    monkeypatch.setattr(hub_admin.admin_ops, "list_hub_tokens", fake_list)

    result = hub_admin.handle_hub_admin_token_list(
        SimpleNamespace(),
        SimpleNamespace(show_revoked=False),
        SimpleNamespace(is_admin=True, user_id="admin"),
    )

    assert result == {"tokens": []}
    assert len(calls) == 1
    assert callable(calls[0][0])
    assert calls[0][1] == {"show_revoked": False}
