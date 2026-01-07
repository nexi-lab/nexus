"""Unit tests for HandlerResponse class.

Tests the standardized response wrapper for backend operations.
"""

import pytest

from nexus.core.exceptions import BackendError, ConflictError, NexusFileNotFoundError
from nexus.core.response import HandlerResponse, ResponseType, timed_response


class TestResponseType:
    """Tests for ResponseType enum."""

    def test_response_type_values(self):
        """Test that ResponseType enum has expected values."""
        assert ResponseType.OK.value == "ok"
        assert ResponseType.ERROR.value == "error"
        assert ResponseType.NOT_FOUND.value == "not_found"
        assert ResponseType.CONFLICT.value == "conflict"


class TestHandlerResponseOk:
    """Tests for HandlerResponse.ok() factory method."""

    def test_ok_with_string_data(self):
        """Test creating success response with string data."""
        response = HandlerResponse.ok(data="content_hash_abc123")
        assert response.success is True
        assert response.resp_type == ResponseType.OK
        assert response.data == "content_hash_abc123"
        assert response.error_message is None
        assert response.error_code is None

    def test_ok_with_bytes_data(self):
        """Test creating success response with bytes data."""
        content = b"Hello, World!"
        response = HandlerResponse.ok(data=content)
        assert response.success is True
        assert response.data == content

    def test_ok_with_int_data(self):
        """Test creating success response with int data (e.g., size)."""
        response = HandlerResponse.ok(data=1024)
        assert response.success is True
        assert response.data == 1024

    def test_ok_with_bool_data(self):
        """Test creating success response with bool data."""
        response = HandlerResponse.ok(data=True)
        assert response.success is True
        assert response.data is True

    def test_ok_with_none_data(self):
        """Test creating success response with None data (e.g., delete operation)."""
        response: HandlerResponse[None] = HandlerResponse.ok(data=None)
        assert response.success is True
        assert response.data is None

    def test_ok_with_all_metadata(self):
        """Test creating success response with all metadata fields."""
        response = HandlerResponse.ok(
            data="hash123",
            execution_time_ms=15.5,
            backend_name="local",
            path="/test/file.txt",
            affected_rows=1,
        )
        assert response.success is True
        assert response.data == "hash123"
        assert response.execution_time_ms == 15.5
        assert response.backend_name == "local"
        assert response.path == "/test/file.txt"
        assert response.affected_rows == 1


class TestHandlerResponseError:
    """Tests for HandlerResponse.error() factory method."""

    def test_error_basic(self):
        """Test creating basic error response."""
        response = HandlerResponse.error(message="Something went wrong")
        assert response.success is False
        assert response.resp_type == ResponseType.ERROR
        assert response.error_message == "Something went wrong"
        assert response.error_code == 500  # Default
        assert response.is_expected_error is False

    def test_error_with_custom_code(self):
        """Test creating error response with custom code."""
        response = HandlerResponse.error(message="Bad request", code=400)
        assert response.error_code == 400

    def test_error_expected(self):
        """Test creating expected error response."""
        response = HandlerResponse.error(
            message="Validation failed",
            code=400,
            is_expected=True,
        )
        assert response.is_expected_error is True
        assert response.error_code == 400

    def test_error_with_metadata(self):
        """Test creating error response with metadata."""
        response = HandlerResponse.error(
            message="Write failed",
            code=500,
            execution_time_ms=10.2,
            backend_name="gcs",
            path="/bucket/file.txt",
        )
        assert response.backend_name == "gcs"
        assert response.path == "/bucket/file.txt"
        assert response.execution_time_ms == 10.2


