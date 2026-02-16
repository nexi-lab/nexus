"""Parametrized unit tests for SandboxClient + AsyncSandboxClient.

Issue #1603: Domain client tests.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock

import pytest

from nexus.remote.domain.sandbox import AsyncSandboxClient, SandboxClient


def _make_sandbox_client(mock_rpc):
    return SandboxClient(mock_rpc, lambda: "http://localhost:2026", lambda: "sk-test")


def _make_async_sandbox_client(mock_rpc):
    return AsyncSandboxClient(mock_rpc, lambda: "http://localhost:2026", lambda: "sk-test")


SANDBOX_TEST_CASES = [
    (
        "connect",
        {"sandbox_id": "sb1"},
        "sandbox_connect",
        {
            "sandbox_id": "sb1",
            "provider": "e2b",
            "mount_path": "/mnt/nexus",
            "nexus_url": "http://localhost:2026",
            "nexus_api_key": "sk-test",
        },
    ),
    ("pause", {"sandbox_id": "sb1"}, "sandbox_pause", {"sandbox_id": "sb1"}),
    ("resume", {"sandbox_id": "sb1"}, "sandbox_resume", {"sandbox_id": "sb1"}),
    ("stop", {"sandbox_id": "sb1"}, "sandbox_stop", {"sandbox_id": "sb1"}),
    ("list", {}, "sandbox_list", {}),
    ("status", {"sandbox_id": "sb1"}, "sandbox_status", {"sandbox_id": "sb1"}),
    ("disconnect", {"sandbox_id": "sb1"}, "sandbox_disconnect", {"sandbox_id": "sb1"}),
]


@pytest.mark.parametrize("method,kwargs,expected_rpc,expected_params", SANDBOX_TEST_CASES)
def test_sync_sandbox_dispatch(method, kwargs, expected_rpc, expected_params):
    mock_rpc = Mock(return_value={})
    client = _make_sandbox_client(mock_rpc)
    getattr(client, method)(**kwargs)
    mock_rpc.assert_called_once()
    call_args = mock_rpc.call_args
    assert call_args[0][0] == expected_rpc
    for key, value in expected_params.items():
        assert call_args[0][1][key] == value


@pytest.mark.asyncio
@pytest.mark.parametrize("method,kwargs,expected_rpc,expected_params", SANDBOX_TEST_CASES)
async def test_async_sandbox_dispatch(method, kwargs, expected_rpc, expected_params):
    mock_rpc = AsyncMock(return_value={})
    client = _make_async_sandbox_client(mock_rpc)
    await getattr(client, method)(**kwargs)
    mock_rpc.assert_called_once()
    call_args = mock_rpc.call_args
    assert call_args[0][0] == expected_rpc
    for key, value in expected_params.items():
        assert call_args[0][1][key] == value


def test_connect_auto_fills_nexus_url():
    """connect() should auto-fill nexus_url and nexus_api_key from get_server_url/get_api_key."""
    mock_rpc = Mock(return_value={})
    client = SandboxClient(mock_rpc, lambda: "http://my-server:8000", lambda: "my-key")
    client.connect("sb1")
    params = mock_rpc.call_args[0][1]
    assert params["nexus_url"] == "http://my-server:8000"
    assert params["nexus_api_key"] == "my-key"


def test_connect_explicit_url_overrides():
    """Explicit nexus_url should override the auto-filled value."""
    mock_rpc = Mock(return_value={})
    client = _make_sandbox_client(mock_rpc)
    client.connect("sb1", nexus_url="http://custom:9000", nexus_api_key="custom-key")
    params = mock_rpc.call_args[0][1]
    assert params["nexus_url"] == "http://custom:9000"
    assert params["nexus_api_key"] == "custom-key"
