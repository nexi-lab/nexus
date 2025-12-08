"""Unit tests for Gmail connector backend."""

from unittest.mock import AsyncMock, Mock, patch

import pytest

from nexus.backends.gmail_connector import GmailConnectorBackend
from nexus.core.exceptions import BackendError
from nexus.core.permissions import OperationContext


@pytest.fixture
def mock_token_manager():
    """Create a mock TokenManager."""
    with patch("nexus.server.auth.token_manager.TokenManager") as mock_tm:
        mock_instance = Mock()
        mock_instance.get_valid_token = AsyncMock(return_value="test-access-token")
        mock_instance.register_provider = Mock()
        mock_tm.return_value = mock_instance
        yield mock_tm


@pytest.fixture
def mock_oauth_factory():
    """Create a mock OAuthProviderFactory."""
    with patch("nexus.server.auth.oauth_factory.OAuthProviderFactory") as mock_factory:
        mock_instance = Mock()
        mock_provider = Mock()
        mock_instance.create_provider.return_value = mock_provider
        mock_factory.return_value = mock_instance
        yield mock_factory


@pytest.fixture
def gmail_connector(mock_token_manager, mock_oauth_factory) -> GmailConnectorBackend:
    """Create a Gmail connector instance."""
    return GmailConnectorBackend(
        token_manager_db="sqlite:///test.db",
        user_email="test@example.com",
        provider="gmail",
        max_results=10,
        labels=["INBOX"],
    )


class TestGmailConnectorInitialization:
    """Test Gmail connector initialization."""

    def test_init_with_db_path(self, mock_token_manager, mock_oauth_factory) -> None:
        """Test initialization with database path."""
        backend = GmailConnectorBackend(
            token_manager_db="/path/to/nexus.db",
            user_email="test@example.com",
        )

        assert backend.name == "gmail"
        assert backend.user_email == "test@example.com"
        assert backend.provider == "gmail"
        assert backend.max_results == 100
        assert backend.labels == ["INBOX"]
        assert backend.user_scoped is True

    def test_init_with_db_url(self, mock_token_manager, mock_oauth_factory) -> None:
        """Test initialization with database URL."""
        backend = GmailConnectorBackend(
            token_manager_db="postgresql://user:pass@localhost/nexus",
            user_email="test@example.com",
        )

        assert backend.name == "gmail"
        assert backend.user_email == "test@example.com"

    def test_init_custom_values(self, mock_token_manager, mock_oauth_factory) -> None:
        """Test initialization with custom values."""
        backend = GmailConnectorBackend(
            token_manager_db="sqlite:///test.db",
            user_email="custom@example.com",
            provider="gmail-custom",
            max_results=50,
            labels=["INBOX", "SENT", "DRAFTS"],
        )

        assert backend.user_email == "custom@example.com"
        assert backend.provider == "gmail-custom"
        assert backend.max_results == 50
        assert backend.labels == ["INBOX", "SENT", "DRAFTS"]

    def test_init_without_user_email(self, mock_token_manager, mock_oauth_factory) -> None:
        """Test initialization without user_email (uses context)."""
        backend = GmailConnectorBackend(
            token_manager_db="sqlite:///test.db",
            user_email=None,
        )

        assert backend.user_email is None
        assert backend.name == "gmail"


class TestGmailConnectorProperties:
    """Test Gmail connector properties."""

    def test_name_property(self, gmail_connector) -> None:
        """Test name property."""
        assert gmail_connector.name == "gmail"

    def test_user_scoped_property(self, gmail_connector) -> None:
        """Test user_scoped property."""
        assert gmail_connector.user_scoped is True

    def test_has_caching_with_session_factory(self, mock_token_manager, mock_oauth_factory) -> None:
        """Test _has_caching with session_factory."""
        mock_session_factory = Mock()
        backend = GmailConnectorBackend(
            token_manager_db="sqlite:///test.db",
            user_email="test@example.com",
            session_factory=mock_session_factory,
        )

        assert backend._has_caching() is True

    def test_has_caching_without_session(self, gmail_connector) -> None:
        """Test _has_caching without session."""
        assert gmail_connector._has_caching() is False


