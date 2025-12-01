"""Unit tests for Gmail connector backend with OAuth and caching support."""

from unittest.mock import Mock, patch, MagicMock
import warnings

import pytest

from nexus.backends.gmail_connector import GmailConnectorBackend
from nexus.core.exceptions import BackendError, NexusFileNotFoundError
from nexus.core.permissions import OperationContext


@pytest.fixture
def mock_token_manager():
    """Create a mock TokenManager."""
    with patch("nexus.server.auth.token_manager.TokenManager") as mock_tm:
        mock_instance = Mock()
        mock_instance.get_access_token.return_value = "mock_access_token"
        mock_tm.return_value = mock_instance
        yield mock_tm


@pytest.fixture
def mock_gmail_service():
    """Create a mock Gmail API service."""
    service = Mock()
    return service


@pytest.fixture
def mock_session_factory():
    """Create a mock session factory."""
    mock_session = Mock()
    mock_factory = Mock(return_value=mock_session)
    return mock_factory


@pytest.fixture
def mock_db_session():
    """Create a mock database session."""
    return Mock()


@pytest.fixture
def operation_context():
    """Create an operation context for testing."""
    return OperationContext(
        user="test@example.com",
        groups=[],
        tenant_id="default",
        subject_type="user",
        subject_id="test@example.com",
    )


@pytest.fixture
def gmail_connector_no_cache(mock_token_manager):
    """Create Gmail connector without caching."""
    with patch("nexus.server.auth.oauth_factory.OAuthProviderFactory"):
        return GmailConnectorBackend(
            token_manager_db="sqlite:///test.db",
            user_email="test@example.com",
            provider="gmail",
        )


@pytest.fixture
def gmail_connector_with_cache(mock_token_manager, mock_session_factory):
    """Create Gmail connector with caching via session_factory."""
    with patch("nexus.server.auth.oauth_factory.OAuthProviderFactory"):
        return GmailConnectorBackend(
            token_manager_db="sqlite:///test.db",
            user_email="test@example.com",
            provider="gmail",
            session_factory=mock_session_factory,
        )


@pytest.fixture
def gmail_connector_legacy_cache(mock_token_manager, mock_db_session):
    """Create Gmail connector with legacy db_session caching."""
    with patch("nexus.server.auth.oauth_factory.OAuthProviderFactory"):
        # Capture deprecation warning
        with warnings.catch_warnings(record=True):
            warnings.simplefilter("always")
            return GmailConnectorBackend(
                token_manager_db="sqlite:///test.db",
                user_email="test@example.com",
                provider="gmail",
                db_session=mock_db_session,
            )


class TestGmailConnectorInitialization:
    """Test Gmail connector backend initialization."""

    def test_init_basic(self, mock_token_manager):
        """Test basic initialization without caching."""
        with patch("nexus.server.auth.oauth_factory.OAuthProviderFactory"):
            backend = GmailConnectorBackend(
                token_manager_db="sqlite:///test.db",
                user_email="test@example.com",
            )

            assert backend.user_email == "test@example.com"
            assert backend.provider == "gmail"
            assert backend.max_results == 100
            assert backend.labels == ["INBOX"]
            assert backend.session_factory is None
            assert backend.db_session is None

    def test_init_with_session_factory(self, mock_token_manager, mock_session_factory):
        """Test initialization with session_factory for caching."""
        with patch("nexus.server.auth.oauth_factory.OAuthProviderFactory"):
            backend = GmailConnectorBackend(
                token_manager_db="sqlite:///test.db",
                user_email="test@example.com",
                session_factory=mock_session_factory,
            )

            assert backend.session_factory is mock_session_factory
            assert backend.db_session is None

    def test_init_with_db_session_shows_deprecation_warning(self, mock_token_manager, mock_db_session):
        """Test that using db_session shows deprecation warning."""
        with patch("nexus.server.auth.oauth_factory.OAuthProviderFactory"):
            with warnings.catch_warnings(record=True) as w:
                warnings.simplefilter("always")

                backend = GmailConnectorBackend(
                    token_manager_db="sqlite:///test.db",
                    user_email="test@example.com",
                    db_session=mock_db_session,
                )

                # Check deprecation warning was raised
                assert len(w) == 1
                assert issubclass(w[0].category, DeprecationWarning)
                assert "db_session" in str(w[0].message)
                assert "session_factory" in str(w[0].message)
                assert backend.db_session is mock_db_session

    def test_init_with_both_session_factory_and_db_session(
        self, mock_token_manager, mock_session_factory, mock_db_session
    ):
        """Test that session_factory takes precedence when both are provided."""
        with patch("nexus.server.auth.oauth_factory.OAuthProviderFactory"):
            # Should not show deprecation warning since session_factory is provided
            with warnings.catch_warnings(record=True) as w:
                warnings.simplefilter("always")

                backend = GmailConnectorBackend(
                    token_manager_db="sqlite:///test.db",
                    user_email="test@example.com",
                    session_factory=mock_session_factory,
                    db_session=mock_db_session,
                )

                # No deprecation warning should be raised
                assert len(w) == 0
                assert backend.session_factory is mock_session_factory
                assert backend.db_session is mock_db_session

    def test_init_with_custom_labels(self, mock_token_manager):
        """Test initialization with custom labels."""
        with patch("nexus.server.auth.oauth_factory.OAuthProviderFactory"):
            backend = GmailConnectorBackend(
                token_manager_db="sqlite:///test.db",
                user_email="test@example.com",
                labels=["INBOX", "SENT", "DRAFTS"],
            )

            assert backend.labels == ["INBOX", "SENT", "DRAFTS"]

    def test_init_with_max_results(self, mock_token_manager):
        """Test initialization with custom max_results."""
        with patch("nexus.server.auth.oauth_factory.OAuthProviderFactory"):
            backend = GmailConnectorBackend(
                token_manager_db="sqlite:///test.db",
                user_email="test@example.com",
                max_results=50,
            )

            assert backend.max_results == 50