class TestHandlerResponseNotFound:
    """Tests for HandlerResponse.not_found() factory method."""

    def test_not_found_basic(self):
        """Test creating basic not-found response."""
        response = HandlerResponse.not_found(path="/missing/file.txt")
        assert response.success is False
        assert response.resp_type == ResponseType.NOT_FOUND
        assert response.error_code == 404
        assert response.is_expected_error is True
        assert response.path == "/missing/file.txt"
        assert "Not found" in response.error_message

    def test_not_found_with_custom_message(self):
        """Test creating not-found response with custom message."""
        response = HandlerResponse.not_found(
            path="/custom/path.txt",
            message="Custom not found message",
        )
        assert response.error_message == "Custom not found message"

    def test_not_found_with_metadata(self):
        """Test creating not-found response with metadata."""
        response = HandlerResponse.not_found(
            path="/test.txt",
            execution_time_ms=5.0,
            backend_name="s3",
        )
        assert response.execution_time_ms == 5.0
        assert response.backend_name == "s3"


class TestHandlerResponseConflict:
    """Tests for HandlerResponse.conflict() factory method."""

    def test_conflict_basic(self):
        """Test creating basic conflict response."""
        response = HandlerResponse.conflict(
            path="/test/file.txt",
            expected_etag="abc123def456abc123def456abc123def456",
            current_etag="xyz789xyz789xyz789xyz789xyz789xyz789",
        )
        assert response.success is False
        assert response.resp_type == ResponseType.CONFLICT
        assert response.error_code == 409
        assert response.is_expected_error is True
        assert response.path == "/test/file.txt"
        assert "Conflict detected" in response.error_message
        assert "abc123def456abc1" in response.error_message  # First 16 chars
        assert "xyz789xyz789xyz7" in response.error_message


class TestHandlerResponseFromException:
    """Tests for HandlerResponse.from_exception() factory method."""

    def test_from_nexus_file_not_found_error(self):
        """Test converting NexusFileNotFoundError to response."""
        exc = NexusFileNotFoundError(path="/missing.txt", message="File not found")
        response = HandlerResponse.from_exception(exc)
        assert response.resp_type == ResponseType.NOT_FOUND
        assert response.error_code == 404
        assert response.is_expected_error is True

    def test_from_python_file_not_found_error(self):
        """Test converting Python FileNotFoundError to response."""
        exc = FileNotFoundError("No such file: /test.txt")
        response = HandlerResponse.from_exception(exc)
        assert response.resp_type == ResponseType.NOT_FOUND
        assert response.error_code == 404

    def test_from_conflict_error(self):
        """Test converting ConflictError to response."""
        exc = ConflictError(
            path="/test.txt",
            expected_etag="expected_hash_value_here_1234567",
            current_etag="current_hash_value_here_1234567",
        )
        response = HandlerResponse.from_exception(exc)
        assert response.resp_type == ResponseType.CONFLICT
        assert response.error_code == 409

    def test_from_generic_exception(self):
        """Test converting generic exception to response."""
        exc = ValueError("Invalid value")
        response = HandlerResponse.from_exception(exc)
        assert response.resp_type == ResponseType.ERROR
        assert response.error_code == 500
        assert response.error_message == "Invalid value"

    def test_from_exception_with_metadata(self):
        """Test converting exception with metadata."""
        exc = RuntimeError("Backend unavailable")
        response = HandlerResponse.from_exception(
            exc,
            execution_time_ms=100.0,
            backend_name="gcs",
            path="/bucket/key",
        )
        assert response.execution_time_ms == 100.0
        assert response.backend_name == "gcs"
        assert response.path == "/bucket/key"

    def test_from_exception_respects_is_expected_attribute(self):
        """Test that from_exception uses the is_expected attribute from exceptions.

        Issue #706: Expected vs Unexpected Error Classification
        """
        # Expected error (NexusFileNotFoundError has is_expected=True)
        exc = NexusFileNotFoundError("/test")
        response = HandlerResponse.from_exception(exc)
        assert response.is_expected_error is True

        # Unexpected error (BackendError has is_expected=False)
        exc = BackendError("Connection failed")
        response = HandlerResponse.from_exception(exc)
        assert response.is_expected_error is False


