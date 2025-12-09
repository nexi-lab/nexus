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
        max_message_per_label=10,
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
        assert backend.max_message_per_label == 200
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
            max_message_per_label=50,
        )

        assert backend.user_email == "custom@example.com"
        assert backend.provider == "gmail-custom"
        assert backend.max_message_per_label == 50

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

    @pytest.mark.skip(
        reason="Method _create_yaml_content no longer exists, use _format_email_as_yaml instead"
    )
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

    @pytest.mark.skip(
        reason="Method _create_yaml_content no longer exists, use _format_email_as_yaml instead"
    )
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

    @pytest.mark.skip(
        reason="Method _create_yaml_content no longer exists, use _format_email_as_yaml instead"
    )
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

    def test_mkdir_raises_error(self, gmail_connector) -> None:
        """Test that mkdir raises BackendError."""
        with pytest.raises(BackendError, match="read-only"):
            gmail_connector.mkdir("/INBOX")

    def test_rmdir_raises_error(self, gmail_connector) -> None:
        """Test that rmdir raises BackendError."""
        with pytest.raises(BackendError, match="read-only"):
            gmail_connector.rmdir("/INBOX")


class TestGmailConnectorDirectoryOperations:
    """Test directory-related operations."""

    def test_is_directory_known_labels(self, gmail_connector) -> None:
        """Test is_directory for known Gmail labels."""
        # Test labels that are in LABEL_FOLDERS: SENT, STARRED, IMPORTANT, INBOX
        assert gmail_connector.is_directory("INBOX") is True
        assert gmail_connector.is_directory("/INBOX") is True
        assert gmail_connector.is_directory("SENT") is True
        assert gmail_connector.is_directory("STARRED") is True
        assert gmail_connector.is_directory("IMPORTANT") is True
        # Note: is_directory is case-sensitive, lowercase "inbox" returns False
        # Labels not in LABEL_FOLDERS (DRAFTS, TRASH, SPAM) return False
        assert gmail_connector.is_directory("DRAFTS") is False
        assert gmail_connector.is_directory("TRASH") is False
        assert gmail_connector.is_directory("SPAM") is False

    def test_is_directory_unknown_path(self, gmail_connector) -> None:
        """Test is_directory for unknown paths."""
        assert gmail_connector.is_directory("UNKNOWN") is False
        assert gmail_connector.is_directory("/random/path") is False

    def test_is_directory_email_file_is_not_directory(self, gmail_connector) -> None:
        """Test is_directory returns False for email files (flattened format)."""
        # Email files in flattened format: {thread_id}-{msg_id}.yaml
        assert gmail_connector.is_directory("SENT/19a93df17407089a-19a93df17407089a") is False
        assert gmail_connector.is_directory("INBOX/thread123-msg456") is False

    def test_is_directory_thread_folder_no_longer_exists(self, gmail_connector) -> None:
        """Test is_directory returns False for old thread folder paths (flattened hierarchy)."""
        # In flattened hierarchy, thread folders no longer exist
        assert gmail_connector.is_directory("SENT/19a93df17407089a") is False
        assert gmail_connector.is_directory("INBOX/thread123") is False

    def test_get_ref_count(self, gmail_connector) -> None:
        """Test get_ref_count always returns 1."""
        assert gmail_connector.get_ref_count("any_message_id") == 1