class TestHasCaching:
    """Test _has_caching() helper method."""

    def test_has_caching_with_session_factory(self, gmail_connector_with_cache):
        """Test _has_caching returns True with session_factory."""
        assert gmail_connector_with_cache._has_caching() is True

    def test_has_caching_with_db_session(self, gmail_connector_legacy_cache):
        """Test _has_caching returns True with db_session."""
        assert gmail_connector_legacy_cache._has_caching() is True

    def test_has_caching_without_cache(self, gmail_connector_no_cache):
        """Test _has_caching returns False without caching."""
        assert gmail_connector_no_cache._has_caching() is False


class TestReadContent:
    """Test read_content with cache integration."""

    def test_read_content_cache_hit(self, gmail_connector_with_cache, operation_context):
        """Test read_content returns cached content when available."""
        # Mock cache hit
        cached_data = Mock()
        cached_data.stale = False
        cached_data.content_binary = b"cached email content"

        with patch.object(gmail_connector_with_cache, "_read_from_cache", return_value=cached_data):
            result = gmail_connector_with_cache.read_content("message123", operation_context)

            assert result == b"cached email content"
            gmail_connector_with_cache._read_from_cache.assert_called_once()

    def test_read_content_cache_miss_fetches_from_gmail(
        self, gmail_connector_with_cache, operation_context
    ):
        """Test read_content fetches from Gmail on cache miss."""
        # Mock cache miss
        with patch.object(gmail_connector_with_cache, "_read_from_cache", side_effect=Exception("Not in cache")):
            with patch.object(gmail_connector_with_cache, "_get_gmail_service") as mock_service:
                # Mock Gmail API response
                mock_gmail = Mock()
                mock_response = {"raw": "SGVsbG8gV29ybGQh"}  # "Hello World!" base64
                mock_gmail.users().messages().get().execute.return_value = mock_response
                mock_service.return_value = mock_gmail

                with patch.object(gmail_connector_with_cache, "_write_to_cache"):
                    result = gmail_connector_with_cache.read_content("message123", operation_context)

                    assert result == b"Hello World!"
                    # Should cache the result
                    gmail_connector_with_cache._write_to_cache.assert_called_once()

    def test_read_content_no_cache_fetches_from_gmail(self, gmail_connector_no_cache, operation_context):
        """Test read_content without caching fetches directly from Gmail."""
        with patch.object(gmail_connector_no_cache, "_get_gmail_service") as mock_service:
            # Mock Gmail API response
            mock_gmail = Mock()
            mock_response = {"raw": "SGVsbG8gV29ybGQh"}  # "Hello World!" base64
            mock_gmail.users().messages().get().execute.return_value = mock_response
            mock_service.return_value = mock_gmail

            result = gmail_connector_no_cache.read_content("message123", operation_context)

            assert result == b"Hello World!"

    def test_read_content_not_found(self, gmail_connector_with_cache, operation_context):
        """Test read_content raises error when message not found."""
        with patch.object(gmail_connector_with_cache, "_read_from_cache", side_effect=Exception("Not in cache")):
            with patch.object(gmail_connector_with_cache, "_get_gmail_service") as mock_service:
                # Mock Gmail API 404
                mock_gmail = Mock()
                mock_gmail.users().messages().get().execute.side_effect = Exception("404: Not found")
                mock_service.return_value = mock_gmail

                with pytest.raises(NexusFileNotFoundError):
                    gmail_connector_with_cache.read_content("nonexistent", operation_context)


