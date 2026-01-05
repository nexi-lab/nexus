"""Unit tests for Backend handler interface (Issue #708).

Tests the new handler interface methods added to the Backend base class:
- HandlerStatusResponse dataclass
- is_connected property
- thread_safe property
- connect() method
- disconnect() method
- check_connection() method
"""

import pytest

from nexus.backends.backend import HandlerStatusResponse
from nexus.backends.local import LocalBackend


class TestHandlerStatusResponse:
    """Tests for HandlerStatusResponse dataclass."""

    def test_success_response(self):
        """Test creating a successful response."""
        response = HandlerStatusResponse(success=True, latency_ms=1.5)

        assert response.success is True
        assert response.error_message is None
        assert response.latency_ms == 1.5
        assert response.details == {}

    def test_failure_response(self):
        """Test creating a failure response."""
        response = HandlerStatusResponse(
            success=False,
            error_message="Connection timeout",
            latency_ms=5000.0,
            details={"backend": "s3", "bucket": "test-bucket"},
        )

        assert response.success is False
        assert response.error_message == "Connection timeout"
        assert response.latency_ms == 5000.0
        assert response.details == {"backend": "s3", "bucket": "test-bucket"}

    def test_to_dict_minimal(self):
        """Test to_dict with minimal fields."""
        response = HandlerStatusResponse(success=True)
        result = response.to_dict()

        assert result == {"success": True}

    def test_to_dict_full(self):
        """Test to_dict with all fields populated."""
        response = HandlerStatusResponse(
            success=False,
            error_message="Auth failed",
            latency_ms=100.5,
            details={"user": "test@example.com"},
        )
        result = response.to_dict()

        assert result == {
            "success": False,
            "error_message": "Auth failed",
            "latency_ms": 100.5,
            "details": {"user": "test@example.com"},
        }

    def test_to_dict_excludes_none_values(self):
        """Test that to_dict excludes None values."""
        response = HandlerStatusResponse(success=True, latency_ms=5.0)
        result = response.to_dict()

        # Should not include error_message or details keys
        assert "error_message" not in result
        assert "details" not in result
        assert result == {"success": True, "latency_ms": 5.0}


class TestBackendHandlerInterface:
    """Tests for Backend base class handler interface."""

    @pytest.fixture
    def local_backend(self, tmp_path):
        """Create a local backend for testing."""
        return LocalBackend(root_path=tmp_path / "backend")

    def test_is_connected_default(self, local_backend):
        """Test that is_connected defaults to True for stateless backends."""
        assert local_backend.is_connected is True

    def test_thread_safe_default(self, local_backend):
        """Test that thread_safe defaults to True."""
        assert local_backend.thread_safe is True

    def test_connect_returns_success(self, local_backend):
        """Test that connect() returns success for stateless backends."""
        result = local_backend.connect()

        assert isinstance(result, HandlerStatusResponse)
        assert result.success is True
        assert result.details.get("backend") == "local"

    def test_disconnect_is_noop(self, local_backend):
        """Test that disconnect() is a no-op for stateless backends."""
        # Should not raise any exception
        local_backend.disconnect()

    def test_check_connection_success(self, local_backend):
        """Test that check_connection() returns success for healthy backend."""
        result = local_backend.check_connection()

        assert isinstance(result, HandlerStatusResponse)
        assert result.success is True
        assert result.latency_ms is not None
        assert result.latency_ms >= 0
        assert result.details.get("backend") == "local"

    def test_check_connection_includes_latency(self, local_backend):
        """Test that check_connection() measures latency."""
        result = local_backend.check_connection()

        assert result.latency_ms is not None
        # Latency should be very small for local backend
        assert result.latency_ms < 100  # Less than 100ms

    def test_check_connection_includes_user_scoped_flag(self, local_backend):
        """Test that check_connection() includes user_scoped in details."""
        result = local_backend.check_connection()

        assert "user_scoped" in result.details
        assert result.details["user_scoped"] is False


class TestHandlerStatusResponseEdgeCases:
    """Edge case tests for HandlerStatusResponse."""

    def test_empty_details_not_included_in_to_dict(self):
        """Test that empty details dict is not included in to_dict output."""
        response = HandlerStatusResponse(success=True, details={})
        result = response.to_dict()

        assert "details" not in result

    def test_zero_latency_included(self):
        """Test that zero latency is included (not treated as falsy)."""
        response = HandlerStatusResponse(success=True, latency_ms=0.0)
        result = response.to_dict()

        assert result["latency_ms"] == 0.0

    def test_empty_error_message_included(self):
        """Test that empty string error message is included."""
        response = HandlerStatusResponse(success=False, error_message="")
        result = response.to_dict()

        # Empty string is falsy, so it should not be included
        assert "error_message" not in result


class TestBackendHandlerInterfaceInheritance:
    """Test that handler interface is properly inherited."""

    def test_local_backend_inherits_interface(self, tmp_path):
        """Test that LocalBackend properly inherits handler interface."""
        backend = LocalBackend(root_path=tmp_path / "backend")

        # Should have all handler interface methods
        assert hasattr(backend, "is_connected")
        assert hasattr(backend, "thread_safe")
        assert hasattr(backend, "connect")
        assert hasattr(backend, "disconnect")
        assert hasattr(backend, "check_connection")

        # Methods should be callable
        assert callable(backend.connect)
        assert callable(backend.disconnect)
        assert callable(backend.check_connection)