class TestGmailConnectorYAMLCreation:
    """Test YAML content creation."""

    def test_create_yaml_content_basic(self, gmail_connector) -> None:
        """Test creating YAML content with basic email."""
        headers = {
            "from": "sender@example.com",
            "to": "recipient@example.com",
            "subject": "Test Email",
            "date": "Mon, 1 Jan 2024 12:00:00 +0000",
        }
        text_body = "Hello World!\n\nThis is a test email."
        html_body = "<html><body><p>Hello World!</p><p>This is a test email.</p></body></html>"
        labels = ["INBOX"]

        yaml_content = gmail_connector._create_yaml_content(headers, text_body, html_body, labels)

        assert "from: sender@example.com" in yaml_content
        assert "to: recipient@example.com" in yaml_content
        assert "subject: Test Email" in yaml_content
        assert "text_body: |" in yaml_content or "text_body:" in yaml_content
        assert "Hello World!" in yaml_content
        assert "labels:" in yaml_content
        assert "- INBOX" in yaml_content

    def test_create_yaml_content_with_html(self, gmail_connector) -> None:
        """Test creating YAML content with HTML body."""
        headers = {
            "from": "sender@example.com",
            "to": "recipient@example.com",
            "subject": "Test",
            "date": "Mon, 1 Jan 2024 12:00:00 +0000",
        }
        text_body = "Plain text"
        html_body = "<html><body>HTML content</body></html>"

        yaml_content = gmail_connector._create_yaml_content(headers, text_body, html_body, None)

        assert "text_body:" in yaml_content
        assert "html_body:" in yaml_content
        assert "HTML content" in yaml_content

    def test_create_yaml_content_without_html(self, gmail_connector) -> None:
        """Test creating YAML content without HTML body."""
        headers = {
            "from": "sender@example.com",
            "to": "recipient@example.com",
            "subject": "Test",
            "date": "Mon, 1 Jan 2024 12:00:00 +0000",
        }
        text_body = "Plain text only"
        html_body = ""

        yaml_content = gmail_connector._create_yaml_content(headers, text_body, html_body, None)

        assert "text_body:" in yaml_content
        assert "html_body:" not in yaml_content


class TestGmailConnectorReadOnly:
    """Test read-only operations."""

    def test_write_content_raises_error(self, gmail_connector) -> None:
        """Test that write_content raises BackendError."""
        with pytest.raises(BackendError, match="read-only"):
            gmail_connector.write_content(b"test content")

    def test_delete_content_raises_error(self, gmail_connector) -> None:
        """Test that delete_content raises BackendError."""
        with pytest.raises(BackendError, match="read-only"):
            gmail_connector.delete_content("message_id_123")

    def test_mkdir_is_noop(self, gmail_connector) -> None:
        """Test that mkdir is a no-op."""
        # Should not raise any error
        gmail_connector.mkdir("/INBOX")

    def test_rmdir_is_noop(self, gmail_connector) -> None:
        """Test that rmdir is a no-op."""
        # Should not raise any error
        gmail_connector.rmdir("/INBOX")


class TestGmailConnectorDirectoryOperations:
    """Test directory-related operations."""

    def test_is_directory_known_labels(self, gmail_connector) -> None:
        """Test is_directory for known Gmail labels."""
        assert gmail_connector.is_directory("INBOX") is True
        assert gmail_connector.is_directory("/INBOX") is True
        assert gmail_connector.is_directory("inbox") is True
        assert gmail_connector.is_directory("SENT") is True
        assert gmail_connector.is_directory("DRAFTS") is True
        assert gmail_connector.is_directory("TRASH") is True
        assert gmail_connector.is_directory("SPAM") is True
        assert gmail_connector.is_directory("STARRED") is True

    def test_is_directory_unknown_path(self, gmail_connector) -> None:
        """Test is_directory for unknown paths."""
        assert gmail_connector.is_directory("UNKNOWN") is False
        assert gmail_connector.is_directory("/random/path") is False

    def test_get_ref_count(self, gmail_connector) -> None:
        """Test get_ref_count always returns 1."""
        assert gmail_connector.get_ref_count("any_message_id") == 1


class TestGmailConnectorGetGmailService:
    """Test Gmail service creation."""

    def test_get_gmail_service_with_user_email(self, gmail_connector) -> None:
        """Test getting Gmail service with configured user_email."""
        with patch("googleapiclient.discovery.build") as mock_build:
            mock_service = Mock()
            mock_build.return_value = mock_service

            service = gmail_connector._get_gmail_service()

            assert service == mock_service
            mock_build.assert_called_once()

    def test_get_gmail_service_with_context(self, mock_token_manager, mock_oauth_factory) -> None:
        """Test getting Gmail service with context.user_id."""
        backend = GmailConnectorBackend(
            token_manager_db="sqlite:///test.db",
            user_email=None,  # No configured email
        )

        context = OperationContext(
            user="context_user@example.com",
            groups=[],
        )

        with patch("googleapiclient.discovery.build") as mock_build:
            mock_service = Mock()
            mock_build.return_value = mock_service

            service = backend._get_gmail_service(context)

            assert service == mock_service

    def test_get_gmail_service_without_user(self, mock_token_manager, mock_oauth_factory) -> None:
        """Test getting Gmail service without user raises error."""
        backend = GmailConnectorBackend(
            token_manager_db="sqlite:///test.db",
            user_email=None,
        )

        with pytest.raises(BackendError, match="requires either configured user_email"):
            backend._get_gmail_service(context=None)

    def test_get_gmail_service_missing_library(self, gmail_connector) -> None:
        """Test getting Gmail service when google-api-python-client is missing."""
        with (
            patch.dict("sys.modules", {"googleapiclient.discovery": None}),
            pytest.raises(BackendError, match="google-api-python-client not installed"),
        ):
            gmail_connector._get_gmail_service()


