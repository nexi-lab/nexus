"""Unit tests for error handler HTTP status code mappings (Issue #1254).

Tests that every NexusError subclass maps to the correct HTTP status code
via the centralized nexus_error_handler.
"""

from unittest.mock import MagicMock

from nexus.contracts.exceptions import (
    AuditLogError,
    AuthenticationError,
    BackendError,
    ConflictError,
    ConnectorAuthError,
    ConnectorError,
    ConnectorQuotaError,
    ConnectorRateLimitError,
    DatabaseConnectionError,
    DatabaseError,
    DatabaseIntegrityError,
    DatabaseTimeoutError,
    InvalidPathError,
    MetadataError,
    NexusError,
    NexusFileNotFoundError,
    NexusPermissionError,
    ParserError,
    PermissionDeniedError,
    ServiceUnavailableError,
    StaleSessionError,
    ValidationError,
)
from nexus.server.error_handlers import nexus_error_handler


def _call_handler(exc: Exception) -> tuple[int, dict]:
    """Helper to call nexus_error_handler and return (status_code, content)."""
    request = MagicMock()
    response = nexus_error_handler(request, exc)
    return response.status_code, response.body


class TestExpectedErrors:
    """Test HTTP mappings for expected (user) errors."""

    def test_file_not_found_returns_404(self) -> None:
        resp = nexus_error_handler(MagicMock(), NexusFileNotFoundError("/test"))
        assert resp.status_code == 404

    def test_permission_error_returns_403(self) -> None:
        resp = nexus_error_handler(MagicMock(), NexusPermissionError("/test"))
        assert resp.status_code == 403

    def test_permission_denied_error_returns_403(self) -> None:
        resp = nexus_error_handler(MagicMock(), PermissionDeniedError("No access"))
        assert resp.status_code == 403

    def test_authentication_error_returns_401(self) -> None:
        resp = nexus_error_handler(MagicMock(), AuthenticationError("Token expired"))
        assert resp.status_code == 401

    def test_authentication_error_serializes_recovery_hint(self) -> None:
        """recovery_hint must round-trip through the JSON response.

        Without this, the structured re-auth pointer that the gdrive
        transport attaches to AuthenticationError is silently dropped at
        the API boundary and clients cannot drive recovery.
        """
        import json

        hint = {
            "endpoint": "/api/v2/connectors/auth/init",
            "method": "POST",
            "connector_name": "gdrive_connector",
            "provider": "google-drive",
            "user_email": "user@example.com",
        }
        exc = AuthenticationError(
            "Token expired",
            provider="google-drive",
            user_email="user@example.com",
            recovery_hint=hint,
        )
        resp = nexus_error_handler(MagicMock(), exc)
        assert resp.status_code == 401
        body = json.loads(resp.body)
        assert body["provider"] == "google-drive"
        assert body["user_email"] == "user@example.com"
        assert body["recovery_hint"] == hint

    def test_invalid_path_returns_400(self) -> None:
        resp = nexus_error_handler(MagicMock(), InvalidPathError("../../etc/passwd"))
        assert resp.status_code == 400

    def test_validation_error_returns_400(self) -> None:
        resp = nexus_error_handler(MagicMock(), ValidationError("Invalid input"))
        assert resp.status_code == 400

    def test_conflict_error_returns_409(self) -> None:
        resp = nexus_error_handler(MagicMock(), ConflictError("/test", "etag1", "etag2"))
        assert resp.status_code == 409

    def test_stale_session_error_returns_409(self) -> None:
        resp = nexus_error_handler(MagicMock(), StaleSessionError("agent-1"))
        assert resp.status_code == 409

    def test_parser_error_returns_422(self) -> None:
        resp = nexus_error_handler(MagicMock(), ParserError("Cannot parse"))
        assert resp.status_code == 422


