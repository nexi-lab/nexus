"""Unit tests for Nexus logging utilities (Issue #706)."""

import logging
from unittest.mock import MagicMock

from nexus.core.exceptions import (
    BackendError,
    NexusFileNotFoundError,
    ValidationError,
)
from nexus.core.logging_utils import (
    get_log_level_for_error,
    log_error,
    log_exception,
    should_alert,
)


def test_log_error_expected() -> None:
    """Test log_error logs expected errors at INFO level."""
    logger = MagicMock(spec=logging.Logger)

    # NexusFileNotFoundError is expected
    error = NexusFileNotFoundError("/missing/file.txt")
    log_error(logger, error)

    # Should log at INFO level
    logger.info.assert_called_once()
    assert "Expected error" in logger.info.call_args[0][0]
    assert "/missing/file.txt" in logger.info.call_args[0][0]

    # Should NOT log at ERROR level
    logger.error.assert_not_called()


def test_log_error_unexpected() -> None:
    """Test log_error logs unexpected errors at ERROR level with traceback."""
    logger = MagicMock(spec=logging.Logger)

    # BackendError is unexpected
    error = BackendError("Connection failed")
    log_error(logger, error)

    # Should log at ERROR level with exc_info=True
    logger.error.assert_called_once()
    assert "System error" in logger.error.call_args[0][0]
    assert "Connection failed" in logger.error.call_args[0][0]
    assert logger.error.call_args.kwargs.get("exc_info") is True

    # Should NOT log at INFO level
    logger.info.assert_not_called()


def test_log_error_with_operation() -> None:
    """Test log_error includes operation name in context."""
    logger = MagicMock(spec=logging.Logger)

    error = ValidationError("Invalid input")
    log_error(logger, error, operation="create_file")

    logger.info.assert_called_once()
    log_message = logger.info.call_args[0][0]
    assert "operation=create_file" in log_message


def test_log_error_with_context() -> None:
    """Test log_error includes additional context."""
    logger = MagicMock(spec=logging.Logger)

    error = BackendError("Timeout")
    log_error(logger, error, context={"backend": "gcs", "retry": 3})

    logger.error.assert_called_once()
    log_message = logger.error.call_args[0][0]
    assert "backend=gcs" in log_message
    assert "retry=3" in log_message


def test_log_error_traceback_override() -> None:
    """Test log_error respects include_traceback override."""
    logger = MagicMock(spec=logging.Logger)

    # Expected error with forced traceback
    error = ValidationError("Invalid")
    log_error(logger, error, include_traceback=True)

    # Still logs at INFO but with traceback
    logger.info.assert_called_once()

    # Reset
    logger.reset_mock()

    # Unexpected error without traceback
    error = BackendError("Failed")
    log_error(logger, error, include_traceback=False)

    logger.error.assert_called_once()
    # exc_info should be False
    assert logger.error.call_args.kwargs.get("exc_info") is False


def test_log_exception_expected() -> None:
    """Test log_exception with expected error."""
    logger = MagicMock(spec=logging.Logger)

    error = NexusFileNotFoundError("/test")
    log_exception(logger, error, "Failed to read file")

    logger.info.assert_called_once()
    log_message = logger.info.call_args[0][0]
    assert "Failed to read file" in log_message


def test_log_exception_unexpected() -> None:
    """Test log_exception with unexpected error."""
    logger = MagicMock(spec=logging.Logger)

    error = BackendError("Database down")
    log_exception(logger, error, "Operation failed", operation="sync")

    logger.error.assert_called_once()
    log_message = logger.error.call_args[0][0]
    assert "Operation failed" in log_message
    assert "operation=sync" in log_message
    assert logger.error.call_args.kwargs.get("exc_info") is True


def test_should_alert_expected() -> None:
    """Test should_alert returns False for expected errors."""
    assert should_alert(NexusFileNotFoundError("/test")) is False
    assert should_alert(ValidationError("Invalid")) is False


def test_should_alert_unexpected() -> None:
    """Test should_alert returns True for unexpected errors."""
    assert should_alert(BackendError("Failed")) is True

    # Generic exception (no is_expected attr) should alert
    assert should_alert(Exception("Unknown error")) is True


def test_get_log_level_for_error() -> None:
    """Test get_log_level_for_error returns correct levels."""
    # Expected errors -> INFO
    assert get_log_level_for_error(NexusFileNotFoundError("/test")) == logging.INFO
    assert get_log_level_for_error(ValidationError("Invalid")) == logging.INFO

    # Unexpected errors -> ERROR
    assert get_log_level_for_error(BackendError("Failed")) == logging.ERROR

    # Generic exception (no is_expected) -> ERROR
    assert get_log_level_for_error(Exception("Unknown")) == logging.ERROR