class TestSync:
    """Test sync() with incremental and full sync modes."""

    def test_sync_with_cache_guards(self, gmail_connector_with_cache, operation_context):
        """Test that sync uses _has_caching guards before cache operations."""
        with patch.object(gmail_connector_with_cache, "_get_gmail_service") as mock_service:
            # Mock Gmail API responses
            mock_gmail = Mock()
            mock_gmail.users().messages().list().execute.return_value = {
                "messages": [{"id": "msg1"}],
                "historyId": "12345",
            }
            mock_gmail.users().messages().get().execute.return_value = {
                "raw": "SGVsbG8h",  # "Hello!" base64
            }
            mock_service.return_value = mock_gmail

            # Mock cache operations
            with patch.object(gmail_connector_with_cache, "_write_to_cache") as mock_cache:
                with patch.object(gmail_connector_with_cache, "_generate_embeddings"):
                    result = gmail_connector_with_cache.sync(
                        path="INBOX",
                        generate_embeddings=False,
                        context=operation_context,
                    )

                    # Should call cache operations since caching is enabled
                    assert mock_cache.called
                    assert result.files_synced > 0

    def test_sync_without_cache_skips_cache_operations(self, gmail_connector_no_cache, operation_context):
        """Test that sync skips cache operations when caching is disabled."""
        with patch.object(gmail_connector_no_cache, "_get_gmail_service") as mock_service:
            # Mock Gmail API responses
            mock_gmail = Mock()
            mock_gmail.users().messages().list().execute.return_value = {
                "messages": [{"id": "msg1"}],
                "historyId": "12345",
            }
            mock_gmail.users().messages().get().execute.return_value = {
                "raw": "SGVsbG8h",
            }
            mock_service.return_value = mock_gmail

            result = gmail_connector_no_cache.sync(
                path="INBOX",
                context=operation_context,
            )

            # Should still sync files even without cache
            assert result.files_scanned > 0

    def test_sync_incremental_with_history_id(self, gmail_connector_with_cache, operation_context):
        """Test incremental sync using historyId."""
        with patch.object(gmail_connector_with_cache, "_get_gmail_service") as mock_service:
            # Mock Gmail History API response
            mock_gmail = Mock()
            mock_gmail.users().history().list().execute.return_value = {
                "history": [
                    {"messagesAdded": [{"message": {"id": "new_msg", "labelIds": ["INBOX"]}}]}
                ],
                "historyId": "67890",
            }
            mock_gmail.users().messages().get().execute.return_value = {
                "raw": "TmV3IG1lc3NhZ2Uh",  # "New message!" base64
            }
            mock_service.return_value = mock_gmail

            with patch.object(gmail_connector_with_cache, "_write_to_cache"):
                result = gmail_connector_with_cache.sync(
                    path="INBOX",
                    history_id="12345",  # Start from this history ID
                    generate_embeddings=False,
                    context=operation_context,
                )

                assert result.files_scanned == 1
                assert hasattr(result, "history_id")
                assert result.history_id == "67890"

    def test_sync_handles_deleted_messages(self, gmail_connector_with_cache, operation_context):
        """Test that sync handles deleted messages with cache invalidation."""
        with patch.object(gmail_connector_with_cache, "_get_gmail_service") as mock_service:
            # Mock Gmail History API response with deleted message
            mock_gmail = Mock()
            mock_gmail.users().history().list().execute.return_value = {
                "history": [
                    {"messagesDeleted": [{"message": {"id": "deleted_msg"}}]}
                ],
                "historyId": "67890",
            }
            mock_service.return_value = mock_gmail

            with patch.object(gmail_connector_with_cache, "_invalidate_cache") as mock_invalidate:
                result = gmail_connector_with_cache.sync(
                    path="INBOX",
                    history_id="12345",
                    context=operation_context,
                )

                # Should invalidate cache for deleted message (both .yaml and .html files)
                assert mock_invalidate.call_count == 2
                # Verify both YAML and HTML files are invalidated
                call_args_list = mock_invalidate.call_args_list
                yaml_call = call_args_list[0]
                html_call = call_args_list[1]
                assert "deleted_msg.yaml" in yaml_call[1]["path"]
                assert yaml_call[1]["delete"] is True
                assert ".deleted_msg.html" in html_call[1]["path"]
                assert html_call[1]["delete"] is True

    def test_sync_embeddings_only_with_cache(self, gmail_connector_with_cache, operation_context):
        """Test that embeddings are only generated when caching is enabled."""
        with patch.object(gmail_connector_with_cache, "_get_gmail_service") as mock_service:
            mock_gmail = Mock()
            mock_gmail.users().messages().list().execute.return_value = {
                "messages": [{"id": "msg1"}],
                "historyId": "12345",
            }
            mock_gmail.users().messages().get().execute.return_value = {
                "raw": "SGVsbG8h",
            }
            mock_service.return_value = mock_gmail

            with patch.object(gmail_connector_with_cache, "_write_to_cache"):
                with patch.object(gmail_connector_with_cache, "_generate_embeddings") as mock_embed:
                    result = gmail_connector_with_cache.sync(
                        path="INBOX",
                        generate_embeddings=True,
                        context=operation_context,
                    )

                    # Should call embeddings since caching is enabled
                    assert mock_embed.called


class TestWriteAndDelete:
    """Test write and delete operations (should fail - read-only)."""

    def test_write_content_raises_error(self, gmail_connector_with_cache, operation_context):
        """Test that write_content raises error (read-only)."""
        with pytest.raises(BackendError) as exc_info:
            gmail_connector_with_cache.write_content(b"Test email", operation_context)

        assert "read-only" in str(exc_info.value).lower()

    def test_delete_content_raises_error(self, gmail_connector_with_cache, operation_context):
        """Test that delete_content raises error (read-only)."""
        with pytest.raises(BackendError) as exc_info:
            gmail_connector_with_cache.delete_content("message123", operation_context)

        assert "read-only" in str(exc_info.value).lower()


class TestBackendProperties:
    """Test backend property methods."""

    def test_backend_name(self, gmail_connector_no_cache):
        """Test backend name property."""
        assert gmail_connector_no_cache.name == "gmail"

    def test_user_scoped(self, gmail_connector_no_cache):
        """Test user_scoped property."""
        assert gmail_connector_no_cache.user_scoped is True


