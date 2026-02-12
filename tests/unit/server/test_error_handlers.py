"""Unit tests for error handler exception-to-response mapping.

Tests cover:
- Each exception class maps to the correct HTTP status code and error type
- is_expected flag is propagated to the response
- path attribute is included when present
- ConflictError includes etag fields
- Generic Exception falls through to 500
"""

from __future__ import annotations

from unittest.mock import MagicMock

from nexus.core.exceptions import (
    AuthenticationError,
    BackendError,
    ConflictError,
    InvalidPathError,
    NexusError,
    NexusFileNotFoundError,
    NexusPermissionError,
    ParserError,
    PermissionDeniedError,
    StaleSessionError,
    ValidationError,
)
from nexus.server.error_handlers import nexus_error_handler

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fake_request() -> MagicMock:
    """Build a minimal mock FastAPI Request."""
    return MagicMock()


def _call_handler(exc: Exception) -> tuple[int, dict]:
    """Invoke nexus_error_handler and return (status_code, content)."""
    response = nexus_error_handler(_fake_request(), exc)
    return response.status_code, response.body  # JSONResponse stores rendered body


def _call_handler_parsed(exc: Exception) -> tuple[int, dict]:
    """Invoke handler and parse the JSON content dict."""
    import json

    response = nexus_error_handler(_fake_request(), exc)
    content = json.loads(response.body.decode())
    return response.status_code, content


# ===========================================================================
# Status code mapping
# ===========================================================================


class TestStatusCodeMapping:
    """Each exception type should map to the correct HTTP status code."""

    def test_file_not_found_returns_404(self):
        status, content = _call_handler_parsed(NexusFileNotFoundError("/missing.txt"))
        assert status == 404
        assert content["error"] == "Not Found"

    def test_permission_error_returns_403(self):
        status, content = _call_handler_parsed(NexusPermissionError("/secret"))
        assert status == 403
        assert content["error"] == "Forbidden"

    def test_permission_denied_error_returns_403(self):
        status, content = _call_handler_parsed(
            PermissionDeniedError("No read access")
        )
        assert status == 403
        assert content["error"] == "Forbidden"

    def test_authentication_error_returns_401(self):
        status, content = _call_handler_parsed(
            AuthenticationError("Token expired")
        )
        assert status == 401
        assert content["error"] == "Unauthorized"

    def test_invalid_path_returns_400(self):
        status, content = _call_handler_parsed(
            InvalidPathError("/bad/../path")
        )
        assert status == 400
        assert content["error"] == "Bad Request"

    def test_validation_error_returns_400(self):
        status, content = _call_handler_parsed(
            ValidationError("name is required")
        )
        assert status == 400
        assert content["error"] == "Bad Request"

    def test_conflict_error_returns_409(self):
        status, content = _call_handler_parsed(
            ConflictError("/file.txt", "etag-expected", "etag-actual")
        )
        assert status == 409
        assert content["error"] == "Conflict"

    def test_stale_session_error_returns_409(self):
        status, content = _call_handler_parsed(
            StaleSessionError("agent-001")
        )
        assert status == 409
        assert content["error"] == "Conflict"

    def test_parser_error_returns_422(self):
        status, content = _call_handler_parsed(
            ParserError("Unsupported format")
        )
        assert status == 422
        assert content["error"] == "Unprocessable Entity"

    def test_backend_error_returns_502(self):
        status, content = _call_handler_parsed(
            BackendError("GCS timeout", backend="gcs")
        )
        assert status == 502
        assert content["error"] == "Bad Gateway"

    def test_generic_nexus_error_returns_500(self):
        status, content = _call_handler_parsed(
            NexusError("Something broke")
        )
        assert status == 500
        assert content["error"] == "Internal Server Error"

    def test_unknown_exception_returns_500(self):
        """Non-NexusError exceptions should still get a 500 response."""
        status, content = _call_handler_parsed(
            RuntimeError("unexpected")
        )
        assert status == 500
        assert content["error"] == "Internal Server Error"


# ===========================================================================
# Response content
# ===========================================================================


class TestResponseContent:
    """Verify response body includes expected fields."""

    def test_detail_contains_message(self):
        _, content = _call_handler_parsed(NexusError("helpful message"))
        assert "helpful message" in content["detail"]

    def test_is_expected_true_for_user_errors(self):
        _, content = _call_handler_parsed(NexusFileNotFoundError("/x"))
        assert content["is_expected"] is True

    def test_is_expected_false_for_system_errors(self):
        _, content = _call_handler_parsed(BackendError("disk full"))
        assert content["is_expected"] is False

    def test_is_expected_false_for_unknown_exceptions(self):
        _, content = _call_handler_parsed(RuntimeError("oops"))
        assert content["is_expected"] is False

    def test_path_included_when_present(self):
        _, content = _call_handler_parsed(NexusFileNotFoundError("/data/file.txt"))
        assert content["path"] == "/data/file.txt"

    def test_path_absent_when_not_set(self):
        _, content = _call_handler_parsed(NexusError("no path"))
        assert "path" not in content

    def test_conflict_error_includes_etags(self):
        _, content = _call_handler_parsed(
            ConflictError("/file.txt", "expected-abc", "actual-xyz")
        )
        assert content["expected_etag"] == "expected-abc"
        assert content["current_etag"] == "actual-xyz"

    def test_conflict_etags_not_present_on_non_conflict(self):
        _, content = _call_handler_parsed(NexusFileNotFoundError("/x"))
        assert "expected_etag" not in content
        assert "current_etag" not in content