class TestGmailConnectorBatchOperations:
    """Test Gmail connector batch read operations."""

    def test_batch_read_content_empty_list(self, gmail_connector) -> None:
        """Test batch_read_content with empty list returns empty dict."""
        result = gmail_connector.batch_read_content([])
        assert result == {}

    def test_batch_read_content_cache_hits(self, gmail_connector) -> None:
        """Test batch_read_content uses cache when available."""
        # Mock caching
        gmail_connector.session_factory = Mock()

        mock_cached = Mock()
        mock_cached.stale = False
        mock_cached.content_binary = b"cached content"

        with patch.object(gmail_connector, "_read_from_cache", return_value=mock_cached):
            result = gmail_connector.batch_read_content(["msg1", "msg2"])

            # Both should be served from cache
            assert result["msg1"] == b"cached content"
            assert result["msg2"] == b"cached content"

    def test_batch_read_content_successful_batch(self, gmail_connector) -> None:
        """Test batch_read_content with successful batch request."""
        message_ids = ["msg1", "msg2", "msg3"]

        # Mock Gmail service and batch request
        mock_service = Mock()
        mock_batch = Mock()

        with (
            patch.object(gmail_connector, "_get_gmail_service", return_value=mock_service),
            patch("googleapiclient.http.BatchHttpRequest", return_value=mock_batch),
        ):
            mock_service.new_batch_http_request.return_value = mock_batch
            mock_batch.execute = Mock()

            # Mock _parse_message_response to return test data
            test_headers = {
                "from": "test@example.com",
                "to": "user@example.com",
                "subject": "Test",
                "date": "2024-01-01",
            }
            with patch.object(
                gmail_connector,
                "_parse_message_response",
                return_value=(test_headers, "Text body", "", [], b"raw"),
            ):
                result = gmail_connector.batch_read_content(message_ids)

                # Should have called new_batch_http_request
                mock_service.new_batch_http_request.assert_called_once()
                # Should have called execute
                mock_batch.execute.assert_called_once()
                # Result should be a dict
                assert isinstance(result, dict)

    def test_batch_read_content_fallback_on_batch_error(self, gmail_connector) -> None:
        """Test batch_read_content falls back to individual reads on batch error."""
        message_ids = ["msg1", "msg2"]

        mock_service = Mock()
        mock_batch = Mock()
        mock_batch.execute.side_effect = Exception("Batch failed")

        with (
            patch.object(gmail_connector, "_get_gmail_service", return_value=mock_service),
            patch("googleapiclient.http.BatchHttpRequest", return_value=mock_batch),
            patch.object(gmail_connector, "read_content", return_value=b"individual read"),
        ):
            mock_service.new_batch_http_request.return_value = mock_batch

            result = gmail_connector.batch_read_content(message_ids)

            # Should have fallen back to individual reads
            assert result["msg1"] == b"individual read"
            assert result["msg2"] == b"individual read"

    def test_batch_read_content_handles_large_batch(self, gmail_connector) -> None:
        """Test batch_read_content handles more than 100 messages (batch size limit)."""
        # Create 150 message IDs (should be split into 2 batches)
        message_ids = [f"msg{i}" for i in range(150)]

        mock_service = Mock()
        mock_batch = Mock()

        with (
            patch.object(gmail_connector, "_get_gmail_service", return_value=mock_service),
            patch("googleapiclient.http.BatchHttpRequest", return_value=mock_batch),
        ):
            mock_service.new_batch_http_request.return_value = mock_batch
            mock_batch.execute = Mock()

            test_headers = {
                "from": "test@example.com",
                "to": "user@example.com",
                "subject": "Test",
                "date": "2024-01-01",
            }
            with patch.object(
                gmail_connector,
                "_parse_message_response",
                return_value=(test_headers, "Text", "", [], b"raw"),
            ):
                gmail_connector.batch_read_content(message_ids)

                # Should have created 2 batch requests (100 + 50)
                assert mock_service.new_batch_http_request.call_count == 2
                assert mock_batch.execute.call_count == 2

    def test_parse_message_response(self, gmail_connector) -> None:
        """Test _parse_message_response helper method."""
        import base64

        # Create a test email message
        raw_email = b"From: sender@example.com\r\nTo: recipient@example.com\r\nSubject: Test\r\n\r\nTest body"
        encoded_raw = base64.urlsafe_b64encode(raw_email).decode()

        message = {
            "labelIds": ["INBOX"],
            "raw": encoded_raw,
        }

        headers, text_body, html_body, labels, raw_bytes = gmail_connector._parse_message_response(
            message
        )

        assert headers["from"] == "sender@example.com"
        assert headers["to"] == "recipient@example.com"
        assert headers["subject"] == "Test"
        assert "Test body" in text_body
        assert html_body == ""  # No HTML in this test message
        assert labels == ["INBOX"]
        assert raw_bytes == raw_email

    def test_parse_message_response_without_raw(self, gmail_connector) -> None:
        """Test _parse_message_response handles messages without raw content."""
        message = {
            "labelIds": ["SENT"],
        }

        headers, text_body, html_body, labels, raw_bytes = gmail_connector._parse_message_response(
            message
        )

        # Should return defaults
        assert headers["from"] == "Unknown"
        assert headers["to"] == "Unknown"
        assert headers["subject"] == "No Subject"
        assert text_body == ""
        assert html_body == ""
        assert labels == ["SENT"]
        assert raw_bytes == b""


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
