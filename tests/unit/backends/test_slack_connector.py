"""Unit tests for Slack connector backend."""

from unittest.mock import AsyncMock, Mock, patch

import pytest

from nexus.backends.slack_connector import SlackConnectorBackend
from nexus.core.exceptions import BackendError
from nexus.core.permissions import OperationContext


@pytest.fixture
def mock_token_manager():
    """Create a mock TokenManager."""
    with patch("nexus.server.auth.token_manager.TokenManager") as mock_tm:
        mock_instance = Mock()
        mock_instance.get_valid_token = AsyncMock(return_value="xoxp-test-token")
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
def slack_connector(mock_token_manager, mock_oauth_factory) -> SlackConnectorBackend:
    """Create a Slack connector instance."""
    return SlackConnectorBackend(
        token_manager_db="sqlite:///test.db",
        user_email="test@example.com",
        provider="slack",
        max_messages_per_channel=100,
    )


class TestSlackConnectorInitialization:
    """Test Slack connector initialization."""

    def test_init_with_db_path(self, mock_token_manager, mock_oauth_factory) -> None:
        """Test initialization with database path."""
        backend = SlackConnectorBackend(
            token_manager_db="/path/to/nexus.db",
            user_email="test@example.com",
        )

        assert backend.name == "slack"
        assert backend.user_email == "test@example.com"
        assert backend.provider == "slack"
        assert backend.max_messages_per_channel == 100
        assert backend.user_scoped is True

    def test_init_with_db_url(self, mock_token_manager, mock_oauth_factory) -> None:
        """Test initialization with database URL."""
        backend = SlackConnectorBackend(
            token_manager_db="postgresql://user:pass@localhost/nexus",
            user_email="test@example.com",
        )

        assert backend.name == "slack"
        assert backend.user_email == "test@example.com"

    def test_init_custom_values(self, mock_token_manager, mock_oauth_factory) -> None:
        """Test initialization with custom values."""
        backend = SlackConnectorBackend(
            token_manager_db="sqlite:///test.db",
            user_email="custom@example.com",
            provider="slack-custom",
            max_messages_per_channel=50,
        )

        assert backend.user_email == "custom@example.com"
        assert backend.provider == "slack-custom"
        assert backend.max_messages_per_channel == 50

    def test_init_without_user_email(self, mock_token_manager, mock_oauth_factory) -> None:
        """Test initialization without user_email (uses context)."""
        backend = SlackConnectorBackend(
            token_manager_db="sqlite:///test.db",
            user_email=None,
        )

        assert backend.user_email is None
        assert backend.name == "slack"


class TestSlackConnectorProperties:
    """Test Slack connector properties."""

    def test_name_property(self, slack_connector) -> None:
        """Test name property."""
        assert slack_connector.name == "slack"

    def test_user_scoped_property(self, slack_connector) -> None:
        """Test user_scoped property."""
        assert slack_connector.user_scoped is True

    def test_has_caching_with_session_factory(self, mock_token_manager, mock_oauth_factory) -> None:
        """Test _has_caching with session_factory."""
        mock_session_factory = Mock()
        backend = SlackConnectorBackend(
            token_manager_db="sqlite:///test.db",
            user_email="test@example.com",
            session_factory=mock_session_factory,
        )

        assert backend._has_caching() is True

    def test_has_caching_without_session(self, slack_connector) -> None:
        """Test _has_caching without session."""
        assert slack_connector._has_caching() is False


class TestSlackConnectorDirectoryStructure:
    """Test directory structure operations."""

    def test_is_directory_root(self, slack_connector) -> None:
        """Test is_directory for root path."""
        assert slack_connector.is_directory("") is True
        assert slack_connector.is_directory("/") is True

    def test_is_directory_folder_types(self, slack_connector) -> None:
        """Test is_directory for folder types."""
        assert slack_connector.is_directory("channels") is True
        assert slack_connector.is_directory("private-channels") is True
        assert slack_connector.is_directory("dms") is True
        assert slack_connector.is_directory("invalid-folder") is False

    def test_is_directory_channel_folders(self, slack_connector) -> None:
        """Test is_directory for channel folders."""
        # In YAML format, channels are files not directories
        assert slack_connector.is_directory("channels/general.yaml") is False
        assert slack_connector.is_directory("channels/random.yaml") is False
        # Top-level folders are directories
        assert slack_connector.is_directory("channels") is True
        assert slack_connector.is_directory("dms") is True

    def test_is_directory_message_files(self, slack_connector) -> None:
        """Test is_directory for message files."""
        assert slack_connector.is_directory("channels/general/1234567890.123456-msg.json") is False