class TestListDir:
    """Test list_dir method for listing messages in a label."""

    def test_list_dir_success(self, gmail_connector_no_cache, operation_context):
        """Test successfully listing messages in INBOX."""
        with patch.object(gmail_connector_no_cache, "_get_gmail_service") as mock_service:
            # Mock Gmail API response
            mock_gmail = Mock()
            mock_gmail.users().messages().list().execute.return_value = {
                "messages": [
                    {"id": "msg123", "threadId": "thread1"},
                    {"id": "msg456", "threadId": "thread2"},
                    {"id": "msg789", "threadId": "thread3"},
                ],
                "historyId": "12345",
            }
            mock_service.return_value = mock_gmail

            result = gmail_connector_no_cache.list_dir("/INBOX", operation_context)

            assert len(result) == 3
            assert result == ["msg123.yaml", "msg456.yaml", "msg789.yaml"]

    def test_list_dir_empty_label(self, gmail_connector_no_cache, operation_context):
        """Test listing an empty label (no messages)."""
        with patch.object(gmail_connector_no_cache, "_get_gmail_service") as mock_service:
            # Mock Gmail API response with no messages
            mock_gmail = Mock()
            mock_gmail.users().messages().list().execute.return_value = {
                "messages": [],
                "historyId": "12345",
            }
            mock_service.return_value = mock_gmail

            result = gmail_connector_no_cache.list_dir("/SENT", operation_context)

            assert result == []

    def test_list_dir_strip_slashes(self, gmail_connector_no_cache, operation_context):
        """Test that path slashes are properly stripped."""
        with patch.object(gmail_connector_no_cache, "_get_gmail_service") as mock_service:
            # Mock Gmail API response
            mock_gmail = Mock()
            mock_gmail.users().messages().list().execute.return_value = {
                "messages": [{"id": "msg123", "threadId": "thread1"}],
                "historyId": "12345",
            }
            mock_service.return_value = mock_gmail

            # Test with leading/trailing slashes
            result = gmail_connector_no_cache.list_dir("/INBOX/", operation_context)

            assert len(result) == 1
            assert result[0] == "msg123.yaml"

    def test_list_dir_respects_max_results(self, gmail_connector_no_cache, operation_context):
        """Test that list_dir respects max_results setting."""
        # Set max_results to 2
        gmail_connector_no_cache.max_results = 2

        with patch.object(gmail_connector_no_cache, "_get_gmail_service") as mock_service:
            # Mock Gmail API response with exactly max_results messages
            mock_gmail = Mock()
            mock_gmail.users().messages().list().execute.return_value = {
                "messages": [
                    {"id": "msg1", "threadId": "thread1"},
                    {"id": "msg2", "threadId": "thread2"},
                ],
                "historyId": "12345",
            }
            mock_service.return_value = mock_gmail

            result = gmail_connector_no_cache.list_dir("/INBOX", operation_context)

            assert len(result) == 2

    def test_list_dir_api_failure(self, gmail_connector_no_cache, operation_context):
        """Test that list_dir raises BackendError when Gmail API fails."""
        with patch.object(gmail_connector_no_cache, "_get_gmail_service") as mock_service:
            # Mock Gmail API failure
            mock_gmail = Mock()
            mock_gmail.users().messages().list().execute.side_effect = Exception("API Error")
            mock_service.return_value = mock_gmail

            with pytest.raises(BackendError) as exc_info:
                gmail_connector_no_cache.list_dir("/INBOX", operation_context)

            assert "Failed to fetch messages from Gmail" in str(exc_info.value)

    def test_list_dir_pagination(self, gmail_connector_no_cache, operation_context):
        """Test that list_dir handles pagination correctly."""
        with patch.object(gmail_connector_no_cache, "_get_gmail_service") as mock_service:
            # Mock Gmail API response with pagination
            mock_gmail = Mock()

            # First page
            first_response = {
                "messages": [{"id": "msg1", "threadId": "thread1"}],
                "historyId": "12345",
                "nextPageToken": "page2",
            }

            # Second page (final)
            second_response = {
                "messages": [{"id": "msg2", "threadId": "thread2"}],
                "historyId": "12345",
            }

            mock_gmail.users().messages().list().execute.side_effect = [
                first_response,
                second_response,
            ]
            mock_service.return_value = mock_gmail

            result = gmail_connector_no_cache.list_dir("/INBOX", operation_context)

            # Should have messages from both pages
            assert len(result) == 2
            assert result == ["msg1.yaml", "msg2.yaml"]