class TestGmailConnectorFlattenedHierarchy:
    """Test flattened 2-level hierarchy (Label/thread_id-msg_id.yaml)."""

    def test_list_dir_root_returns_label_folders(self, gmail_connector) -> None:
        """Test list_dir('') returns hardcoded label folders."""
        result = gmail_connector.list_dir("")

        # Should return SENT/, STARRED/, IMPORTANT/, INBOX/
        assert len(result) == 4
        assert "SENT/" in result
        assert "STARRED/" in result
        assert "IMPORTANT/" in result
        assert "INBOX/" in result

    def test_list_dir_label_returns_email_files(self, gmail_connector) -> None:
        """Test list_dir('SENT') returns email files in flattened format."""
        # Mock Gmail service and email listing
        mock_service = Mock()
        mock_emails = [
            {
                "id": "msg1",
                "threadId": "thread1",
                "folder": "SENT",
            },
            {
                "id": "msg2",
                "threadId": "thread1",
                "folder": "SENT",
            },
            {
                "id": "msg3",
                "threadId": "thread2",
                "folder": "SENT",
            },
        ]

        with (
            patch.object(gmail_connector, "_get_gmail_service", return_value=mock_service),
            patch(
                "nexus.backends.gmail_connector.list_emails_by_folder",
                return_value=mock_emails,
            ),
        ):
            result = gmail_connector.list_dir("SENT")

            # Should return files in format: thread_id-msg_id.yaml
            assert len(result) == 3
            assert "thread1-msg1.yaml" in result
            assert "thread1-msg2.yaml" in result
            assert "thread2-msg3.yaml" in result

    def test_list_dir_label_only_returns_matching_folder(self, gmail_connector) -> None:
        """Test list_dir filters emails by folder."""
        mock_service = Mock()
        mock_emails = [
            {"id": "msg1", "threadId": "thread1", "folder": "SENT"},
            {"id": "msg2", "threadId": "thread2", "folder": "INBOX"},  # Different folder
            {"id": "msg3", "threadId": "thread3", "folder": "SENT"},
        ]

        with (
            patch.object(gmail_connector, "_get_gmail_service", return_value=mock_service),
            patch(
                "nexus.backends.gmail_connector.list_emails_by_folder",
                return_value=mock_emails,
            ),
        ):
            result = gmail_connector.list_dir("SENT")

            # Should only return SENT emails
            assert len(result) == 2
            assert "thread1-msg1.yaml" in result
            assert "thread3-msg3.yaml" in result
            assert "thread2-msg2.yaml" not in result

    def test_list_dir_thread_folder_raises_error(self, gmail_connector) -> None:
        """Test list_dir raises error for thread folders (no longer exist in flattened hierarchy)."""
        # Thread folders don't exist in flattened hierarchy
        with pytest.raises(FileNotFoundError, match="Directory not found"):
            gmail_connector.list_dir("SENT/thread123")

    def test_list_dir_invalid_path_raises_error(self, gmail_connector) -> None:
        """Test list_dir raises error for invalid paths."""
        with pytest.raises(FileNotFoundError, match="Directory not found"):
            gmail_connector.list_dir("INVALID/path/here")

    def test_read_content_flattened_path_format(self, gmail_connector) -> None:
        """Test read_content with flattened path format (LABEL/thread_id-msg_id.yaml)."""
        # Create mock context with flattened path
        mock_context = Mock()
        mock_context.backend_path = "SENT/thread123-msg456.yaml"

        mock_service = Mock()

        # Mock the complete Gmail API response with all required fields
        mock_message = {
            "id": "msg456",
            "threadId": "thread123",
            "labelIds": ["SENT"],
            "payload": {
                "headers": [
                    {"name": "From", "value": "test@example.com"},
                    {"name": "To", "value": "user@example.com"},
                    {"name": "Subject", "value": "Test"},
                    {"name": "Date", "value": "2024-01-01"},
                ],
                "body": {"data": "VGVzdCBib2R5"},  # "Test body" in base64
            },
        }

        with (
            patch.object(gmail_connector, "_get_gmail_service", return_value=mock_service),
            patch.object(gmail_connector, "_read_from_cache", return_value=None),
            patch.object(gmail_connector, "_write_to_cache"),
        ):
            mock_service.users().messages().get().execute.return_value = mock_message

            result = gmail_connector.read_content("dummy_hash", context=mock_context)

            # Should successfully parse and return YAML content
            assert isinstance(result, bytes)
            assert b"from: test@example.com" in result
            assert b"subject: Test" in result

    def test_read_content_invalid_path_format_raises_error(self, gmail_connector) -> None:
        """Test read_content raises error for invalid path format."""
        from nexus.core.exceptions import NexusFileNotFoundError

        # Invalid path: missing thread_id in filename
        mock_context = Mock()
        mock_context.backend_path = "SENT/msg456.yaml"  # Missing thread_id prefix

        with pytest.raises(NexusFileNotFoundError):
            gmail_connector.read_content("dummy_hash", context=mock_context)

    def test_read_content_old_thread_folder_format_raises_error(self, gmail_connector) -> None:
        """Test read_content raises error for old 3-level path format."""
        from nexus.core.exceptions import NexusFileNotFoundError

        # Old format: SENT/thread_id/email-msg_id.yaml (no longer supported)
        mock_context = Mock()
        mock_context.backend_path = "SENT/thread123/email-msg456.yaml"

        with pytest.raises(NexusFileNotFoundError):
            gmail_connector.read_content("dummy_hash", context=mock_context)

    def test_content_exists_flattened_format(self, gmail_connector) -> None:
        """Test content_exists with flattened path format."""
        mock_context = Mock()
        mock_context.backend_path = "SENT/thread123-msg456.yaml"

        mock_service = Mock()

        with patch.object(gmail_connector, "_get_gmail_service", return_value=mock_service):
            mock_service.users().messages().get().execute.return_value = {"id": "msg456"}

            result = gmail_connector.content_exists("dummy_hash", context=mock_context)

            # Should successfully validate path format and check Gmail API
            assert result is True

    def test_content_exists_invalid_format_returns_false(self, gmail_connector) -> None:
        """Test content_exists returns False for invalid path format."""
        # Invalid format: missing thread_id
        mock_context = Mock()
        mock_context.backend_path = "SENT/msg456.yaml"

        result = gmail_connector.content_exists("dummy_hash", context=mock_context)
        assert result is False

    def test_content_exists_old_format_returns_false(self, gmail_connector) -> None:
        """Test content_exists returns False for old 3-level format."""
        # Old format not supported
        mock_context = Mock()
        mock_context.backend_path = "SENT/thread123/email-msg456.yaml"

        result = gmail_connector.content_exists("dummy_hash", context=mock_context)
        assert result is False

    def test_bulk_download_flattened_paths(self, gmail_connector) -> None:
        """Test _bulk_download_contents with flattened path format."""
        paths = [
            "SENT/thread1-msg1.yaml",
            "SENT/thread1-msg2.yaml",
            "INBOX/thread2-msg3.yaml",
        ]

        mock_service = Mock()
        mock_batch = Mock()

        with patch.object(gmail_connector, "_get_gmail_service", return_value=mock_service):
            # Mock the batch execution to return our test responses
            def mock_callback_exec():
                # Simulate batch callback execution
                pass

            mock_batch.execute = mock_callback_exec
            mock_service.new_batch_http_request.return_value = mock_batch

            result = gmail_connector._bulk_download_contents(paths)

            # Should successfully parse flattened paths
            assert isinstance(result, dict)
            # Batch should be created
            mock_service.new_batch_http_request.assert_called_once()

    def test_bulk_download_skips_invalid_paths(self, gmail_connector) -> None:
        """Test _bulk_download_contents skips invalid path formats."""
        paths = [
            "SENT/thread1-msg1.yaml",  # Valid
            "SENT/msg2.yaml",  # Invalid: missing thread_id
            "SENT/thread3/email-msg3.yaml",  # Invalid: old 3-level format
            "INBOX/thread4-msg4.yaml",  # Valid
        ]

        mock_service = Mock()
        mock_batch = Mock()

        with patch.object(gmail_connector, "_get_gmail_service", return_value=mock_service):
            # Mock the batch execution
            def mock_callback_exec():
                # Simulate batch callback execution
                pass

            mock_batch.execute = mock_callback_exec
            mock_service.new_batch_http_request.return_value = mock_batch

            result = gmail_connector._bulk_download_contents(paths)

            # Should process valid paths only (2 out of 4)
            # Invalid paths are skipped silently
            assert isinstance(result, dict)


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

    @pytest.mark.skip(reason="Method batch_read_content no longer exists")
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

    @pytest.mark.skip(
        reason="Method _parse_message_response no longer exists, use _parse_gmail_message instead"
    )
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

    @pytest.mark.skip(
        reason="Method _parse_message_response no longer exists, use _parse_gmail_message instead"
    )
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

    @pytest.mark.skip(
        reason="Method _parse_message_response no longer exists, use _parse_gmail_message instead"
    )
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

    @pytest.mark.skip(
        reason="Method _parse_message_response no longer exists, use _parse_gmail_message instead"
    )
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


