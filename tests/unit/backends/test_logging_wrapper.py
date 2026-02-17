"""Unit tests for LoggingBackendWrapper (#1449).

Tests verify:
1. All operations produce structured debug log output
2. Log messages contain operation name, latency, and success/failure
3. Lifecycle events (connect/disconnect) log at INFO level
4. Delegation passes through correctly to inner backend

Uses pytest's caplog fixture for log capture.

Design reference:
    - NEXUS-LEGO-ARCHITECTURE.md PART 16, Recursive Wrapping (Mechanism 2)
"""

import logging
from unittest.mock import MagicMock, PropertyMock

import pytest

from nexus.backends.backend import Backend, HandlerStatusResponse
from nexus.backends.logging_wrapper import LoggingBackendWrapper
from nexus.core.response import HandlerResponse

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_inner() -> MagicMock:
    """Create a mock Backend for testing."""
    mock = MagicMock(spec=Backend)
    mock.name = "test-backend"
    mock.describe.return_value = "test-backend"
    type(mock).user_scoped = PropertyMock(return_value=False)
    type(mock).is_connected = PropertyMock(return_value=True)
    type(mock).thread_safe = PropertyMock(return_value=True)
    type(mock).supports_rename = PropertyMock(return_value=False)
    type(mock).has_virtual_filesystem = PropertyMock(return_value=False)
    type(mock).has_root_path = PropertyMock(return_value=True)
    type(mock).has_token_manager = PropertyMock(return_value=False)
    type(mock).has_data_dir = PropertyMock(return_value=False)
    type(mock).is_passthrough = PropertyMock(return_value=False)
    type(mock).supports_parallel_mmap_read = PropertyMock(return_value=False)
    return mock

@pytest.fixture
def logged(mock_inner: MagicMock) -> LoggingBackendWrapper:
    """Create a LoggingBackendWrapper wrapping the mock inner."""
    return LoggingBackendWrapper(inner=mock_inner)

# ---------------------------------------------------------------------------
# describe() Tests
# ---------------------------------------------------------------------------

class TestDescribe:
    def test_describe_single(self, logged: LoggingBackendWrapper) -> None:
        assert logged.describe() == "logging → test-backend"

    def test_describe_chain(self, mock_inner: MagicMock) -> None:
        mock_inner.describe.return_value = "cache → s3"
        wrapper = LoggingBackendWrapper(inner=mock_inner)
        assert wrapper.describe() == "logging → cache → s3"

# ---------------------------------------------------------------------------
# Content Operation Logging Tests
# ---------------------------------------------------------------------------