class TestHandlerResponseUnwrap:
    """Tests for HandlerResponse.unwrap() method."""

    def test_unwrap_success(self):
        """Test unwrapping successful response."""
        response = HandlerResponse.ok(data="test_data")
        result = response.unwrap()
        assert result == "test_data"

    def test_unwrap_success_bytes(self):
        """Test unwrapping successful response with bytes."""
        content = b"binary data"
        response = HandlerResponse.ok(data=content)
        result = response.unwrap()
        assert result == content

    def test_unwrap_not_found_raises(self):
        """Test unwrapping not-found response raises exception."""
        response = HandlerResponse.not_found(path="/missing.txt")
        with pytest.raises(NexusFileNotFoundError) as exc_info:
            response.unwrap()
        assert "/missing.txt" in str(exc_info.value) or exc_info.value.path == "/missing.txt"

    def test_unwrap_conflict_raises(self):
        """Test unwrapping conflict response raises exception."""
        response = HandlerResponse.conflict(
            path="/conflict.txt",
            expected_etag="expected_hash_expected_hash_1234",
            current_etag="current_hash_current_hash_12345",
        )
        with pytest.raises(ConflictError):
            response.unwrap()

    def test_unwrap_error_raises_backend_error(self):
        """Test unwrapping error response raises BackendError."""
        response = HandlerResponse.error(
            message="Backend failed",
            backend_name="s3",
            path="/bucket/key",
        )
        with pytest.raises(BackendError) as exc_info:
            response.unwrap()
        assert "Backend failed" in str(exc_info.value)


class TestHandlerResponseUnwrapOr:
    """Tests for HandlerResponse.unwrap_or() method."""

    def test_unwrap_or_success(self):
        """Test unwrap_or with successful response returns data."""
        response = HandlerResponse.ok(data="actual_data")
        result = response.unwrap_or("default")
        assert result == "actual_data"

    def test_unwrap_or_error_returns_default(self):
        """Test unwrap_or with error response returns default."""
        response = HandlerResponse.error(message="Failed")
        result = response.unwrap_or("default_value")
        assert result == "default_value"

    def test_unwrap_or_not_found_returns_default(self):
        """Test unwrap_or with not-found response returns default."""
        response = HandlerResponse.not_found(path="/missing.txt")
        result = response.unwrap_or(b"default content")
        assert result == b"default content"


class TestHandlerResponseToDict:
    """Tests for HandlerResponse.to_dict() method."""

    def test_to_dict_success_minimal(self):
        """Test to_dict with minimal success response."""
        response = HandlerResponse.ok(data="hash123")
        result = response.to_dict()
        assert result["success"] is True
        assert result["resp_type"] == "ok"
        assert result["data"] == "hash123"
        assert "error_message" not in result
        assert "error_code" not in result

    def test_to_dict_success_full(self):
        """Test to_dict with full success response."""
        response = HandlerResponse.ok(
            data="hash123",
            execution_time_ms=15.0,
            backend_name="local",
            path="/test.txt",
            affected_rows=1,
        )
        result = response.to_dict()
        assert result["execution_time_ms"] == 15.0
        assert result["backend_name"] == "local"
        assert result["path"] == "/test.txt"
        assert result["affected_rows"] == 1

    def test_to_dict_error(self):
        """Test to_dict with error response."""
        response = HandlerResponse.error(
            message="Something went wrong",
            code=500,
            backend_name="gcs",
        )
        result = response.to_dict()
        assert result["success"] is False
        assert result["resp_type"] == "error"
        assert result["error_message"] == "Something went wrong"
        assert result["error_code"] == 500
        assert result["backend_name"] == "gcs"

    def test_to_dict_not_found(self):
        """Test to_dict with not-found response."""
        response = HandlerResponse.not_found(path="/missing.txt")
        result = response.to_dict()
        assert result["success"] is False
        assert result["resp_type"] == "not_found"
        assert result["error_code"] == 404
        assert result["is_expected_error"] is True