class TestYAMLFormatting:
    """Test YAML formatting with literal block scalars for text_body."""

    def test_text_body_uses_literal_block_scalar(self, gmail_connector_with_cache, operation_context):
        """Test that text_body field uses YAML literal block scalar (|) to preserve newlines."""
        import base64
        import email

        # Create a realistic email with newlines in the text body
        email_content = """From: sender@example.com
To: recipient@example.com
Subject: Test Email with Multiple Lines
Date: Mon, 29 Nov 2025 12:00:00 +0000
Content-Type: text/plain; charset="UTF-8"

Hello World!

This is a test email with multiple lines.
Each line should be preserved properly.

Best regards,
Test Sender"""

        # Encode to base64 (as Gmail API returns)
        raw_email = base64.urlsafe_b64encode(email_content.encode()).decode()

        with patch.object(gmail_connector_with_cache, "_get_gmail_service") as mock_service:
            # Mock Gmail API responses
            mock_gmail = Mock()

            # Mock list response
            mock_gmail.users().messages().list().execute.return_value = {
                "messages": [{"id": "msg123"}],
                "historyId": "12345",
            }

            # Mock get response with our test email
            mock_gmail.users().messages().get().execute.return_value = {
                "id": "msg123",
                "raw": raw_email,
            }

            mock_service.return_value = mock_gmail

            # Mock _write_to_cache to capture what's being written
            written_yaml_content = None
            def capture_write(*args, **kwargs):
                nonlocal written_yaml_content
                written_yaml_content = kwargs.get("content")

            with patch.object(gmail_connector_with_cache, "_write_to_cache", side_effect=capture_write):
                # Sync to write the message
                gmail_connector_with_cache.sync(
                    path="INBOX",
                    generate_embeddings=False,
                    context=operation_context,
                )

            # Verify the YAML content was written
            assert written_yaml_content is not None

            # Decode the YAML content
            yaml_str = written_yaml_content.decode("utf-8")

            # Verify the YAML uses literal block scalar (|) for text_body
            assert "text_body: |" in yaml_str, (
                "Expected text_body to use literal block scalar (|), "
                f"but YAML content is:\n{yaml_str}"
            )

            # Verify that newlines are preserved (not escaped as \n)
            # The literal block scalar should have actual line breaks, not \n
            assert "\\n" not in yaml_str.split("text_body: |")[1], (
                "Expected text_body to have actual newlines, not escaped \\n characters"
            )

            # Parse the YAML to verify structure
            import yaml
            parsed = yaml.safe_load(yaml_str)

            # Verify the structure
            assert "headers" in parsed
            assert "text_body" in parsed
            assert "html_body" not in parsed  # Should not include html_body

            # Verify text_body contains newlines
            text_body = parsed["text_body"]
            assert "\n" in text_body, "Expected text_body to contain actual newlines"
            assert "Hello World!" in text_body
            assert "This is a test email with multiple lines." in text_body
            assert "Best regards," in text_body

    def test_yaml_includes_cc_bcc_and_labels(self, gmail_connector_with_cache, operation_context):
        """Test that YAML includes cc, bcc, and labels fields when present."""
        import base64
        import email

        # Create an email with cc, bcc, and labels
        email_content = """From: sender@example.com
To: recipient@example.com
Cc: cc1@example.com, cc2@example.com
Bcc: bcc@example.com
Subject: Test Email with CC and BCC
Date: Mon, 29 Nov 2025 12:00:00 +0000
Content-Type: text/plain; charset="UTF-8"

Test email body."""

        # Encode to base64 (as Gmail API returns)
        raw_email = base64.urlsafe_b64encode(email_content.encode()).decode()

        with patch.object(gmail_connector_with_cache, "_get_gmail_service") as mock_service:
            # Mock Gmail API responses
            mock_gmail = Mock()

            # Mock list response
            mock_gmail.users().messages().list().execute.return_value = {
                "messages": [{"id": "msg123"}],
                "historyId": "12345",
            }

            # Mock get response with labels
            mock_gmail.users().messages().get().execute.return_value = {
                "id": "msg123",
                "labelIds": ["INBOX", "UNREAD", "IMPORTANT"],
                "raw": raw_email,
            }

            mock_service.return_value = mock_gmail

            # Mock _write_to_cache to capture what's being written
            written_yaml_content = None
            def capture_write(*args, **kwargs):
                nonlocal written_yaml_content
                written_yaml_content = kwargs.get("content")

            with patch.object(gmail_connector_with_cache, "_write_to_cache", side_effect=capture_write):
                # Sync to write the message
                gmail_connector_with_cache.sync(
                    path="INBOX",
                    generate_embeddings=False,
                    context=operation_context,
                )

            # Verify the YAML content was written
            assert written_yaml_content is not None

            # Decode and parse the YAML content
            import yaml
            yaml_str = written_yaml_content.decode("utf-8")
            parsed = yaml.safe_load(yaml_str)

            # Verify headers include cc and bcc
            assert "headers" in parsed
            assert "cc" in parsed["headers"]
            assert "bcc" in parsed["headers"]
            assert parsed["headers"]["cc"] == "cc1@example.com, cc2@example.com"
            assert parsed["headers"]["bcc"] == "bcc@example.com"

            # Verify labels are present
            assert "labels" in parsed
            assert parsed["labels"] == ["INBOX", "UNREAD", "IMPORTANT"]

    def test_yaml_handles_crlf_line_endings(self, gmail_connector_with_cache, operation_context):
        """Test that YAML properly handles CRLF line endings from real Gmail messages.

        Uses actual content from LinkedIn message 19ad169a81e10dcd fetched via Gmail API.
        """
        import base64
        import email

        # Actual LinkedIn email with CRLF line endings (RFC 822 format)
        # Extracted from real Gmail message ID 19ad169a81e10dcd
        email_content = (
            "From: LinkedIn News <editors-noreply@linkedin.com>\r\n"
            "Message-ID: <75940982.19257950.1764449946488@ltx1-app59265.prod.linkedin.com>\r\n"
            "Subject: The problem with 'device hoarding'\r\n"
            "MIME-Version: 1.0\r\n"
            "Content-Type: multipart/alternative; \r\n"
            "\tboundary=\"----=_Part_19257947_1644883092.1764449946483\"\r\n"
            "To: \"Joe (Jinjing) Zhou\" <jinjing@multifi.ai>\r\n"
            "Date: Sat, 29 Nov 2025 20:59:06 +0000 (UTC)\r\n"
            "X-LinkedIn-Class: EMAIL-DEFAULT\r\n"
            "X-LinkedIn-Template: email_editorial_suggested_top_conversations_01\r\n"
            "\r\n"
            "------=_Part_19257947_1644883092.1764449946483\r\n"
            "Content-Type: text/plain;charset=UTF-8\r\n"
            "Content-Transfer-Encoding: quoted-printable\r\n"
            "Content-ID: text-body\r\n"
            "\r\n"
            "----------------------------------------\r\n"
            "\r\n"
            "This email was intended for Joe (Jinjing) Zhou (Co-Founder @ MultiFi.ai)\r\n"
            "Learn why we included this: https://www.linkedin.com/help/linkedin/answer/4788\r\n"
            "You are receiving Suggested Top Conversations emails\r\n"
            "\r\n"
            "\r\n"
            "Unsubscribe: https://www.linkedin.com/comm/psettings/email-unsubscribe\r\n"
            "Help: https://www.linkedin.com/help/linkedin/answer/67\r\n"
            "\r\n"
            "© 2025 LinkedIn Corporation, 1000 West Maude Avenue, Sunnyvale, CA 94085.\r\n"
            "LinkedIn and the LinkedIn logo are registered trademarks of LinkedIn.\r\n"
            "------=_Part_19257947_1644883092.1764449946483\r\n"
            "Content-Type: text/html;charset=UTF-8\r\n"
            "Content-Transfer-Encoding: quoted-printable\r\n"
            "Content-ID: html-body\r\n"
            "\r\n"
            "<html><body>HTML content here</body></html>\r\n"
            "------=_Part_19257947_1644883092.1764449946483--\r\n"
        )

        # Encode to base64 (as Gmail API returns)
        raw_email = base64.urlsafe_b64encode(email_content.encode()).decode()

        with patch.object(gmail_connector_with_cache, "_get_gmail_service") as mock_service:
            # Mock Gmail API responses
            mock_gmail = Mock()

            # Mock list response
            mock_gmail.users().messages().list().execute.return_value = {
                "messages": [{"id": "19ad169a81e10dcd"}],
                "historyId": "12345",
            }

            # Mock get response with actual labels from the real message
            mock_gmail.users().messages().get().execute.return_value = {
                "id": "19ad169a81e10dcd",
                "labelIds": ["UNREAD", "Label_9", "CATEGORY_UPDATES", "INBOX"],
                "raw": raw_email,
            }

            mock_service.return_value = mock_gmail

            # Mock _write_to_cache to capture what's being written
            written_yaml_content = None
            def capture_write(*args, **kwargs):
                nonlocal written_yaml_content
                # Only capture the YAML file, not the HTML file
                path = kwargs.get("path", "")
                if path.endswith(".yaml"):
                    written_yaml_content = kwargs.get("content")

            with patch.object(gmail_connector_with_cache, "_write_to_cache", side_effect=capture_write):
                # Sync to write the message
                gmail_connector_with_cache.sync(
                    path="INBOX",
                    generate_embeddings=False,
                    context=operation_context,
                )

            # Verify the YAML content was written
            assert written_yaml_content is not None

            # Decode the YAML content
            yaml_str = written_yaml_content.decode("utf-8")

            # Verify the YAML uses literal block scalar (|) for text_body
            assert "text_body: |" in yaml_str, (
                f"Expected text_body to use literal block scalar (|), but got:\n{yaml_str}"
            )

            # Verify that CRLF are NOT escaped as literal strings
            # The content should have actual newlines, not the escaped string "\r\n"
            text_body_section = yaml_str.split("text_body: |")[1]
            assert "\\r\\n" not in text_body_section, (
                f"Expected no escaped \\r\\n in text_body, but found them in:\n{text_body_section}"
            )

            # Parse the YAML to verify content
            import yaml
            parsed = yaml.safe_load(yaml_str)

            # Verify the text body was properly parsed
            text_body = parsed["text_body"]
            # Check for content from the actual LinkedIn email
            assert "This email was intended for Joe (Jinjing) Zhou (Co-Founder @ MultiFi.ai)" in text_body
            assert "https://www.linkedin.com/help/linkedin/answer/4788" in text_body
            assert "You are receiving Suggested Top Conversations emails" in text_body
            assert "2025 LinkedIn Corporation" in text_body

            # Verify it has actual newlines (not escaped)
            assert "\n" in text_body
            # Verify no CRLF sequences remain
            assert "\r\n" not in text_body
            assert "\r" not in text_body

            # Verify labels from actual message
            assert parsed["labels"] == ["UNREAD", "Label_9", "CATEGORY_UPDATES", "INBOX"]

            # Verify headers
            assert parsed["headers"]["from"] == "LinkedIn News <editors-noreply@linkedin.com>"
            assert parsed["headers"]["to"] == '"Joe (Jinjing) Zhou" <jinjing@multifi.ai>'
            assert parsed["headers"]["subject"] == "The problem with 'device hoarding'"