class TestGmailConnectorExponentialBackoff:
    """Test exponential backoff for Gmail API rate limiting."""

    def test_batch_fetch_succeeds_on_first_try(self) -> None:
        """Test batch fetch succeeds without retries when no errors occur."""
        from nexus.backends.gmail_connector_utils import fetch_emails_batch

        mock_service = Mock()
        mock_batch = Mock()
        message_ids = ["msg1", "msg2", "msg3"]
        email_cache = {}

        def mock_parse(msg: dict) -> dict:
            return {"id": msg["id"], "content": "test"}

        # Mock successful batch execution
        mock_service.new_batch_http_request.return_value = mock_batch
        mock_batch.execute = Mock()  # Success on first try

        with patch("time.sleep") as mock_sleep:
            fetch_emails_batch(mock_service, message_ids, mock_parse, email_cache)

            # Should succeed without any retries
            mock_batch.execute.assert_called_once()
            mock_sleep.assert_not_called()

    def test_batch_fetch_retries_on_429_error(self) -> None:
        """Test batch fetch retries with exponential backoff on 429 errors."""
        from nexus.backends.gmail_connector_utils import fetch_emails_batch

        mock_service = Mock()
        mock_batch = Mock()
        message_ids = ["msg1", "msg2"]
        email_cache = {}

        def mock_parse(msg: dict) -> dict:
            return {"id": msg["id"], "content": "test"}

        # Simulate 429 error on first two attempts, then success
        error_429 = Exception("429 rateLimitExceeded: Too many concurrent requests for user")
        mock_batch.execute = Mock(side_effect=[error_429, error_429, None])
        mock_service.new_batch_http_request.return_value = mock_batch

        with patch("time.sleep") as mock_sleep:
            fetch_emails_batch(mock_service, message_ids, mock_parse, email_cache)

            # Should retry 2 times (fail, fail, succeed)
            assert mock_batch.execute.call_count == 3

            # Should sleep with exponential backoff: 1s, 2s
            assert mock_sleep.call_count == 2
            mock_sleep.assert_any_call(1.0)  # First retry delay
            mock_sleep.assert_any_call(2.0)  # Second retry delay

    def test_batch_fetch_gives_up_after_max_retries(self) -> None:
        """Test batch fetch stops retrying after max attempts."""
        from nexus.backends.gmail_connector_utils import fetch_emails_batch

        mock_service = Mock()
        mock_batch = Mock()
        message_ids = ["msg1"]
        email_cache = {}

        def mock_parse(msg: dict) -> dict:
            return {"id": msg["id"], "content": "test"}

        # Always return 429 error
        error_429 = Exception("429 rateLimitExceeded")
        mock_batch.execute = Mock(side_effect=error_429)
        mock_service.new_batch_http_request.return_value = mock_batch

        with patch("time.sleep") as mock_sleep:
            fetch_emails_batch(mock_service, message_ids, mock_parse, email_cache)

            # Should try 5 times (max_retries)
            assert mock_batch.execute.call_count == 5

            # Should sleep 4 times (not on last retry): 1s, 2s, 4s, 8s
            assert mock_sleep.call_count == 4
            mock_sleep.assert_any_call(1.0)
            mock_sleep.assert_any_call(2.0)
            mock_sleep.assert_any_call(4.0)
            mock_sleep.assert_any_call(8.0)

    def test_batch_fetch_does_not_retry_non_rate_limit_errors(self) -> None:
        """Test batch fetch doesn't retry for non-429 errors."""
        from nexus.backends.gmail_connector_utils import fetch_emails_batch

        mock_service = Mock()
        mock_batch = Mock()
        message_ids = ["msg1"]
        email_cache = {}

        def mock_parse(msg: dict) -> dict:
            return {"id": msg["id"], "content": "test"}

        # Non-rate-limit error (e.g., 500 server error)
        error_500 = Exception("500 Internal Server Error")
        mock_batch.execute = Mock(side_effect=error_500)
        mock_service.new_batch_http_request.return_value = mock_batch

        with patch("time.sleep") as mock_sleep:
            fetch_emails_batch(mock_service, message_ids, mock_parse, email_cache)

            # Should only try once (no retries for non-429 errors)
            mock_batch.execute.assert_called_once()
            mock_sleep.assert_not_called()

    def test_list_emails_retries_on_429_error(self) -> None:
        """Test list_emails_by_folder retries with exponential backoff on 429 errors."""
        from nexus.backends.gmail_connector_utils import list_emails_by_folder

        mock_service = Mock()

        # Simulate 429 error on first attempt, then success
        error_429 = Exception("429 rateLimitExceeded")
        success_response = {"messages": [{"id": "msg1", "threadId": "thread1"}]}

        mock_list = Mock()
        mock_list.execute = Mock(side_effect=[error_429, success_response])
        mock_service.users().messages().list = Mock(return_value=mock_list)

        with patch("time.sleep") as mock_sleep:
            result = list_emails_by_folder(mock_service, folder_filter=["SENT"], silent=True)

            # Should retry once and succeed
            assert mock_list.execute.call_count == 2
            mock_sleep.assert_called_once_with(1.0)  # First retry delay

            # Should return emails
            assert len(result) > 0

    def test_list_emails_exponential_delays_are_correct(self) -> None:
        """Test that exponential backoff delays follow 2^n pattern."""
        from nexus.backends.gmail_connector_utils import list_emails_by_folder

        mock_service = Mock()

        # Simulate multiple 429 errors
        error_429 = Exception("429 rateLimitExceeded")
        success_response = {"messages": []}

        mock_list = Mock()
        # Fail 4 times, then succeed on 5th try
        mock_list.execute = Mock(
            side_effect=[error_429, error_429, error_429, error_429, success_response]
        )
        mock_service.users().messages().list = Mock(return_value=mock_list)

        with patch("time.sleep") as mock_sleep:
            list_emails_by_folder(mock_service, folder_filter=["SENT"], silent=True)

            # Should have delays: 1, 2, 4, 8 seconds
            assert mock_sleep.call_count == 4
            calls = [call[0][0] for call in mock_sleep.call_args_list]
            assert calls == [1.0, 2.0, 4.0, 8.0]

    def test_list_emails_raises_after_max_retries(self) -> None:
        """Test list_emails raises exception after max retries."""
        from nexus.backends.gmail_connector_utils import list_emails_by_folder

        mock_service = Mock()

        # Always return 429 error
        error_429 = Exception("429 rateLimitExceeded")
        mock_list = Mock()
        mock_list.execute = Mock(side_effect=error_429)
        mock_service.users().messages().list = Mock(return_value=mock_list)

        with (
            patch("time.sleep"),
            pytest.raises(Exception, match="429"),
        ):
            list_emails_by_folder(mock_service, folder_filter=["SENT"], silent=True)

            # Should try 5 times before giving up
            assert mock_list.execute.call_count == 5


