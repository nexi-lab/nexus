"""Tests for OperationLogProtocol conformance (Issue #552).

Verifies that OperationLogger structurally conforms to OperationLogProtocol.
"""

from unittest.mock import MagicMock

from nexus.contracts.protocols.operation_log import OperationLogProtocol
from nexus.storage.operation_logger import OperationLogger


class TestOperationLogProtocol:
    """Test OperationLogProtocol conformance."""

    def test_logger_conforms_to_protocol(self) -> None:
        """Test that OperationLogger satisfies OperationLogProtocol."""
        logger = OperationLogger(session=MagicMock())
        assert isinstance(logger, OperationLogProtocol)

    def test_protocol_methods_exist(self) -> None:
        """Test that all protocol methods exist on logger."""
        logger = OperationLogger(session=MagicMock())
        assert hasattr(logger, "log_operation")
        assert hasattr(logger, "get_operation")
        assert hasattr(logger, "list_operations")
        assert hasattr(logger, "list_operations_cursor")
        assert hasattr(logger, "count_operations")
        assert hasattr(logger, "get_last_operation")
        assert hasattr(logger, "get_path_history")
        assert hasattr(logger, "agent_activity_summary")
        assert hasattr(logger, "get_metadata_snapshot")

    def test_protocol_methods_are_callable(self) -> None:
        """Test that all protocol methods are callable."""
        logger = OperationLogger(session=MagicMock())
        assert callable(logger.log_operation)
        assert callable(logger.get_operation)
        assert callable(logger.list_operations)
        assert callable(logger.list_operations_cursor)
        assert callable(logger.count_operations)
        assert callable(logger.get_last_operation)
        assert callable(logger.get_path_history)
        assert callable(logger.agent_activity_summary)
        assert callable(logger.get_metadata_snapshot)
