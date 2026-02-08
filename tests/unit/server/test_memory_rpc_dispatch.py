"""Unit tests for memory RPC method dispatch in FastAPI server (#1203).

Tests that each memory method correctly dispatches through
_dispatch_method() and calls the appropriate memory_api method.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from nexus.server.protocol import (
    ApproveMemoryBatchParams,
    ApproveMemoryParams,
    DeactivateMemoryBatchParams,
    DeactivateMemoryParams,
    DeleteMemoryBatchParams,
    DeleteMemoryParams,
    ListMemoriesParams,
    QueryMemoriesParams,
    RetrieveMemoryParams,
    StoreMemoryParams,
)


@pytest.fixture
def mock_memory_api():
    """Create a mock Memory API with all expected methods."""
    api = MagicMock()
    api.store.return_value = "mem_123"
    api.list.return_value = [{"memory_id": "mem_1", "content": "test"}]
    api.query.return_value = [{"memory_id": "mem_2", "content": "queried"}]
    api.search.return_value = [{"memory_id": "mem_3", "content": "searched"}]
    api.retrieve.return_value = {"memory_id": "mem_4", "content": "retrieved"}
    api.delete.return_value = True
    api.approve.return_value = True
    api.deactivate.return_value = True
    api.approve_batch.return_value = {"approved": 2, "failed": 0}
    api.deactivate_batch.return_value = {"deactivated": 2, "failed": 0}
    api.delete_batch.return_value = {"deleted": 2, "failed": 0}
    return api


@pytest.fixture
def mock_app_state(mock_memory_api):
    """Mock _app_state with NexusFS and memory API."""
    mock_nexus_fs = MagicMock()
    mock_nexus_fs._get_memory_api.return_value = mock_memory_api

    with (
        patch("nexus.server.fastapi_server._app_state") as mock_state,
        patch(
            "nexus.server.fastapi_server._get_memory_api_with_context",
            return_value=mock_memory_api,
        ),
    ):
        mock_state.nexus_fs = mock_nexus_fs
        mock_state.exposed_methods = {}
        yield mock_state


class TestMemoryRPCDispatch:
    """Test that memory methods dispatch correctly through FastAPI server."""

    def test_store_memory(self, mock_app_state, mock_memory_api):
        """store_memory dispatches to memory_api.store()."""
        from nexus.server.fastapi_server import _handle_store_memory

        params = StoreMemoryParams(content="Test memory", scope="user")
        context = MagicMock()
        result = _handle_store_memory(params, context)

        assert result == {"memory_id": "mem_123"}
        mock_memory_api.store.assert_called_once_with(
            content="Test memory",
            memory_type="fact",
            scope="user",
            importance=0.5,
            namespace=None,
            path_key=None,
            state="active",
        )

    def test_list_memories(self, mock_app_state, mock_memory_api):
        """list_memories dispatches to memory_api.list()."""
        from nexus.server.fastapi_server import _handle_list_memories

        params = ListMemoriesParams(scope="user", limit=10)
        context = MagicMock()
        result = _handle_list_memories(params, context)

        assert result == {"memories": [{"memory_id": "mem_1", "content": "test"}]}
        mock_memory_api.list.assert_called_once_with(
            scope="user",
            memory_type=None,
            namespace=None,
            namespace_prefix=None,
            state="active",
            limit=10,
        )

    def test_list_memories_with_filters(self, mock_app_state, mock_memory_api):
        """list_memories passes all filter parameters correctly."""
        from nexus.server.fastapi_server import _handle_list_memories

        params = ListMemoriesParams(
            scope="agent",
            memory_type="preference",
            namespace="project/alpha",
            namespace_prefix="project/",
            state="inactive",
            limit=25,
        )
        context = MagicMock()
        _handle_list_memories(params, context)

        mock_memory_api.list.assert_called_once_with(
            scope="agent",
            memory_type="preference",
            namespace="project/alpha",
            namespace_prefix="project/",
            state="inactive",
            limit=25,
        )

    def test_query_memories_without_query(self, mock_app_state, mock_memory_api):
        """query_memories without query param dispatches to memory_api.query()."""
        from nexus.server.fastapi_server import _handle_query_memories

        params = QueryMemoriesParams(scope="user", limit=10)
        context = MagicMock()
        result = _handle_query_memories(params, context)

        assert result == {"memories": [{"memory_id": "mem_2", "content": "queried"}]}
        mock_memory_api.query.assert_called_once()
        mock_memory_api.search.assert_not_called()

    def test_query_memories_with_query(self, mock_app_state, mock_memory_api):
        """query_memories with query param dispatches to memory_api.search()."""
        from nexus.server.fastapi_server import _handle_query_memories

        params = QueryMemoriesParams(query="dark mode", scope="user", limit=10)
        context = MagicMock()
        result = _handle_query_memories(params, context)

        assert result == {"memories": [{"memory_id": "mem_3", "content": "searched"}]}
        mock_memory_api.search.assert_called_once()
        mock_memory_api.query.assert_not_called()

    def test_retrieve_memory(self, mock_app_state, mock_memory_api):
        """retrieve_memory dispatches to memory_api.retrieve()."""
        from nexus.server.fastapi_server import _handle_retrieve_memory

        params = RetrieveMemoryParams(namespace="config", path_key="theme")
        context = MagicMock()
        result = _handle_retrieve_memory(params, context)

        assert result == {"memory": {"memory_id": "mem_4", "content": "retrieved"}}
        mock_memory_api.retrieve.assert_called_once_with(
            namespace="config",
            path_key="theme",
            path=None,
        )

    def test_delete_memory(self, mock_app_state, mock_memory_api):
        """delete_memory dispatches to memory_api.delete()."""
        from nexus.server.fastapi_server import _handle_delete_memory

        params = DeleteMemoryParams(memory_id="mem_123")
        context = MagicMock()
        result = _handle_delete_memory(params, context)

        assert result == {"deleted": True}
        mock_memory_api.delete.assert_called_once_with("mem_123")

    def test_approve_memory(self, mock_app_state, mock_memory_api):
        """approve_memory dispatches to memory_api.approve()."""
        from nexus.server.fastapi_server import _handle_approve_memory

        params = ApproveMemoryParams(memory_id="mem_123")
        context = MagicMock()
        result = _handle_approve_memory(params, context)

        assert result == {"approved": True}
        mock_memory_api.approve.assert_called_once_with("mem_123")

    def test_deactivate_memory(self, mock_app_state, mock_memory_api):
        """deactivate_memory dispatches to memory_api.deactivate()."""
        from nexus.server.fastapi_server import _handle_deactivate_memory

        params = DeactivateMemoryParams(memory_id="mem_123")
        context = MagicMock()
        result = _handle_deactivate_memory(params, context)

        assert result == {"deactivated": True}
        mock_memory_api.deactivate.assert_called_once_with("mem_123")

    def test_approve_memory_batch(self, mock_app_state, mock_memory_api):
        """approve_memory_batch dispatches to memory_api.approve_batch()."""
        from nexus.server.fastapi_server import _handle_approve_memory_batch

        params = ApproveMemoryBatchParams(memory_ids=["mem_1", "mem_2"])
        context = MagicMock()
        result = _handle_approve_memory_batch(params, context)

        assert result == {"approved": 2, "failed": 0}
        mock_memory_api.approve_batch.assert_called_once_with(["mem_1", "mem_2"])

    def test_deactivate_memory_batch(self, mock_app_state, mock_memory_api):
        """deactivate_memory_batch dispatches to memory_api.deactivate_batch()."""
        from nexus.server.fastapi_server import _handle_deactivate_memory_batch

        params = DeactivateMemoryBatchParams(memory_ids=["mem_1", "mem_2"])
        context = MagicMock()
        result = _handle_deactivate_memory_batch(params, context)

        assert result == {"deactivated": 2, "failed": 0}
        mock_memory_api.deactivate_batch.assert_called_once_with(["mem_1", "mem_2"])

    def test_delete_memory_batch(self, mock_app_state, mock_memory_api):
        """delete_memory_batch dispatches to memory_api.delete_batch()."""
        from nexus.server.fastapi_server import _handle_delete_memory_batch

        params = DeleteMemoryBatchParams(memory_ids=["mem_1", "mem_2"])
        context = MagicMock()
        result = _handle_delete_memory_batch(params, context)

        assert result == {"deleted": 2, "failed": 0}
        mock_memory_api.delete_batch.assert_called_once_with(["mem_1", "mem_2"])


class TestStoreMemoryParamDefaults:
    """Test that StoreMemoryParams default values are correctly applied."""

    def test_defaults(self):
        """StoreMemoryParams has correct defaults for optional fields."""
        params = StoreMemoryParams(content="test")
        assert params.memory_type == "fact"
        assert params.scope == "agent"
        assert params.importance == 0.5
        assert params.namespace is None
        assert params.path_key is None
        assert params.state == "active"

    def test_list_memories_defaults(self):
        """ListMemoriesParams has correct defaults."""
        params = ListMemoriesParams()
        assert params.limit == 50
        assert params.scope is None
        assert params.memory_type is None
        assert params.namespace is None
        assert params.namespace_prefix is None
        assert params.state == "active"
