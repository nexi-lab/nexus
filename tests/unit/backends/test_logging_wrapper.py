"""Unit tests for LoggingBackendWrapper (#1449, #2077).

Tests verify:
1. All operations produce structured debug log output
2. Log messages contain operation name, latency, and success/failure
3. Lifecycle events (connect/disconnect) log at INFO level
4. Delegation passes through correctly to inner backend
5. Exception logging (#2077, Issue 11)
6. batch_read_content logging (#2077, Issue 7)
7. check_connection logging (#2077, Issue 7)

Uses pytest's caplog fixture for log capture.

Design reference:
    - NEXUS-LEGO-ARCHITECTURE.md PART 16, Recursive Wrapping (Mechanism 2)
    - Issue #2077: Deduplicate backend wrapper boilerplate
"""

import logging
from unittest.mock import MagicMock

import pytest

from nexus.backends.base.backend import HandlerStatusResponse
from nexus.backends.wrappers.logging import LoggingBackendWrapper
from nexus.core.object_store import WriteResult
from tests.unit.backends.wrapper_test_helpers import make_leaf

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_inner() -> MagicMock:
    """Create a mock Backend for testing."""
    return make_leaf("test-backend")


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
        mock_inner.read_content.return_value = b"content"
        with caplog.at_level(logging.DEBUG, logger="nexus.backends.wrappers.logging"):
            result = logged.read_content("abcdef123456")
        assert result == b"content"
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
        mock_inner.write_content.return_value = WriteResult(content_hash="hash123456ab", size=11)
        with caplog.at_level(logging.DEBUG, logger="nexus.backends.wrappers.logging"):
            result = logged.write_content(b"hello world")
        assert isinstance(result, WriteResult)
        assert len(caplog.records) == 1
        record = caplog.records[0]
        assert "write_content" in record.message
        assert "size=11" in record.message
        assert "success=True" in record.message

    def test_delete_content_logs(
        self, logged: LoggingBackendWrapper, mock_inner: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        mock_inner.delete_content.return_value = None
        with caplog.at_level(logging.DEBUG, logger="nexus.backends.wrappers.logging"):
            logged.delete_content("abcdef123456")
        assert len(caplog.records) == 1
        assert "delete_content" in caplog.records[0].message

    def test_content_exists_logs(
        self, logged: LoggingBackendWrapper, mock_inner: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        mock_inner.content_exists.return_value = True
        with caplog.at_level(logging.DEBUG, logger="nexus.backends.wrappers.logging"):
            result = logged.content_exists("abcdef123456")
        assert result is True
        assert len(caplog.records) == 1
        assert "content_exists" in caplog.records[0].message
        assert "exists=True" in caplog.records[0].message


# ---------------------------------------------------------------------------
# Batch Read Logging Tests (#2077, Issue 7)
# ---------------------------------------------------------------------------


class TestBatchReadLogs:
    """batch_read_content should be logged with count and hits."""

    def test_batch_read_content_logs(
        self, logged: LoggingBackendWrapper, mock_inner: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        mock_inner.batch_read_content.return_value = {
            "hash1": b"data1",
            "hash2": None,
            "hash3": b"data3",
        }
        with caplog.at_level(logging.DEBUG, logger="nexus.backends.wrappers.logging"):
            results = logged.batch_read_content(["hash1", "hash2", "hash3"])
        assert results["hash1"] == b"data1"
        assert results["hash2"] is None
        assert len(caplog.records) == 1
        record = caplog.records[0]
        assert "batch_read_content" in record.message
        assert "count=3" in record.message
        assert "hits=2" in record.message
        assert "latency_ms=" in record.message


# ---------------------------------------------------------------------------
# Directory Operation Logging Tests
# ---------------------------------------------------------------------------


class TestDirectoryOperationLogs:
    def test_mkdir_logs(
        self, logged: LoggingBackendWrapper, mock_inner: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        mock_inner.mkdir.return_value = None
        with caplog.at_level(logging.DEBUG, logger="nexus.backends.wrappers.logging"):
            logged.mkdir("/test/dir", parents=True)
        assert len(caplog.records) == 1
        assert "mkdir" in caplog.records[0].message
        assert "/test/dir" in caplog.records[0].message

    def test_rmdir_logs(
        self, logged: LoggingBackendWrapper, mock_inner: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        mock_inner.rmdir.return_value = None
        with caplog.at_level(logging.DEBUG, logger="nexus.backends.wrappers.logging"):
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
        with caplog.at_level(logging.INFO, logger="nexus.backends.wrappers.logging"):
            result = logged.connect()
        assert result.success
        assert len(caplog.records) == 1
        assert caplog.records[0].levelno == logging.INFO
        assert "connect" in caplog.records[0].message

    def test_disconnect_logs_info(
        self, logged: LoggingBackendWrapper, mock_inner: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.INFO, logger="nexus.backends.wrappers.logging"):
            logged.disconnect()
        assert len(caplog.records) == 1
        assert caplog.records[0].levelno == logging.INFO
        assert "disconnect" in caplog.records[0].message


# ---------------------------------------------------------------------------
# Check Connection Logging Tests (#2077, Issue 7)
# ---------------------------------------------------------------------------


class TestCheckConnectionLogs:
    """check_connection should be logged."""

    def test_check_connection_logs(
        self, logged: LoggingBackendWrapper, mock_inner: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        mock_inner.check_connection.return_value = HandlerStatusResponse(success=True)
        with caplog.at_level(logging.DEBUG, logger="nexus.backends.wrappers.logging"):
            result = logged.check_connection()
        assert result.success
        assert len(caplog.records) == 1
        assert "check_connection" in caplog.records[0].message
        assert "success=True" in caplog.records[0].message


# ---------------------------------------------------------------------------
# Delegation Correctness Tests
# ---------------------------------------------------------------------------


class TestDelegationCorrectness:
    """LoggingBackendWrapper should not alter return values or arguments."""

    def test_read_returns_inner_response(
        self, logged: LoggingBackendWrapper, mock_inner: MagicMock
    ) -> None:
        expected = b"exact-data"
        mock_inner.read_content.return_value = expected
        result = logged.read_content("hash")
        assert result is expected

    def test_write_returns_inner_response(
        self, logged: LoggingBackendWrapper, mock_inner: MagicMock
    ) -> None:
        expected = WriteResult(content_hash="hash-result")
        mock_inner.write_content.return_value = expected
        result = logged.write_content(b"data")
        assert result is expected

    def test_failure_response_logged(
        self, logged: LoggingBackendWrapper, mock_inner: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        mock_inner.read_content.side_effect = RuntimeError("not found")
        with (
            caplog.at_level(logging.DEBUG, logger="nexus.backends.wrappers.logging"),
            pytest.raises(RuntimeError, match="not found"),
        ):
            logged.read_content("missing-hash")
        assert len(caplog.records) == 1
        record = caplog.records[0]
        assert "read_content" in record.message
        assert "error=" in record.message
        assert "not found" in record.message


# ---------------------------------------------------------------------------
# Exception Logging Tests (#2077, Issue 11)
# ---------------------------------------------------------------------------


class TestExceptionLogging:
    """Exceptions from inner backend should be logged with latency before re-raising."""

    def test_read_exception_logged(
        self, logged: LoggingBackendWrapper, mock_inner: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        mock_inner.read_content.side_effect = RuntimeError("connection lost")
        with (
            caplog.at_level(logging.DEBUG, logger="nexus.backends.wrappers.logging"),
            pytest.raises(RuntimeError, match="connection lost"),
        ):
            logged.read_content("some-hash")
        assert len(caplog.records) == 1
        record = caplog.records[0]
        assert "read_content" in record.message
        assert "error=" in record.message
        assert "connection lost" in record.message
        assert "latency_ms=" in record.message

    def test_write_exception_logged(
        self, logged: LoggingBackendWrapper, mock_inner: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        mock_inner.write_content.side_effect = OSError("disk full")
        with (
            caplog.at_level(logging.DEBUG, logger="nexus.backends.wrappers.logging"),
            pytest.raises(OSError, match="disk full"),
        ):
            logged.write_content(b"data")
        assert len(caplog.records) == 1
        assert "error=" in caplog.records[0].message

    def test_connect_exception_logged_at_info(
        self, logged: LoggingBackendWrapper, mock_inner: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        mock_inner.connect.side_effect = ConnectionError("refused")
        with (
            caplog.at_level(logging.INFO, logger="nexus.backends.wrappers.logging"),
            pytest.raises(ConnectionError, match="refused"),
        ):
            logged.connect()
        assert len(caplog.records) == 1
        assert caplog.records[0].levelno == logging.INFO
        assert "error=" in caplog.records[0].message