class TestTimedResponseDecorator:
    """Tests for the timed_response decorator."""

    def test_timed_response_adds_execution_time(self):
        """Test that decorator adds execution time to response."""

        class MockBackend:
            name = "mock"

            @timed_response
            def read_content(self, content_hash: str) -> HandlerResponse[bytes]:
                return HandlerResponse.ok(data=b"content", backend_name=self.name)

        backend = MockBackend()
        response = backend.read_content("hash123")
        assert response.success is True
        # Should have non-zero execution time
        assert response.execution_time_ms >= 0

    def test_timed_response_handles_exception(self):
        """Test that decorator handles exceptions and returns error response."""

        class MockBackend:
            name = "mock"

            @timed_response
            def read_content(self, content_hash: str) -> HandlerResponse[bytes]:
                raise ValueError("Test error")

        backend = MockBackend()
        response = backend.read_content("hash123")
        assert response.success is False
        assert response.resp_type == ResponseType.ERROR
        assert "Test error" in response.error_message
        assert response.backend_name == "mock"

    def test_timed_response_preserves_existing_time(self):
        """Test that decorator doesn't overwrite existing execution time."""

        class MockBackend:
            name = "mock"

            @timed_response
            def read_content(self, content_hash: str) -> HandlerResponse[bytes]:
                return HandlerResponse.ok(
                    data=b"content",
                    backend_name=self.name,
                    execution_time_ms=42.0,  # Pre-set time
                )

        backend = MockBackend()
        response = backend.read_content("hash123")
        # Should preserve the original time since it's non-zero
        # Actually, the decorator only sets if execution_time_ms == 0
        assert response.execution_time_ms == 42.0


class TestHandlerResponseTypeHints:
    """Tests for type hint correctness with HandlerResponse generic."""

    def test_generic_string_type(self):
        """Test HandlerResponse with string type."""
        response: HandlerResponse[str] = HandlerResponse.ok(data="hash")
        result: str = response.unwrap()
        assert result == "hash"

    def test_generic_bytes_type(self):
        """Test HandlerResponse with bytes type."""
        response: HandlerResponse[bytes] = HandlerResponse.ok(data=b"content")
        result: bytes = response.unwrap()
        assert result == b"content"

    def test_generic_int_type(self):
        """Test HandlerResponse with int type."""
        response: HandlerResponse[int] = HandlerResponse.ok(data=1024)
        result: int = response.unwrap()
        assert result == 1024

    def test_generic_bool_type(self):
        """Test HandlerResponse with bool type."""
        response: HandlerResponse[bool] = HandlerResponse.ok(data=True)
        result: bool = response.unwrap()
        assert result is True

    def test_generic_none_type(self):
        """Test HandlerResponse with None type (for void operations)."""
        response: HandlerResponse[None] = HandlerResponse.ok(data=None)
        result: None = response.unwrap()
        assert result is None


class TestHandlerResponseEdgeCases:
    """Tests for edge cases and boundary conditions."""

    def test_success_property_only_true_for_ok(self):
        """Test that success property is only True for OK responses."""
        ok_response = HandlerResponse.ok(data="test")
        error_response = HandlerResponse.error(message="error")
        not_found_response = HandlerResponse.not_found(path="/test")
        conflict_response = HandlerResponse.conflict(
            path="/test",
            expected_etag="expected_hash_value_abc123def456",
            current_etag="current_hash_value_xyz789uvw012",
        )

        assert ok_response.success is True
        assert error_response.success is False
        assert not_found_response.success is False
        assert conflict_response.success is False

    def test_empty_string_data(self):
        """Test handling empty string as data."""
        response = HandlerResponse.ok(data="")
        assert response.success is True
        assert response.data == ""
        assert response.unwrap() == ""

    def test_empty_bytes_data(self):
        """Test handling empty bytes as data."""
        response = HandlerResponse.ok(data=b"")
        assert response.success is True
        assert response.data == b""
        assert response.unwrap() == b""

    def test_zero_execution_time(self):
        """Test handling zero execution time."""
        response = HandlerResponse.ok(data="test", execution_time_ms=0.0)
        result = response.to_dict()
        assert "execution_time_ms" not in result  # Should not include if 0

    def test_negative_execution_time_preserved(self):
        """Test that negative execution time is preserved (edge case)."""
        response = HandlerResponse.ok(data="test", execution_time_ms=-1.0)
        assert response.execution_time_ms == -1.0
