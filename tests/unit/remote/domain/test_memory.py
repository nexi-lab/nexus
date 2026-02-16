"""Parametrized unit tests for MemoryClient + AsyncMemoryClient.

Issue #1603: Domain client tests.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock

import pytest

from nexus.remote.domain.memory import AsyncMemoryClient, MemoryClient

MEMORY_TEST_CASES = [
    ("store", {"content": "test"}, "store_memory"),
    ("list", {}, "list_memories"),
    ("retrieve", {"namespace": "ns1"}, "retrieve_memory"),
    ("query", {}, "query_memories"),
    ("search", {"query": "test"}, "query_memories"),
    ("delete", {"memory_id": "m1"}, "delete_memory"),
    ("approve", {"memory_id": "m1"}, "approve_memory"),
    ("deactivate", {"memory_id": "m1"}, "deactivate_memory"),
    ("start_trajectory", {"task_description": "test"}, "start_trajectory"),
    (
        "log_step",
        {"trajectory_id": "t1", "step_type": "action", "description": "did x"},
        "log_trajectory_step",
    ),
    ("complete_trajectory", {"trajectory_id": "t1", "status": "completed"}, "complete_trajectory"),
    ("query_trajectories", {}, "query_trajectories"),
    ("get_playbook", {}, "get_playbook"),
    ("query_playbooks", {}, "query_playbooks"),
]


@pytest.mark.parametrize("method,kwargs,expected_rpc", MEMORY_TEST_CASES)
def test_sync_memory_dispatch(method, kwargs, expected_rpc):
    mock_rpc = Mock(
        return_value={
            "memory_id": "m1",
            "memories": [],
            "deleted": True,
            "approved": True,
            "deactivated": True,
            "trajectory_id": "t1",
            "memory": {},
        }
    )
    client = MemoryClient(mock_rpc)
    getattr(client, method)(**kwargs)
    mock_rpc.assert_called_once()
    assert mock_rpc.call_args[0][0] == expected_rpc


@pytest.mark.asyncio
@pytest.mark.parametrize("method,kwargs,expected_rpc", MEMORY_TEST_CASES)
async def test_async_memory_dispatch(method, kwargs, expected_rpc):
    mock_rpc = AsyncMock(
        return_value={
            "memory_id": "m1",
            "memories": [],
            "deleted": True,
            "approved": True,
            "deactivated": True,
            "trajectory_id": "t1",
            "memory": {},
        }
    )
    client = AsyncMemoryClient(mock_rpc)
    await getattr(client, method)(**kwargs)
    mock_rpc.assert_called_once()
    assert mock_rpc.call_args[0][0] == expected_rpc


def test_store_extracts_memory_id():
    """store() should return the memory_id from the RPC result."""
    mock_rpc = Mock(return_value={"memory_id": "m-abc"})
    client = MemoryClient(mock_rpc)
    result = client.store("test content")
    assert result == "m-abc"


def test_delete_extracts_deleted():
    """delete() should return the boolean deleted flag."""
    mock_rpc = Mock(return_value={"deleted": True})
    client = MemoryClient(mock_rpc)
    result = client.delete("m1")
    assert result is True
