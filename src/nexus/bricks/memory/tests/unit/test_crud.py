"""Unit tests for Memory brick CRUD operations.

Tests the core CRUD functionality (store, get, retrieve, delete, list)
using centralized fixtures from conftest.py.

Related: Issue #2128 (Memory brick extraction)
"""

from unittest.mock import Mock, patch

import pytest


class TestMemoryCRUD:
    """Test suite for MemoryCRUD operations."""

    def test_store_basic(self, memory_router_mock, permission_enforcer_allow_all, backend_mock):
        """Test basic memory storage."""
        from nexus.bricks.memory.crud import MemoryCRUD
        from nexus.core.permissions import OperationContext

        context = OperationContext(user_id="test", groups=[], is_admin=False)
        crud = MemoryCRUD(
            memory_router=memory_router_mock,
            permission_enforcer=permission_enforcer_allow_all,
            backend=backend_mock,
            context=context,
        )

        # Mock enrichment pipeline
        with patch("nexus.bricks.memory.crud.EnrichmentPipeline") as mock_pipeline_class:
            mock_pipeline = Mock()
            mock_pipeline.enrich.return_value = Mock(
                embedding_json=None,
                entities_json=None,
                temporal_refs_json=None,
            )
            mock_pipeline_class.return_value = mock_pipeline

            memory_id = crud.store(content="Test memory", scope="user")

            assert memory_id == "mem_test_123"
            backend_mock.write_content.assert_called_once()

    def test_get_with_permissions(self, memory_router_mock, permission_enforcer_allow_all, backend_mock):
        """Test memory retrieval with permission checks."""
        from nexus.bricks.memory.crud import MemoryCRUD
        from nexus.core.permissions import OperationContext

        context = OperationContext(user_id="test", groups=[], is_admin=False)
        memory_router_mock.get_memory_by_id.return_value = Mock(
            memory_id="mem_test",
            content_hash="hash_123",
            scope="user",
        )
        backend_mock.read_content.return_value = Mock(unwrap=lambda: b"Test content")

        crud = MemoryCRUD(
            memory_router=memory_router_mock,
            permission_enforcer=permission_enforcer_allow_all,
            backend=backend_mock,
            context=context,
        )

        result = crud.get(memory_id="mem_test")

        assert result is not None
        permission_enforcer_allow_all.check_memory.assert_called_once()

    def test_get_denied(self, memory_router_mock, permission_enforcer_deny_all, backend_mock):
        """Test memory retrieval fails when permissions denied."""
        from nexus.bricks.memory.crud import MemoryCRUD
        from nexus.core.permissions import OperationContext

        context = OperationContext(user_id="test", groups=[], is_admin=False)
        memory_router_mock.get_memory_by_id.return_value = Mock(
            memory_id="mem_test",
            content_hash="hash_123",
        )

        crud = MemoryCRUD(
            memory_router=memory_router_mock,
            permission_enforcer=permission_enforcer_deny_all,
            backend=backend_mock,
            context=context,
        )

        result = crud.get(memory_id="mem_test")

        assert result is None

    def test_delete_success(self, memory_router_mock, permission_enforcer_allow_all, backend_mock):
        """Test successful memory deletion."""
        from nexus.bricks.memory.crud import MemoryCRUD
        from nexus.core.permissions import OperationContext

        context = OperationContext(user_id="test", groups=[], is_admin=False)
        memory_router_mock.get_memory_by_id.return_value = Mock(memory_id="mem_test")
        memory_router_mock.delete_memory.return_value = True

        crud = MemoryCRUD(
            memory_router=memory_router_mock,
            permission_enforcer=permission_enforcer_allow_all,
            backend=backend_mock,
            context=context,
        )

        result = crud.delete(memory_id="mem_test")

        assert result is True
        memory_router_mock.delete_memory.assert_called_once_with("mem_test")
