"""Tests for WorkQueryBuilder."""

import json
import uuid
from unittest.mock import Mock

import pytest

from nexus.core.exceptions import MetadataError
from nexus.storage.query_builder import WorkQueryBuilder


class TestGetReadyWork:
    """Test get_ready_work method."""

    def test_get_ready_work_no_limit(self):
        """Test getting ready work without limit."""
        # Mock session
        session = Mock()
        mock_result = Mock()
        mock_rows = [
            (
                uuid.uuid4(),  # path_id
                uuid.uuid4(),  # tenant_id
                "/test/path",  # virtual_path
                "backend1",  # backend_id
                "/physical/path",  # physical_path
                "file",  # file_type
                1024,  # size_bytes
                "hash123",  # content_hash
                "2024-01-01",  # created_at
                "2024-01-02",  # updated_at
                json.dumps({"state": "ready"}),  # status
                json.dumps({"value": 1}),  # priority
            )
        ]
        mock_result.fetchall.return_value = mock_rows
        session.execute.return_value = mock_result

        # Execute
        result = WorkQueryBuilder.get_ready_work(session)

        # Verify
        session.execute.assert_called_once()
        call_args = session.execute.call_args[0][0]
        assert "SELECT * FROM ready_work_items" in str(call_args)
        assert "LIMIT" not in str(call_args)
        assert len(result) == 1
        assert result[0]["virtual_path"] == "/test/path"
        assert result[0]["status"] == {"state": "ready"}
        assert result[0]["priority"] == {"value": 1}

    def test_get_ready_work_with_limit(self):
        """Test getting ready work with limit."""
        # Mock session
        session = Mock()
        mock_result = Mock()
        mock_result.fetchall.return_value = []
        session.execute.return_value = mock_result

        # Execute
        WorkQueryBuilder.get_ready_work(session, limit=10)

        # Verify
        session.execute.assert_called_once()
        call_args = session.execute.call_args[0][0]
        assert "SELECT * FROM ready_work_items" in str(call_args)
        assert "LIMIT 10" in str(call_args)

    def test_get_ready_work_null_status(self):
        """Test getting ready work with null status and priority."""
        # Mock session
        session = Mock()
        mock_result = Mock()
        mock_rows = [
            (
                uuid.uuid4(),  # path_id
                uuid.uuid4(),  # tenant_id
                "/test/path",  # virtual_path
                "backend1",  # backend_id
                "/physical/path",  # physical_path
                "file",  # file_type
                1024,  # size_bytes
                "hash123",  # content_hash
                "2024-01-01",  # created_at
                "2024-01-02",  # updated_at
                None,  # status
                None,  # priority
            )
        ]
        mock_result.fetchall.return_value = mock_rows
        session.execute.return_value = mock_result

        # Execute
        result = WorkQueryBuilder.get_ready_work(session)

        # Verify
        assert len(result) == 1
        assert result[0]["status"] is None
        assert result[0]["priority"] is None

    def test_get_ready_work_error(self):
        """Test error handling in get_ready_work."""
        # Mock session that raises error
        session = Mock()
        session.execute.side_effect = Exception("Database error")

        # Execute and verify exception
        with pytest.raises(MetadataError) as exc_info:
            WorkQueryBuilder.get_ready_work(session)

        assert "Failed to get ready work" in str(exc_info.value)
        assert "Database error" in str(exc_info.value)


class TestGetPendingWork:
    """Test get_pending_work method."""

    def test_get_pending_work_no_limit(self):
        """Test getting pending work without limit."""
        # Mock session
        session = Mock()
        mock_result = Mock()
        mock_rows = [
            (
                uuid.uuid4(),  # path_id
                uuid.uuid4(),  # tenant_id
                "/pending/path",  # virtual_path
                "backend1",  # backend_id
                "/physical/path",  # physical_path
                "file",  # file_type
                2048,  # size_bytes
                "hash456",  # content_hash
                "2024-01-01",  # created_at
                "2024-01-02",  # updated_at
                json.dumps({"state": "pending"}),  # status
                json.dumps({"value": 5}),  # priority
            )
        ]
        mock_result.fetchall.return_value = mock_rows
        session.execute.return_value = mock_result

        # Execute
        result = WorkQueryBuilder.get_pending_work(session)

        # Verify
        session.execute.assert_called_once()
        call_args = session.execute.call_args[0][0]
        assert "SELECT * FROM pending_work_items" in str(call_args)
        assert len(result) == 1
        assert result[0]["virtual_path"] == "/pending/path"
        assert result[0]["size_bytes"] == 2048

    def test_get_pending_work_with_limit(self):
        """Test getting pending work with limit."""
        # Mock session
        session = Mock()
        mock_result = Mock()
        mock_result.fetchall.return_value = []
        session.execute.return_value = mock_result

        # Execute
        WorkQueryBuilder.get_pending_work(session, limit=5)

        # Verify
        call_args = session.execute.call_args[0][0]
        assert "LIMIT 5" in str(call_args)

    def test_get_pending_work_error(self):
        """Test error handling in get_pending_work."""
        # Mock session that raises error
        session = Mock()
        session.execute.side_effect = Exception("Query failed")

        # Execute and verify exception
        with pytest.raises(MetadataError) as exc_info:
            WorkQueryBuilder.get_pending_work(session)

        assert "Failed to get pending work" in str(exc_info.value)


