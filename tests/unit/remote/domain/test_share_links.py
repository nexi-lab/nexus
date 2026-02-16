"""Parametrized unit tests for ShareLinksClient + AsyncShareLinksClient.

Issue #1603: Domain client tests.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock

import pytest

from nexus.remote.domain.share_links import AsyncShareLinksClient, ShareLinksClient

SHARE_LINKS_TEST_CASES = [
    ("create", {"path": "/file.txt"}, "create_share_link"),
    ("get", {"link_id": "l1"}, "get_share_link"),
    ("list", {}, "list_share_links"),
    ("revoke", {"link_id": "l1"}, "revoke_share_link"),
    ("access", {"link_id": "l1"}, "access_share_link"),
    ("get_access_logs", {"link_id": "l1"}, "get_share_link_access_logs"),
]


@pytest.mark.parametrize("method,kwargs,expected_rpc", SHARE_LINKS_TEST_CASES)
def test_sync_share_links_dispatch(method, kwargs, expected_rpc):
    mock_rpc = Mock(return_value={})
    client = ShareLinksClient(mock_rpc)
    getattr(client, method)(**kwargs)
    mock_rpc.assert_called_once()
    assert mock_rpc.call_args[0][0] == expected_rpc


@pytest.mark.asyncio
@pytest.mark.parametrize("method,kwargs,expected_rpc", SHARE_LINKS_TEST_CASES)
async def test_async_share_links_dispatch(method, kwargs, expected_rpc):
    mock_rpc = AsyncMock(return_value={})
    client = AsyncShareLinksClient(mock_rpc)
    await getattr(client, method)(**kwargs)
    mock_rpc.assert_called_once()
    assert mock_rpc.call_args[0][0] == expected_rpc
