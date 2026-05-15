"""Verifies authenticate_api_key() consults AuthIdentityCache (#3779)."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from nexus.bricks.mcp import auth_bridge, auth_cache


@pytest.fixture(autouse=True)
def reset_cache():
    auth_cache._reset_singleton_for_tests()
    yield
    auth_cache._reset_singleton_for_tests()


def _mk_auth_result(subject_id: str = "u", zone_id: str = "z") -> Any:
    result = MagicMock()
    result.subject_id = subject_id
    result.zone_id = zone_id
    result.is_admin = False
    result.authenticated = True
    result.subject_type = "user"
    result.agent_generation = None
    result.inherit_permissions = None
    return result


def test_first_call_invokes_provider_and_caches():
    provider = MagicMock()
    provider.authenticate = MagicMock(return_value=_mk_auth_result())

    out1 = auth_bridge.authenticate_api_key(provider, "sk-zone_user_id_abc")
    out2 = auth_bridge.authenticate_api_key(provider, "sk-zone_user_id_abc")

    assert out1 is not None
    assert out2 is not None
    assert provider.authenticate.call_count == 1


def test_failed_auth_not_cached():
    provider = MagicMock()
    provider.authenticate = MagicMock(return_value=None)

    auth_bridge.authenticate_api_key(provider, "sk-bad_key_here_xyz")
    auth_bridge.authenticate_api_key(provider, "sk-bad_key_here_xyz")

    assert provider.authenticate.call_count == 2


def test_different_keys_cached_independently():
    provider = MagicMock()
    provider.authenticate = MagicMock(side_effect=lambda k: _mk_auth_result(subject_id=k[-3:]))

    auth_bridge.authenticate_api_key(provider, "sk-zone_user_id_aaa")
    auth_bridge.authenticate_api_key(provider, "sk-zone_user_id_bbb")
    auth_bridge.authenticate_api_key(provider, "sk-zone_user_id_aaa")

    assert provider.authenticate.call_count == 2


def test_cache_hit_preserves_authenticated_flag():
    """Regression: cache hit must expose `.authenticated` so that
    `resolve_mcp_operation_context` does not silently fail closed on
    warm-cache paths.
    """
    provider = MagicMock()
    first = MagicMock()
    first.subject_id = "user-1"
    first.zone_id = "zone-a"
    first.is_admin = False
    first.authenticated = True
    first.subject_type = "user"
    first.agent_generation = None
    first.inherit_permissions = None
    provider.authenticate = MagicMock(return_value=first)

    out1 = auth_bridge.authenticate_api_key(provider, "sk-z_u_id_abc")
    out2 = auth_bridge.authenticate_api_key(provider, "sk-z_u_id_abc")

    assert getattr(out1, "authenticated", False) is True
    assert getattr(out2, "authenticated", False) is True
    assert out2.subject_type == "user"
    assert out2.subject_id == "user-1"
    assert out2.zone_id == "zone-a"
    assert provider.authenticate.call_count == 1


def test_explicit_unauthenticated_not_cached():
    """auth_provider returning a result with authenticated=False must not be cached."""
    provider = MagicMock()
    result = MagicMock()
    result.subject_id = "user-x"
    result.zone_id = "zone-a"
    result.is_admin = False
    result.authenticated = False
    provider.authenticate = MagicMock(return_value=result)

    auth_bridge.authenticate_api_key(provider, "sk-z_u_id_xyz")
    auth_bridge.authenticate_api_key(provider, "sk-z_u_id_xyz")

    assert provider.authenticate.call_count == 2


def test_zone_set_cached_with_identity():
    """Cached identity preserves zone_set tuple (#3785)."""
    result = _mk_auth_result(subject_id="alice", zone_id="eng")
    result.zone_set = ("eng", "ops")  # provider returns full set

    provider = MagicMock()
    provider.authenticate = MagicMock(return_value=result)

    cached_1 = auth_bridge.authenticate_api_key(provider, "sk-test_alice_x_abcdefghij")
    cached_2 = auth_bridge.authenticate_api_key(provider, "sk-test_alice_x_abcdefghij")

    assert cached_1.zone_set == ("eng", "ops")
    assert cached_2.zone_set == ("eng", "ops")
    assert provider.authenticate.call_count == 1
