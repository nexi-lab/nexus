"""Parametrized unit tests for OAuthClient + AsyncOAuthClient.

Issue #1603: Domain client tests.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock

import pytest

from nexus.remote.domain.oauth import AsyncOAuthClient, OAuthClient

OAUTH_TEST_CASES = [
    ("list_providers", {}, "oauth_list_providers"),
    ("get_auth_url", {"provider": "github"}, "oauth_get_auth_url"),
    ("exchange_code", {"provider": "github", "code": "abc123"}, "oauth_exchange_code"),
    ("list_credentials", {}, "oauth_list_credentials"),
    (
        "revoke_credential",
        {"provider": "github", "user_email": "u@e.com"},
        "oauth_revoke_credential",
    ),
    (
        "test_credential",
        {"provider": "github", "user_email": "u@e.com"},
        "oauth_test_credential",
    ),
]


@pytest.mark.parametrize("method,kwargs,expected_rpc", OAUTH_TEST_CASES)
def test_sync_oauth_dispatch(method, kwargs, expected_rpc):
    mock_rpc = Mock(return_value={})
    client = OAuthClient(mock_rpc)
    getattr(client, method)(**kwargs)
    mock_rpc.assert_called_once()
    assert mock_rpc.call_args[0][0] == expected_rpc


@pytest.mark.asyncio
@pytest.mark.parametrize("method,kwargs,expected_rpc", OAUTH_TEST_CASES)
async def test_async_oauth_dispatch(method, kwargs, expected_rpc):
    mock_rpc = AsyncMock(return_value={})
    client = AsyncOAuthClient(mock_rpc)
    await getattr(client, method)(**kwargs)
    mock_rpc.assert_called_once()
    assert mock_rpc.call_args[0][0] == expected_rpc


def test_get_auth_url_includes_redirect_uri():
    """get_auth_url() should include the default redirect_uri."""
    mock_rpc = Mock(return_value={})
    client = OAuthClient(mock_rpc)
    client.get_auth_url("github")
    params = mock_rpc.call_args[0][1]
    assert "redirect_uri" in params
    assert params["provider"] == "github"