class TestSlackConnectorMessageFormatting:
    """Test message formatting."""

    def test_format_message_as_json(self, slack_connector) -> None:
        """Test formatting message as JSON."""
        message = {
            "type": "message",
            "user": "U1234567890",
            "text": "Hello world!",
            "ts": "1234567890.123456",
            "channel_id": "C1234567890",
            "channel_name": "general",
        }

        json_bytes = slack_connector._format_message_as_json(message)
        json_str = json_bytes.decode("utf-8")

        assert "Hello world!" in json_str
        assert "U1234567890" in json_str
        assert "general" in json_str
        assert "1234567890.123456" in json_str

    def test_parse_message_timestamp(self, slack_connector) -> None:
        """Test parsing Slack timestamps."""
        dt = slack_connector._parse_message_timestamp("1234567890.123456")

        assert dt is not None
        # Slack timestamps are Unix timestamps
        assert dt.timestamp() == pytest.approx(1234567890.123456, rel=1e-3)


class TestSlackConnectorWriteOperations:
    """Test write operations."""

    @patch("nexus.backends.slack_connector.SlackConnectorBackend._get_slack_client")
    def test_write_content_basic_message(self, mock_get_client, slack_connector) -> None:
        """Test writing a basic message."""
        # Mock Slack client
        mock_client = Mock()
        mock_client.chat_postMessage.return_value = {
            "ok": True,
            "ts": "1234567890.123456",
            "channel": "C1234567890",
        }
        mock_get_client.return_value = mock_client

        # Create message data
        message_data = {
            "channel": "C1234567890",
            "text": "Test message",
        }

        import json

        content = json.dumps(message_data).encode("utf-8")
        context = OperationContext(
            user="test@example.com",
            groups=[],
            backend_path="channels/general/new-message.json",
        )
        context.tenant_id = "default"

        # Write message
        result = slack_connector.write_content(content, context)

        # Verify
        assert result == "1234567890.123456"
        mock_client.chat_postMessage.assert_called_once_with(
            channel="C1234567890",
            text="Test message",
        )

    @patch("nexus.backends.slack_connector.SlackConnectorBackend._get_slack_client")
    def test_write_content_threaded_message(self, mock_get_client, slack_connector) -> None:
        """Test writing a threaded message."""
        # Mock Slack client
        mock_client = Mock()
        mock_client.chat_postMessage.return_value = {
            "ok": True,
            "ts": "1234567891.123456",
            "channel": "C1234567890",
        }
        mock_get_client.return_value = mock_client

        # Create threaded message data
        message_data = {
            "channel": "C1234567890",
            "text": "Reply in thread",
            "thread_ts": "1234567890.123456",
        }

        import json

        content = json.dumps(message_data).encode("utf-8")
        context = OperationContext(
            user="test@example.com",
            groups=[],
            backend_path="channels/general/new-message.json",
        )
        context.tenant_id = "default"

        # Write message
        result = slack_connector.write_content(content, context)

        # Verify
        assert result == "1234567891.123456"
        mock_client.chat_postMessage.assert_called_once_with(
            channel="C1234567890",
            text="Reply in thread",
            thread_ts="1234567890.123456",
        )


class TestSlackConnectorReadOperations:
    """Test read operations."""

    def test_read_content_without_context(self, slack_connector) -> None:
        """Test reading content without context returns error."""
        result = slack_connector.read_content("fake-hash", context=None)

        # Should return error HandlerResponse
        assert not result.success
        assert result.error_message and "requires backend_path" in result.error_message


class TestSlackConnectorGetVersion:
    """Test version management."""

    def test_get_version_for_message_file(self, slack_connector) -> None:
        """Test get_version for message file."""
        context = OperationContext(
            user="test@example.com",
            groups=[],
            backend_path="channels/general/1234567890.123456-msg.json",
        )
        context.tenant_id = "default"

        version = slack_connector.get_version(
            "channels/general/1234567890.123456-msg.json", context
        )

        assert version == "immutable"

    def test_get_version_for_directory(self, slack_connector) -> None:
        """Test get_version for directory."""
        context = OperationContext(
            user="test@example.com",
            groups=[],
            backend_path="channels/general",
        )
        context.tenant_id = "default"

        version = slack_connector.get_version("channels/general", context)

        assert version is None


class TestSlackConnectorUnsupportedOperations:
    """Test unsupported operations."""

    def test_mkdir_raises_error(self, slack_connector) -> None:
        """Test mkdir raises error."""
        with pytest.raises(BackendError):
            slack_connector.mkdir("channels/new-channel")

    def test_rmdir_raises_error(self, slack_connector) -> None:
        """Test rmdir raises error."""
        with pytest.raises(BackendError):
            slack_connector.rmdir("channels/general")

    def test_delete_content_raises_error(self, slack_connector) -> None:
        """Test delete_content raises error."""
        with pytest.raises(BackendError):
            slack_connector.delete_content("fake-hash")