class TestContentOperationLogs:
    """Content operations should log at DEBUG with structured fields."""

    def test_read_content_logs(
        self, logged: LoggingBackendWrapper, mock_inner: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        mock_inner.read_content.return_value = HandlerResponse.ok(data=b"content")
        with caplog.at_level(logging.DEBUG, logger="nexus.backends.logging_wrapper"):
            result = logged.read_content("abcdef123456")
        assert result.success
        assert result.data == b"content"
        assert len(caplog.records) == 1
        record = caplog.records[0]
        assert record.levelno == logging.DEBUG
        assert "read_content" in record.message
        assert "abcdef123456" in record.message
        assert "success=True" in record.message
        assert "latency_ms=" in record.message

    def test_write_content_logs(
        self, logged: LoggingBackendWrapper, mock_inner: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        mock_inner.write_content.return_value = HandlerResponse.ok(data="hash123456ab")
        with caplog.at_level(logging.DEBUG, logger="nexus.backends.logging_wrapper"):
            result = logged.write_content(b"hello world")
        assert result.success
        assert len(caplog.records) == 1
        record = caplog.records[0]
        assert "write_content" in record.message
        assert "size=11" in record.message
        assert "success=True" in record.message

    def test_delete_content_logs(
        self, logged: LoggingBackendWrapper, mock_inner: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        mock_inner.delete_content.return_value = HandlerResponse.ok(data=None)
        with caplog.at_level(logging.DEBUG, logger="nexus.backends.logging_wrapper"):
            logged.delete_content("abcdef123456")
        assert len(caplog.records) == 1
        assert "delete_content" in caplog.records[0].message

    def test_content_exists_logs(
        self, logged: LoggingBackendWrapper, mock_inner: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        mock_inner.content_exists.return_value = HandlerResponse.ok(data=True)
        with caplog.at_level(logging.DEBUG, logger="nexus.backends.logging_wrapper"):
            result = logged.content_exists("abcdef123456")
        assert result.data is True
        assert len(caplog.records) == 1
        assert "content_exists" in caplog.records[0].message
        assert "exists=True" in caplog.records[0].message

# ---------------------------------------------------------------------------
# Directory Operation Logging Tests
# ---------------------------------------------------------------------------

class TestDirectoryOperationLogs:
    def test_mkdir_logs(
        self, logged: LoggingBackendWrapper, mock_inner: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        mock_inner.mkdir.return_value = HandlerResponse.ok(data=None)
        with caplog.at_level(logging.DEBUG, logger="nexus.backends.logging_wrapper"):
            logged.mkdir("/test/dir", parents=True)
        assert len(caplog.records) == 1
        assert "mkdir" in caplog.records[0].message
        assert "/test/dir" in caplog.records[0].message

    def test_rmdir_logs(
        self, logged: LoggingBackendWrapper, mock_inner: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        mock_inner.rmdir.return_value = HandlerResponse.ok(data=None)
        with caplog.at_level(logging.DEBUG, logger="nexus.backends.logging_wrapper"):
            logged.rmdir("/test/dir", recursive=True)
        assert len(caplog.records) == 1
        assert "rmdir" in caplog.records[0].message
        assert "recursive=True" in caplog.records[0].message

# ---------------------------------------------------------------------------
# Lifecycle Logging Tests (INFO level)
# ---------------------------------------------------------------------------

class TestLifecycleLogs:
    """Connect/disconnect should log at INFO, not DEBUG."""

    def test_connect_logs_info(
        self, logged: LoggingBackendWrapper, mock_inner: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        mock_inner.connect.return_value = HandlerStatusResponse(success=True)
        with caplog.at_level(logging.INFO, logger="nexus.backends.logging_wrapper"):
            result = logged.connect()
        assert result.success
        assert len(caplog.records) == 1
        assert caplog.records[0].levelno == logging.INFO
        assert "connect" in caplog.records[0].message

    def test_disconnect_logs_info(
        self, logged: LoggingBackendWrapper, mock_inner: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.INFO, logger="nexus.backends.logging_wrapper"):
            logged.disconnect()
        assert len(caplog.records) == 1
        assert caplog.records[0].levelno == logging.INFO
        assert "disconnect" in caplog.records[0].message

# ---------------------------------------------------------------------------
# Delegation Correctness Tests
# ---------------------------------------------------------------------------

class TestDelegationCorrectness:
    """LoggingBackendWrapper should not alter return values or arguments."""

    def test_read_returns_inner_response(
        self, logged: LoggingBackendWrapper, mock_inner: MagicMock
    ) -> None:
        expected = HandlerResponse.ok(data=b"exact-data")
        mock_inner.read_content.return_value = expected
        result = logged.read_content("hash")
        assert result is expected

    def test_write_returns_inner_response(
        self, logged: LoggingBackendWrapper, mock_inner: MagicMock
    ) -> None:
        expected = HandlerResponse.ok(data="hash-result")
        mock_inner.write_content.return_value = expected
        result = logged.write_content(b"data")
        assert result is expected

    def test_failure_response_logged(
        self, logged: LoggingBackendWrapper, mock_inner: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        failed = HandlerResponse.error(message="not found")
        mock_inner.read_content.return_value = failed
        with caplog.at_level(logging.DEBUG, logger="nexus.backends.logging_wrapper"):
            result = logged.read_content("missing-hash")
        assert not result.success
        assert "success=False" in caplog.records[0].message