class TestGetBlockedWork:
    """Test get_blocked_work method."""

    def test_get_blocked_work_no_limit(self):
        """Test getting blocked work without limit."""
        # Mock session
        session = Mock()
        mock_result = Mock()
        mock_rows = [
            (
                uuid.uuid4(),  # path_id
                uuid.uuid4(),  # tenant_id
                "/blocked/path",  # virtual_path
                "backend1",  # backend_id
                "/physical/path",  # physical_path
                "file",  # file_type
                4096,  # size_bytes
                "hash789",  # content_hash
                "2024-01-01",  # created_at
                "2024-01-02",  # updated_at
                json.dumps({"state": "blocked"}),  # status
                json.dumps({"value": 3}),  # priority
                2,  # blocker_count
            )
        ]
        mock_result.fetchall.return_value = mock_rows
        session.execute.return_value = mock_result

        # Execute
        result = WorkQueryBuilder.get_blocked_work(session)

        # Verify
        session.execute.assert_called_once()
        call_args = session.execute.call_args[0][0]
        assert "SELECT * FROM blocked_work_items" in str(call_args)
        assert len(result) == 1
        assert result[0]["virtual_path"] == "/blocked/path"
        assert result[0]["blocker_count"] == 2

    def test_get_blocked_work_with_limit(self):
        """Test getting blocked work with limit."""
        # Mock session
        session = Mock()
        mock_result = Mock()
        mock_result.fetchall.return_value = []
        session.execute.return_value = mock_result

        # Execute
        WorkQueryBuilder.get_blocked_work(session, limit=20)

        # Verify
        call_args = session.execute.call_args[0][0]
        assert "LIMIT 20" in str(call_args)

    def test_get_blocked_work_error(self):
        """Test error handling in get_blocked_work."""
        # Mock session that raises error
        session = Mock()
        session.execute.side_effect = RuntimeError("Connection lost")

        # Execute and verify exception
        with pytest.raises(MetadataError) as exc_info:
            WorkQueryBuilder.get_blocked_work(session)

        assert "Failed to get blocked work" in str(exc_info.value)


class TestGetInProgressWork:
    """Test get_in_progress_work method."""

    def test_get_in_progress_work_no_limit(self):
        """Test getting in-progress work without limit."""
        # Mock session
        session = Mock()
        mock_result = Mock()
        mock_rows = [
            (
                uuid.uuid4(),  # path_id
                uuid.uuid4(),  # tenant_id
                "/progress/path",  # virtual_path
                "backend1",  # backend_id
                "file",  # file_type
                8192,  # size_bytes
                "2024-01-01",  # created_at
                "2024-01-02",  # updated_at
                json.dumps({"state": "in_progress"}),  # status
                json.dumps({"worker": "worker-1"}),  # worker_id
                json.dumps({"time": "2024-01-02T10:00:00"}),  # started_at
            )
        ]
        mock_result.fetchall.return_value = mock_rows
        session.execute.return_value = mock_result

        # Execute
        result = WorkQueryBuilder.get_in_progress_work(session)

        # Verify
        session.execute.assert_called_once()
        call_args = session.execute.call_args[0][0]
        assert "SELECT * FROM in_progress_work" in str(call_args)
        assert len(result) == 1
        assert result[0]["virtual_path"] == "/progress/path"
        assert result[0]["worker_id"] == {"worker": "worker-1"}
        assert result[0]["started_at"] == {"time": "2024-01-02T10:00:00"}

    def test_get_in_progress_work_with_limit(self):
        """Test getting in-progress work with limit."""
        # Mock session
        session = Mock()
        mock_result = Mock()
        mock_result.fetchall.return_value = []
        session.execute.return_value = mock_result

        # Execute
        WorkQueryBuilder.get_in_progress_work(session, limit=15)

        # Verify
        call_args = session.execute.call_args[0][0]
        assert "LIMIT 15" in str(call_args)

    def test_get_in_progress_work_null_fields(self):
        """Test getting in-progress work with null fields."""
        # Mock session
        session = Mock()
        mock_result = Mock()
        mock_rows = [
            (
                uuid.uuid4(),  # path_id
                uuid.uuid4(),  # tenant_id
                "/progress/path",  # virtual_path
                "backend1",  # backend_id
                "file",  # file_type
                8192,  # size_bytes
                "2024-01-01",  # created_at
                "2024-01-02",  # updated_at
                None,  # status
                None,  # worker_id
                None,  # started_at
            )
        ]
        mock_result.fetchall.return_value = mock_rows
        session.execute.return_value = mock_result

        # Execute
        result = WorkQueryBuilder.get_in_progress_work(session)

        # Verify
        assert len(result) == 1
        assert result[0]["status"] is None
        assert result[0]["worker_id"] is None
        assert result[0]["started_at"] is None

    def test_get_in_progress_work_error(self):
        """Test error handling in get_in_progress_work."""
        # Mock session that raises error
        session = Mock()
        session.execute.side_effect = Exception("Timeout")

        # Execute and verify exception
        with pytest.raises(MetadataError) as exc_info:
            WorkQueryBuilder.get_in_progress_work(session)

        assert "Failed to get in-progress work" in str(exc_info.value)


