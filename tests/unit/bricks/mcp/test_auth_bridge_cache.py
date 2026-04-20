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