class TestGmailCachePathFix:
    """Test suite for Gmail cache path consistency fix.

    Verifies that cache paths used during sync (write) match paths used
    during read operations, ensuring cache hits instead of cache misses.
    """

    def test_sync_uses_full_virtual_paths_for_cache(
        self, gmail_connector_with_cache, mock_gmail_service, operation_context
    ):
        """Test that sync() writes cache entries with full virtual paths including mount_point.

        This is the fix for cache misses. Previously, sync wrote with backend-relative
        paths like "/inbox/msg.yaml" but read_content used full paths like
        "/mnt/gmail_test/inbox/msg.yaml", causing 100% cache misses.
        """
        connector = gmail_connector_with_cache
        mount_point = "/mnt/gmail_test"

        # Mock Gmail API responses
        with patch.object(connector, "_get_gmail_service", return_value=mock_gmail_service):
            # Mock list messages response
            mock_gmail = mock_gmail_service
            mock_gmail.users().messages().list().execute.return_value = {
                "messages": [{"id": "msg123", "threadId": "thread123"}],
                "resultSizeEstimate": 1,
            }

            # Mock get message response with simple content
            mock_gmail.users().messages().get().execute.return_value = {
                "id": "msg123",
                "labelIds": ["INBOX"],
                "payload": {
                    "headers": [
                        {"name": "From", "value": "test@example.com"},
                        {"name": "To", "value": "user@example.com"},
                        {"name": "Subject", "value": "Test Subject"},
                        {"name": "Date", "value": "Mon, 1 Jan 2024 12:00:00 +0000"},
                    ],
                    "body": {"data": "VGVzdCBtZXNzYWdlIGJvZHk="},  # base64: "Test message body"
                },
            }

            # Track cache write calls
            written_paths = []
            original_write = connector._write_to_cache

            def capture_write(*args, **kwargs):
                path = kwargs.get("path")
                written_paths.append(path)
                return original_write(*args, **kwargs)

            with patch.object(connector, "_write_to_cache", side_effect=capture_write):
                # Run sync with mount_point and specific label
                result = connector.sync(
                    path="INBOX",  # Sync specific label, not root "/"
                    mount_point=mount_point,
                    context=operation_context,
                )

            # Verify paths were written with full mount_point prefix
            assert len(written_paths) >= 1, "Should have written at least one cache entry"

            # All paths should start with mount_point
            for path in written_paths:
                assert path.startswith(mount_point), (
                    f"Cache path '{path}' should start with mount_point '{mount_point}'. "
                    f"This ensures read operations can find cached entries."
                )

            # Verify specific path format for YAML file
            yaml_paths = [p for p in written_paths if p.endswith(".yaml")]
            assert len(yaml_paths) >= 1, "Should have written YAML cache entry"
            assert yaml_paths[0] == f"{mount_point}/inbox/msg123.yaml", (
                f"YAML path should be '{mount_point}/inbox/msg123.yaml', "
                f"got '{yaml_paths[0]}'"
            )

    def test_virtual_path_includes_label_folder(
        self, gmail_connector_with_cache, mock_gmail_service, operation_context
    ):
        """Test that virtual paths include the Gmail label as a folder in the path.

        The path structure should be: /mount_point/label/message_id.yaml
        This ensures messages are organized by label (INBOX, SENT, etc.)
        """
        connector = gmail_connector_with_cache
        mount_point = "/mnt/gmail_test"

        # Mock Gmail API responses for INBOX label
        with patch.object(connector, "_get_gmail_service", return_value=mock_gmail_service):
            mock_gmail = mock_gmail_service
            mock_gmail.users().messages().list().execute.return_value = {
                "messages": [{"id": "msg_inbox", "threadId": "thread1"}],
                "resultSizeEstimate": 1,
            }

            mock_gmail.users().messages().get().execute.return_value = {
                "id": "msg_inbox",
                "labelIds": ["INBOX"],
                "payload": {
                    "headers": [
                        {"name": "From", "value": "test@example.com"},
                        {"name": "Subject", "value": "Test"},
                    ],
                    "body": {"data": "VGVzdA=="},
                },
            }

            # Track cache write calls
            written_paths = []
            original_write = connector._write_to_cache

            def capture_write(*args, **kwargs):
                path = kwargs.get("path")
                written_paths.append(path)
                return original_write(*args, **kwargs)

            with patch.object(connector, "_write_to_cache", side_effect=capture_write):
                # Sync INBOX label
                connector.sync(
                    path="INBOX",
                    mount_point=mount_point,
                    context=operation_context,
                )

            # Verify the label "inbox" is included in the path
            yaml_paths = [p for p in written_paths if p.endswith(".yaml")]
            assert len(yaml_paths) >= 1, "Should have written cache entry"

            # Path should have format: /mount_point/label/message_id.yaml
            expected_path = f"{mount_point}/inbox/msg_inbox.yaml"
            assert yaml_paths[0] == expected_path, (
                f"Virtual path should include label folder: expected '{expected_path}', "
                f"got '{yaml_paths[0]}'"
            )

            # Verify the path components
            path_parts = yaml_paths[0].split("/")
            assert "inbox" in path_parts, (
                f"Path should contain 'inbox' label folder, got parts: {path_parts}"
            )

            # Verify HTML path also includes label
            html_paths = [p for p in written_paths if p.endswith(".html")]
            if html_paths:
                expected_html = f"{mount_point}/inbox/.msg_inbox.html"
                assert html_paths[0] == expected_html, (
                    f"HTML path should also include label: expected '{expected_html}', "
                    f"got '{html_paths[0]}'"
                )

    def test_read_content_uses_consistent_cache_path(
        self, gmail_connector_with_cache, operation_context
    ):
        """Test that read_content() uses the same path format as sync() for cache lookups.

        Verifies that when reading a file, the cache lookup uses the full virtual_path
        from the context, which should match the path used during sync.
        """
        connector = gmail_connector_with_cache
        mount_point = "/mnt/gmail_test"
        msg_id = "msg123"

        # Populate cache with correct full path
        full_path = f"{mount_point}/inbox/{msg_id}.yaml"
        test_content = "test: data\nfrom: test@example.com"
        connector._write_to_cache(path=full_path, content=test_content.encode())

        # Create context with full virtual_path (as it would be in real usage)
        context = OperationContext(
            user=operation_context.user,
            groups=operation_context.groups,
            tenant_id=operation_context.tenant_id,
            virtual_path=full_path,  # Full path including mount point
        )

        # Track cache read calls
        read_paths = []
        original_read = connector._read_from_cache

        def capture_read(path):
            read_paths.append(path)
            return original_read(path)

        with patch.object(connector, "_read_from_cache", side_effect=capture_read):
            # Attempt to read content
            try:
                content = connector.read_content(context=context)
                # If read succeeds, verify it used the correct path
                assert len(read_paths) > 0
                assert read_paths[0] == full_path, (
                    f"read_content should use full path '{full_path}', "
                    f"got '{read_paths[0]}'"
                )
            except NexusFileNotFoundError:
                # If file not found, still verify the path attempted was correct
                assert len(read_paths) > 0
                assert read_paths[0] == full_path, (
                    f"Cache lookup should use full path '{full_path}', "
                    f"got '{read_paths[0]}'"
                )