class TestUnexpectedErrors:
    """Test HTTP mappings for unexpected (system) errors."""

    def test_backend_error_returns_502(self) -> None:
        resp = nexus_error_handler(MagicMock(), BackendError("Connection failed"))
        assert resp.status_code == 502

    def test_metadata_error_returns_500(self) -> None:
        resp = nexus_error_handler(MagicMock(), MetadataError("DB error"))
        assert resp.status_code == 500

    def test_service_unavailable_returns_503(self) -> None:
        resp = nexus_error_handler(MagicMock(), ServiceUnavailableError("Down"))
        assert resp.status_code == 503

    def test_audit_log_error_returns_500(self) -> None:
        resp = nexus_error_handler(MagicMock(), AuditLogError("Audit failed"))
        assert resp.status_code == 500

    def test_generic_nexus_error_returns_500(self) -> None:
        resp = nexus_error_handler(MagicMock(), NexusError("Something broke"))
        assert resp.status_code == 500

    def test_unknown_exception_returns_500(self) -> None:
        resp = nexus_error_handler(MagicMock(), RuntimeError("unknown"))
        assert resp.status_code == 500


class TestNewExceptionTypes:
    """Test HTTP mappings for new Database/Connector exception types (Issue #1254)."""

    def test_database_error_returns_502(self) -> None:
        resp = nexus_error_handler(MagicMock(), DatabaseError("Connection lost"))
        assert resp.status_code == 502

    def test_database_connection_error_returns_502(self) -> None:
        resp = nexus_error_handler(MagicMock(), DatabaseConnectionError("Refused"))
        assert resp.status_code == 502

    def test_database_timeout_error_returns_502(self) -> None:
        resp = nexus_error_handler(MagicMock(), DatabaseTimeoutError("Timed out"))
        assert resp.status_code == 502

    def test_database_integrity_error_returns_502(self) -> None:
        resp = nexus_error_handler(MagicMock(), DatabaseIntegrityError("Dup key"))
        assert resp.status_code == 502

    def test_connector_error_returns_502(self) -> None:
        resp = nexus_error_handler(MagicMock(), ConnectorError("API failed"))
        assert resp.status_code == 502

    def test_connector_auth_error_returns_401(self) -> None:
        resp = nexus_error_handler(MagicMock(), ConnectorAuthError("Token expired"))
        assert resp.status_code == 401

    def test_connector_rate_limit_error_returns_429(self) -> None:
        resp = nexus_error_handler(MagicMock(), ConnectorRateLimitError("Rate limited"))
        assert resp.status_code == 429

    def test_connector_quota_error_returns_502(self) -> None:
        resp = nexus_error_handler(MagicMock(), ConnectorQuotaError("Quota exceeded"))
        assert resp.status_code == 502


class TestResponseContent:
    """Test response content structure."""

    def test_response_includes_is_expected(self) -> None:
        import json

        resp = nexus_error_handler(MagicMock(), ValidationError("Bad input"))
        body = json.loads(resp.body)
        assert body["is_expected"] is True

        resp = nexus_error_handler(MagicMock(), BackendError("System failure"))
        body = json.loads(resp.body)
        assert body["is_expected"] is False

    def test_response_includes_path_when_present(self) -> None:
        import json

        resp = nexus_error_handler(MagicMock(), NexusFileNotFoundError("/missing/file"))
        body = json.loads(resp.body)
        assert body["path"] == "/missing/file"

    def test_conflict_response_includes_etag_data(self) -> None:
        import json

        resp = nexus_error_handler(MagicMock(), ConflictError("/test", "etag-old", "etag-new"))
        body = json.loads(resp.body)
        assert body["expected_etag"] == "etag-old"
        assert body["current_etag"] == "etag-new"

    def test_is_expected_controls_classification(self) -> None:
        """Verify is_expected flag matches exception class defaults."""
        import json

        # Expected errors
        for exc in [
            NexusFileNotFoundError("/test"),
            ValidationError("Bad"),
            AuthenticationError("Expired"),
            ConnectorAuthError("Token revoked"),
            ConnectorRateLimitError("Rate limited"),
            DatabaseIntegrityError("Duplicate"),
        ]:
            resp = nexus_error_handler(MagicMock(), exc)
            body = json.loads(resp.body)
            assert body["is_expected"] is True, f"{type(exc).__name__} should be expected"

        # Unexpected errors
        for exc in [
            BackendError("Failed"),
            DatabaseError("Connection lost"),
            ConnectorError("API failed"),
            DatabaseConnectionError("Refused"),
        ]:
            resp = nexus_error_handler(MagicMock(), exc)
            body = json.loads(resp.body)
            assert body["is_expected"] is False, f"{type(exc).__name__} should be unexpected"