class TestGetWorkByPriority:
    """Test get_work_by_priority method."""

    def test_get_work_by_priority_no_limit(self):
        """Test getting work by priority without limit."""
        # Mock session
        session = Mock()
        mock_result = Mock()
        mock_rows = [
            (
                uuid.uuid4(),  # path_id
                uuid.uuid4(),  # tenant_id
                "/priority/path",  # virtual_path
                "backend1",  # backend_id
                "file",  # file_type
                16384,  # size_bytes
                "2024-01-01",  # created_at
                "2024-01-02",  # updated_at
                json.dumps({"state": "ready"}),  # status
                json.dumps({"value": 10}),  # priority
                json.dumps(["important", "urgent"]),  # tags
            )
        ]
        mock_result.fetchall.return_value = mock_rows
        session.execute.return_value = mock_result

        # Execute
        result = WorkQueryBuilder.get_work_by_priority(session)

        # Verify
        session.execute.assert_called_once()
        call_args = session.execute.call_args[0][0]
        assert "SELECT * FROM work_by_priority" in str(call_args)
        assert len(result) == 1
        assert result[0]["virtual_path"] == "/priority/path"
        assert result[0]["priority"] == {"value": 10}
        assert result[0]["tags"] == ["important", "urgent"]

    def test_get_work_by_priority_with_limit(self):
        """Test getting work by priority with limit."""
        # Mock session
        session = Mock()
        mock_result = Mock()
        mock_result.fetchall.return_value = []
        session.execute.return_value = mock_result

        # Execute
        WorkQueryBuilder.get_work_by_priority(session, limit=100)

        # Verify
        call_args = session.execute.call_args[0][0]
        assert "LIMIT 100" in str(call_args)

    def test_get_work_by_priority_multiple_results(self):
        """Test getting multiple work items by priority."""
        # Mock session
        session = Mock()
        mock_result = Mock()
        mock_rows = [
            (
                uuid.uuid4(),
                uuid.uuid4(),
                "/high/priority",
                "backend1",
                "file",
                100,
                "2024-01-01",
                "2024-01-02",
                json.dumps({"state": "ready"}),
                json.dumps({"value": 100}),
                json.dumps(["high"]),
            ),
            (
                uuid.uuid4(),
                uuid.uuid4(),
                "/low/priority",
                "backend1",
                "file",
                100,
                "2024-01-01",
                "2024-01-02",
                json.dumps({"state": "ready"}),
                json.dumps({"value": 1}),
                json.dumps(["low"]),
            ),
        ]
        mock_result.fetchall.return_value = mock_rows
        session.execute.return_value = mock_result

        # Execute
        result = WorkQueryBuilder.get_work_by_priority(session)

        # Verify
        assert len(result) == 2
        assert result[0]["virtual_path"] == "/high/priority"
        assert result[1]["virtual_path"] == "/low/priority"

    def test_get_work_by_priority_error(self):
        """Test error handling in get_work_by_priority."""
        # Mock session that raises error
        session = Mock()
        session.execute.side_effect = Exception("Invalid query")

        # Execute and verify exception
        with pytest.raises(MetadataError) as exc_info:
            WorkQueryBuilder.get_work_by_priority(session)

        assert "Failed to get work by priority" in str(exc_info.value)