class TestGmailHistoryIdPersistence:
    """Test suite for Gmail history_id persistence fix.

    Verifies that sync() accepts and returns history_id parameter,
    enabling incremental sync via Gmail History API.
    """

    def test_sync_returns_history_id_in_result(
        self, gmail_connector_with_cache, mock_gmail_service, operation_context
    ):
        """Test that sync() returns history_id in CacheSyncResult.

        This enables incremental sync - the returned history_id should be
        persisted and passed back in subsequent sync calls.
        """
        connector = gmail_connector_with_cache

        with patch.object(connector, "_get_gmail_service", return_value=mock_gmail_service):
            mock_gmail = mock_gmail_service

            # Mock list messages response with historyId
            mock_gmail.users().messages().list().execute.return_value = {
                "messages": [{"id": "msg123", "threadId": "thread123"}],
                "resultSizeEstimate": 1,
            }

            # Mock get message response with historyId
            mock_gmail.users().messages().get().execute.return_value = {
                "id": "msg123",
                "historyId": "987654321",  # New history ID
                "labelIds": ["INBOX"],
                "payload": {
                    "headers": [
                        {"name": "From", "value": "test@example.com"},
                        {"name": "Subject", "value": "Test"},
                    ],
                    "body": {"data": "VGVzdA=="},
                },
            }

            # Run sync without history_id (initial sync)
            result = connector.sync(
                path="INBOX",  # Sync specific label
                mount_point="/mnt/gmail_test",
                context=operation_context,
            )

            # Verify result has history_id
            assert hasattr(result, "history_id"), "Result should have history_id attribute"
            assert result.history_id is not None, "history_id should not be None"
            assert result.history_id == "987654321", (
                f"history_id should be '987654321', got '{result.history_id}'"
            )

    def test_sync_accepts_history_id_for_incremental_sync(
        self, gmail_connector_with_cache, mock_gmail_service, operation_context
    ):
        """Test that sync() uses history_id parameter for incremental sync.

        When history_id is provided, sync should use Gmail History API instead of
        listing all messages, making subsequent syncs 10-100x faster.
        """
        connector = gmail_connector_with_cache
        previous_history_id = "123456789"

        with patch.object(connector, "_get_gmail_service", return_value=mock_gmail_service):
            mock_gmail = mock_gmail_service

            # Mock history list response (incremental sync)
            mock_gmail.users().history().list().execute.return_value = {
                "history": [
                    {
                        "id": "987654321",
                        "messagesAdded": [
                            {
                                "message": {
                                    "id": "new_msg",
                                    "threadId": "thread456",
                                    "labelIds": ["INBOX"],
                                }
                            }
                        ],
                    }
                ],
                "historyId": "987654321",  # New history ID
            }

            # Mock get message for the new message
            mock_gmail.users().messages().get().execute.return_value = {
                "id": "new_msg",
                "historyId": "987654321",
                "labelIds": ["INBOX"],
                "payload": {
                    "headers": [
                        {"name": "From", "value": "test@example.com"},
                        {"name": "Subject", "value": "New Message"},
                    ],
                    "body": {"data": "TmV3IG1lc3NhZ2U="},
                },
            }

            # Run sync WITH history_id (incremental sync)
            result = connector.sync(
                path="INBOX",  # Sync specific label
                mount_point="/mnt/gmail_test",
                history_id=previous_history_id,  # Pass previous history_id
                context=operation_context,
            )

            # Verify history API was called (not messages.list)
            mock_gmail.users().history().list.assert_called_once()
            history_call_kwargs = mock_gmail.users().history().list.call_args[1]
            assert history_call_kwargs["startHistoryId"] == previous_history_id, (
                f"History API should be called with startHistoryId='{previous_history_id}'"
            )

            # Verify new history_id is returned
            assert hasattr(result, "history_id")
            assert result.history_id == "987654321"

    def test_sync_without_history_id_uses_full_sync(
        self, gmail_connector_with_cache, mock_gmail_service, operation_context
    ):
        """Test that sync() without history_id performs full sync via messages.list().

        When no history_id is provided (initial sync), should list all messages
        instead of using History API.
        """
        connector = gmail_connector_with_cache

        with patch.object(connector, "_get_gmail_service", return_value=mock_gmail_service):
            mock_gmail = mock_gmail_service

            # Mock list messages response (full sync)
            mock_gmail.users().messages().list().execute.return_value = {
                "messages": [{"id": "msg1", "threadId": "thread1"}],
                "resultSizeEstimate": 1,
            }

            mock_gmail.users().messages().get().execute.return_value = {
                "id": "msg1",
                "historyId": "111111111",
                "labelIds": ["INBOX"],
                "payload": {
                    "headers": [
                        {"name": "From", "value": "test@example.com"},
                        {"name": "Subject", "value": "Test"},
                    ],
                    "body": {"data": "VGVzdA=="},
                },
            }

            # Run sync WITHOUT history_id (initial sync)
            result = connector.sync(
                path="INBOX",  # Sync specific label
                mount_point="/mnt/gmail_test",
                context=operation_context,
                # history_id NOT provided
            )

            # Verify messages.list was called (not history API)
            mock_gmail.users().messages().list.assert_called_once()

            # Verify history API was NOT called
            mock_gmail.users().history().list.assert_not_called()

            # Verify history_id is still returned for next sync
            assert hasattr(result, "history_id")
            assert result.history_id is not None
