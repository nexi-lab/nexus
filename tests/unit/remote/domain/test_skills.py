"""Parametrized unit tests for SkillsClient + AsyncSkillsClient.

Issue #1603: Domain client tests.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock

import pytest

from nexus.remote.domain.skills import AsyncSkillsClient, SkillsClient

SKILL_TEST_CASES = [
    ("create", {"name": "s1", "description": "d1"}, "skills_create"),
    ("list", {}, "skills_list"),
    ("info", {"skill_name": "s1"}, "skills_info"),
    ("fork", {"source_name": "s1", "target_name": "s2"}, "skills_fork"),
    ("publish", {"skill_name": "s1"}, "skills_publish"),
    ("search", {"query": "test"}, "skills_search"),
    ("export", {"skill_name": "s1"}, "skills_export"),
    ("share", {"skill_path": "s1", "share_with": "z1"}, "skills_share"),
    ("unshare", {"skill_path": "s1", "unshare_from": "z1"}, "skills_unshare"),
    ("discover", {}, "skills_discover"),
    ("subscribe", {"skill_path": "s1"}, "skills_subscribe"),
    ("unsubscribe", {"skill_path": "s1"}, "skills_unsubscribe"),
    ("get_prompt_context", {}, "skills_get_prompt_context"),
    ("load", {"skill_path": "s1"}, "skills_load"),
]


@pytest.mark.parametrize("method,kwargs,expected_rpc", SKILL_TEST_CASES)
def test_sync_skills_dispatch(method, kwargs, expected_rpc):
    mock_rpc = Mock(return_value={})
    client = SkillsClient(mock_rpc)
    getattr(client, method)(**kwargs)
    mock_rpc.assert_called_once()
    assert mock_rpc.call_args[0][0] == expected_rpc


@pytest.mark.asyncio
@pytest.mark.parametrize("method,kwargs,expected_rpc", SKILL_TEST_CASES)
async def test_async_skills_dispatch(method, kwargs, expected_rpc):
    mock_rpc = AsyncMock(return_value={})
    client = AsyncSkillsClient(mock_rpc)
    await getattr(client, method)(**kwargs)
    mock_rpc.assert_called_once()
    assert mock_rpc.call_args[0][0] == expected_rpc


def test_create_includes_defaults():
    """create() should include template and tier defaults."""
    mock_rpc = Mock(return_value={})
    client = SkillsClient(mock_rpc)
    client.create("s1", "d1")
    params = mock_rpc.call_args[0][1]
    assert params["name"] == "s1"
    assert params["description"] == "d1"
    assert params["template"] == "basic"
    assert params["tier"] == "agent"


def test_create_with_author():
    """create() should include author only when provided."""
    mock_rpc = Mock(return_value={})
    client = SkillsClient(mock_rpc)
    client.create("s1", "d1", author="me")
    params = mock_rpc.call_args[0][1]
    assert params["author"] == "me"
