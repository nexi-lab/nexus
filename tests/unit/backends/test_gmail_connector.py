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


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