class TestGmailConnectorRecursiveBodyParsing:
    """Test recursive body extraction from nested multipart messages."""

    def test_extract_body_from_simple_parts(self, gmail_connector) -> None:
        """Test extracting body from simple non-nested parts."""
        import base64

        text_content = "This is plain text body"
        html_content = "<html><body>This is HTML body</body></html>"

        parts = [
            {
                "mimeType": "text/plain",
                "body": {"data": base64.urlsafe_b64encode(text_content.encode()).decode()},
            },
            {
                "mimeType": "text/html",
                "body": {"data": base64.urlsafe_b64encode(html_content.encode()).decode()},
            },
        ]

        body_text, body_html = gmail_connector._extract_body_from_parts(parts)

        assert body_text == text_content
        assert body_html == html_content

    def test_extract_body_from_nested_multipart(self, gmail_connector) -> None:
        """Test extracting body from nested multipart/alternative inside multipart/mixed."""
        import base64

        text_content = "Nested plain text"
        html_content = "<html><body>Nested HTML</body></html>"

        # Simulate: multipart/mixed containing multipart/alternative
        parts = [
            {
                "mimeType": "multipart/alternative",
                "parts": [
                    {
                        "mimeType": "text/plain",
                        "body": {"data": base64.urlsafe_b64encode(text_content.encode()).decode()},
                    },
                    {
                        "mimeType": "text/html",
                        "body": {"data": base64.urlsafe_b64encode(html_content.encode()).decode()},
                    },
                ],
            },
            {
                "mimeType": "application/pdf",
                "body": {"attachmentId": "attachment123"},
            },
        ]

        body_text, body_html = gmail_connector._extract_body_from_parts(parts)

        assert body_text == text_content
        assert body_html == html_content

    def test_extract_body_from_deeply_nested_multipart(self, gmail_connector) -> None:
        """Test extracting body from deeply nested multipart structures."""
        import base64

        text_content = "Deeply nested text"
        html_content = "<html>Deeply nested HTML</html>"

        # Simulate: multipart/mixed > multipart/related > multipart/alternative
        parts = [
            {
                "mimeType": "multipart/related",
                "parts": [
                    {
                        "mimeType": "multipart/alternative",
                        "parts": [
                            {
                                "mimeType": "text/plain",
                                "body": {
                                    "data": base64.urlsafe_b64encode(text_content.encode()).decode()
                                },
                            },
                            {
                                "mimeType": "text/html",
                                "body": {
                                    "data": base64.urlsafe_b64encode(html_content.encode()).decode()
                                },
                            },
                        ],
                    },
                    {
                        "mimeType": "image/png",
                        "body": {"attachmentId": "img123"},
                    },
                ],
            }
        ]

        body_text, body_html = gmail_connector._extract_body_from_parts(parts)

        assert body_text == text_content
        assert body_html == html_content

    def test_extract_body_prefers_first_occurrence(self, gmail_connector) -> None:
        """Test that extraction prefers the first occurrence of each type."""
        import base64

        first_text = "First text"
        second_text = "Second text"
        first_html = "<html>First HTML</html>"
        second_html = "<html>Second HTML</html>"

        parts = [
            {
                "mimeType": "text/plain",
                "body": {"data": base64.urlsafe_b64encode(first_text.encode()).decode()},
            },
            {
                "mimeType": "text/plain",
                "body": {"data": base64.urlsafe_b64encode(second_text.encode()).decode()},
            },
            {
                "mimeType": "text/html",
                "body": {"data": base64.urlsafe_b64encode(first_html.encode()).decode()},
            },
            {
                "mimeType": "text/html",
                "body": {"data": base64.urlsafe_b64encode(second_html.encode()).decode()},
            },
        ]

        body_text, body_html = gmail_connector._extract_body_from_parts(parts)

        # Should use first occurrence
        assert body_text == first_text
        assert body_html == first_html

    def test_extract_body_handles_empty_parts(self, gmail_connector) -> None:
        """Test extracting body from empty parts list."""
        parts = []

        body_text, body_html = gmail_connector._extract_body_from_parts(parts)

        assert body_text == ""
        assert body_html == ""

    def test_extract_body_handles_parts_without_data(self, gmail_connector) -> None:
        """Test extracting body from parts without data (e.g., attachments only)."""
        parts = [
            {
                "mimeType": "application/pdf",
                "body": {"attachmentId": "attachment123"},
            },
            {
                "mimeType": "image/jpeg",
                "body": {"attachmentId": "img456"},
            },
        ]

        body_text, body_html = gmail_connector._extract_body_from_parts(parts)

        assert body_text == ""
        assert body_html == ""

    def test_extract_body_handles_decode_errors(self, gmail_connector) -> None:
        """Test that decode errors are gracefully handled."""
        parts = [
            {
                "mimeType": "text/plain",
                "body": {"data": "invalid-base64!!!"},
            },
            {
                "mimeType": "text/html",
                "body": {"data": "also-invalid-base64!!!"},
            },
        ]

        # Should not raise, should return empty strings
        body_text, body_html = gmail_connector._extract_body_from_parts(parts)

        assert body_text == ""
        assert body_html == ""

    def test_parse_gmail_message_with_nested_multipart(self, gmail_connector) -> None:
        """Test _parse_gmail_message with nested multipart structure."""
        import base64

        text_content = "Test message body"
        html_content = "<html><body>Test HTML</body></html>"

        message = {
            "id": "msg123",
            "threadId": "thread456",
            "labelIds": ["INBOX"],
            "snippet": "Test snippet",
            "payload": {
                "headers": [
                    {"name": "From", "value": "sender@example.com"},
                    {"name": "To", "value": "recipient@example.com"},
                    {"name": "Subject", "value": "Test Subject"},
                    {"name": "Date", "value": "Mon, 1 Jan 2024 12:00:00 +0000"},
                ],
                "parts": [
                    {
                        "mimeType": "multipart/alternative",
                        "parts": [
                            {
                                "mimeType": "text/plain",
                                "body": {
                                    "data": base64.urlsafe_b64encode(text_content.encode()).decode()
                                },
                            },
                            {
                                "mimeType": "text/html",
                                "body": {
                                    "data": base64.urlsafe_b64encode(html_content.encode()).decode()
                                },
                            },
                        ],
                    }
                ],
            },
        }

        email_data = gmail_connector._parse_gmail_message(message)

        assert email_data["id"] == "msg123"
        assert email_data["threadId"] == "thread456"
        assert email_data["subject"] == "Test Subject"
        assert email_data["from"] == "sender@example.com"
        assert email_data["to"] == "recipient@example.com"
        assert email_data["body_text"] == text_content
        assert email_data["body_html"] == html_content

    def test_parse_gmail_message_with_simple_body(self, gmail_connector) -> None:
        """Test _parse_gmail_message with simple non-multipart body."""
        import base64

        text_content = "Simple message body"

        message = {
            "id": "msg789",
            "threadId": "thread012",
            "labelIds": ["SENT"],
            "snippet": "Simple snippet",
            "payload": {
                "headers": [
                    {"name": "From", "value": "me@example.com"},
                    {"name": "To", "value": "you@example.com"},
                    {"name": "Subject", "value": "Simple Subject"},
                    {"name": "Date", "value": "Tue, 2 Jan 2024 12:00:00 +0000"},
                ],
                "body": {"data": base64.urlsafe_b64encode(text_content.encode()).decode()},
            },
        }

        email_data = gmail_connector._parse_gmail_message(message)

        assert email_data["id"] == "msg789"
        assert email_data["body_text"] == text_content
        assert email_data["body_html"] == ""


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
