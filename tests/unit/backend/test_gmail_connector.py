"""Unit tests for Gmail connector backend.

This test suite covers the Gmail connector functionality:
1. Initialization with OAuth
2. Email syncing (date-based and historyId-based)
3. Directory listing
4. Email reading
5. Label change handling
6. HistoryId persistence
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, Mock, patch

import pytest

from nexus.backends.gmail_connector import GmailConnectorBackend
from nexus.core.exceptions import BackendError, NexusFileNotFoundError
from nexus.core.permissions import OperationContext


class TestGmailConnectorInitialization:
    """Test Gmail connector initialization."""

    @pytest.fixture
    def mock_token_manager(self):
        """Mock TokenManager."""
        token_manager = Mock()
        token_manager.db_path = ":memory:"
        token_manager.db_url = None
        token_manager.register_provider = Mock()
        token_manager.get_valid_token = AsyncMock(return_value="test_token")
        return token_manager

    @pytest.fixture
    def connector(self, mock_token_manager):
        """Create a GmailConnectorBackend instance with mocked TokenManager."""
        with patch("nexus.backends.gmail_connector.TokenManager", return_value=mock_token_manager):
            backend = GmailConnectorBackend(
                token_manager_db=":memory:",
                user_email="test@gmail.com",
                sync_from_date="2024-01-01",
                provider="gmail",
            )
            backend.token_manager = mock_token_manager
            return backend

    def test_initialization_with_sync_from_date(self, connector):
        """Test initialization with sync_from_date."""
        assert connector.user_email == "test@gmail.com"
        assert connector.provider == "gmail"
        assert connector.sync_from_date is not None
        assert connector.last_history_id is None

    def test_initialization_with_last_history_id(self, mock_token_manager):
        """Test initialization with last_history_id for incremental sync."""
        with patch("nexus.backends.gmail_connector.TokenManager", return_value=mock_token_manager):
            backend = GmailConnectorBackend(
                token_manager_db=":memory:",
                user_email="test@gmail.com",
                last_history_id="12345",
                provider="gmail",
            )
            assert backend.last_history_id == "12345"
            assert backend.sync_from_date is not None  # Still set as fallback (30 days ago)

    def test_initialization_defaults_to_30_days_ago(self, mock_token_manager):
        """Test that sync_from_date defaults to 30 days ago if not provided."""
        with patch("nexus.backends.gmail_connector.TokenManager", return_value=mock_token_manager):
            backend = GmailConnectorBackend(
                token_manager_db=":memory:",
                user_email="test@gmail.com",
                provider="gmail",
            )
            expected_date = datetime.now(UTC) - timedelta(days=30)
            # Allow 1 second tolerance
            assert abs((backend.sync_from_date - expected_date).total_seconds()) < 1

    def test_set_mount_point(self, connector):
        """Test setting mount point."""
        connector.set_mount_point("/mnt/gmail")
        assert connector._mount_point == "/mnt/gmail"

    def test_get_updated_config_no_sync(self, connector):
        """Test get_updated_config returns None if no sync performed."""
        assert connector.get_updated_config() is None

    def test_get_updated_config_with_history_id(self, connector):
        """Test get_updated_config returns config with historyId after sync."""
        connector._current_history_id = "12345"
        config = connector.get_updated_config()
        assert config is not None
        assert config["last_history_id"] == "12345"
        assert config["token_manager_db"] == ":memory:"
        assert config["provider"] == "gmail"
        assert config["user_email"] == "test@gmail.com"


class TestGmailConnectorSync:
    """Test Gmail connector email syncing."""

    @pytest.fixture
    def mock_token_manager(self):
        """Mock TokenManager."""
        token_manager = Mock()
        token_manager.db_path = ":memory:"
        token_manager.db_url = None
        token_manager.register_provider = Mock()
        token_manager.get_valid_token = AsyncMock(return_value="test_token")
        return token_manager

    @pytest.fixture
    def mock_gmail_service(self):
        """Mock Gmail service."""
        service = Mock()

        # Mock profile
        profile = {"historyId": "41182853"}
        service.users.return_value.getProfile.return_value.execute.return_value = profile

        # Mock messages list
        messages_result = {
            "messages": [
                {"id": "msg1"},
                {"id": "msg2"},
            ],
            "nextPageToken": None,
        }
        service.users.return_value.messages.return_value.list.return_value.execute.return_value = (
            messages_result
        )

        # Mock message get
        def get_message(message_id):
            message = {
                "id": message_id,
                "threadId": message_id,
                "labelIds": ["INBOX", "UNREAD"],
                "snippet": "Test email snippet",
                "historyId": "41182853",
                "sizeEstimate": 1000,
                "payload": {
                    "headers": [
                        {"name": "Date", "value": "Mon, 25 Nov 2024 10:00:00 -0800"},
                        {"name": "Subject", "value": "Test Subject"},
                        {"name": "From", "value": "test@example.com"},
                        {"name": "To", "value": "recipient@example.com"},
                    ],
                    "parts": [
                        {
                            "mimeType": "text/plain",
                            "body": {"data": "dGVzdCBib2R5"},  # base64 "test body"
                        }
                    ],
                },
            }
            return message

        def execute_get(userId, id, format):
            return get_message(id)

        service.users.return_value.messages.return_value.get.return_value.execute.side_effect = (
            execute_get
        )

        return service

    @pytest.fixture
    def connector(self, mock_token_manager):
        """Create a GmailConnectorBackend instance."""
        with patch("nexus.backends.gmail_connector.TokenManager", return_value=mock_token_manager):
            backend = GmailConnectorBackend(
                token_manager_db=":memory:",
                user_email="test@gmail.com",
                sync_from_date="2024-11-25",
                provider="gmail",
            )
            backend.token_manager = mock_token_manager
            return backend

    @pytest.fixture
    def context(self):
        """Create operation context."""
        return OperationContext(
            user="test@gmail.com",
            user_id="test@gmail.com",
            groups=[],
            tenant_id="default",
            subject_type="user",
            subject_id="test@gmail.com",
        )

    def test_sync_from_date_initial_sync(self, connector, mock_gmail_service, context):
        """Test initial sync using date-based query."""
        with (
            patch.object(connector, "_get_gmail_service", return_value=mock_gmail_service),
            patch.object(connector, "_fetch_email") as mock_fetch,
        ):
            # Mock _fetch_email to return email data
            mock_fetch.return_value = {
                "id": "msg1",
                "threadId": "msg1",
                "labelIds": ["INBOX"],
                "snippet": "Test",
                "date": "2024-11-25T10:00:00-08:00",
                "headers": {},
                "subject": "Test",
                "from": "test@example.com",
                "to": "recipient@example.com",
                "body_text": "test body",
                "body_html": "",
                "sizeEstimate": 1000,
                "historyId": "41182853",
            }

            connector._sync_from_date(mock_gmail_service)

            # Verify emails were fetched
            assert len(connector._email_cache) == 2
            assert connector._current_history_id == "41182853"

    def test_sync_from_history_incremental_sync(self, connector, mock_gmail_service, context):
        """Test incremental sync using historyId."""
        # Mock history API
        history_result = {
            "history": [
                {
                    "messagesAdded": [
                        {"message": {"id": "msg3"}},
                    ],
                    "labelsAdded": [
                        {"message": {"id": "msg1"}, "labelIds": ["STARRED"]},
                    ],
                }
            ],
            "nextPageToken": None,
        }
        mock_gmail_service.users.return_value.history.return_value.list.return_value.execute.return_value = history_result

        with (
            patch.object(connector, "_get_gmail_service", return_value=mock_gmail_service),
            patch.object(connector, "_fetch_email") as mock_fetch,
        ):
            mock_fetch.return_value = {
                "id": "msg3",
                "threadId": "msg3",
                "labelIds": ["INBOX"],
                "snippet": "Test",
                "date": "2024-11-25T10:00:00-08:00",
                "headers": {},
                "subject": "Test",
                "from": "test@example.com",
                "to": "recipient@example.com",
                "body_text": "test body",
                "body_html": "",
                "sizeEstimate": 1000,
                "historyId": "41182854",
            }

            connector.last_history_id = "41182850"
            connector._sync_from_history(mock_gmail_service, "41182850")

            # Verify historyId was updated (from profile)
            assert connector._current_history_id == "41182853"
            # Verify email was fetched
            assert "msg3" in connector._email_cache

    def test_sync_handles_label_changes(self, connector, mock_gmail_service, context):
        """Test that sync handles label changes on existing emails."""
        # Add an email to cache first
        connector._email_cache["msg1"] = {
            "id": "msg1",
            "labelIds": ["INBOX"],
            "date": "2024-11-25T10:00:00-08:00",
        }

        # Mock history with label change
        history_result = {
            "history": [
                {
                    "labelsAdded": [
                        {"message": {"id": "msg1"}, "labelIds": ["STARRED"]},
                    ],
                }
            ],
            "nextPageToken": None,
        }
        mock_gmail_service.users.return_value.history.return_value.list.return_value.execute.return_value = history_result

        with (
            patch.object(connector, "_get_gmail_service", return_value=mock_gmail_service),
            patch.object(connector, "_fetch_email") as mock_fetch,
        ):
            mock_fetch.return_value = {
                "id": "msg1",
                "threadId": "msg1",
                "labelIds": ["INBOX", "STARRED"],  # Updated labels
                "snippet": "Test",
                "date": "2024-11-25T10:00:00-08:00",
                "headers": {},
                "subject": "Test",
                "from": "test@example.com",
                "to": "recipient@example.com",
                "body_text": "test body",
                "body_html": "",
                "sizeEstimate": 1000,
                "historyId": "41182854",
            }

            connector.last_history_id = "41182850"
            connector._sync_from_history(mock_gmail_service, "41182850")

            # Verify email was re-fetched (mock_fetch should be called)
            assert mock_fetch.called
            # Verify updated labels are in cache
            assert "STARRED" in connector._email_cache["msg1"]["labelIds"]

    def test_sync_handles_label_removed(self, connector, mock_gmail_service, context):
        """Test that sync handles label removal on existing emails."""
        # Add an email to cache first
        connector._email_cache["msg1"] = {
            "id": "msg1",
            "labelIds": ["INBOX", "STARRED"],
            "date": "2024-11-25T10:00:00-08:00",
        }

        # Mock history with label removal
        history_result = {
            "history": [
                {
                    "labelsRemoved": [
                        {"message": {"id": "msg1"}, "labelIds": ["STARRED"]},
                    ],
                }
            ],
            "nextPageToken": None,
        }
        mock_gmail_service.users.return_value.history.return_value.list.return_value.execute.return_value = history_result

        with (
            patch.object(connector, "_get_gmail_service", return_value=mock_gmail_service),
            patch.object(connector, "_fetch_email") as mock_fetch,
        ):
            mock_fetch.return_value = {
                "id": "msg1",
                "threadId": "msg1",
                "labelIds": ["INBOX"],  # STARRED removed
                "snippet": "Test",
                "date": "2024-11-25T10:00:00-08:00",
                "headers": {},
                "subject": "Test",
                "from": "test@example.com",
                "to": "recipient@example.com",
                "body_text": "test body",
                "body_html": "",
                "sizeEstimate": 1000,
                "historyId": "41182854",
            }

            connector.last_history_id = "41182850"
            connector._sync_from_history(mock_gmail_service, "41182850")

            # Verify email was re-fetched
            assert mock_fetch.called
            # Verify STARRED label is removed
            assert "STARRED" not in connector._email_cache["msg1"]["labelIds"]
            assert "INBOX" in connector._email_cache["msg1"]["labelIds"]

    def test_sync_handles_deleted_messages(self, connector, mock_gmail_service, context):
        """Test that sync handles deleted messages."""
        # Add an email to cache first
        connector._email_cache["msg1"] = {
            "id": "msg1",
            "labelIds": ["INBOX"],
            "date": "2024-11-25T10:00:00-08:00",
        }

        # Mock history with message deletion
        history_result = {
            "history": [
                {
                    "messagesDeleted": [
                        {"message": {"id": "msg1"}},
                    ],
                }
            ],
            "nextPageToken": None,
        }
        mock_gmail_service.users.return_value.history.return_value.list.return_value.execute.return_value = history_result

        with patch.object(connector, "_get_gmail_service", return_value=mock_gmail_service):
            connector.last_history_id = "41182850"
            connector._sync_from_history(mock_gmail_service, "41182850")

            # Verify deleted email is removed from cache
            assert "msg1" not in connector._email_cache

    def test_sync_falls_back_to_date_if_history_too_old(
        self, connector, mock_gmail_service, context
    ):
        """Test that sync falls back to date-based sync if historyId is too old."""

        # Mock history API to raise error with "400" or "historyId" in message
        class HistoryIdError(Exception):
            def __str__(self):
                return "400 Bad Request: historyId too old"

        mock_gmail_service.users.return_value.history.return_value.list.return_value.execute.side_effect = HistoryIdError()

        with (
            patch.object(connector, "_get_gmail_service", return_value=mock_gmail_service),
            patch.object(connector, "_sync_from_date") as mock_sync_date,
        ):
            connector.last_history_id = "41182850"
            connector._sync_from_history(mock_gmail_service, "41182850")

            # Verify fallback to date-based sync
            assert mock_sync_date.called


class TestGmailConnectorDirectoryOperations:
    """Test Gmail connector directory operations."""

    @pytest.fixture
    def connector(self):
        """Create a GmailConnectorBackend with sample email cache."""
        with patch("nexus.backends.gmail_connector.TokenManager"):
            backend = GmailConnectorBackend(
                token_manager_db=":memory:",
                user_email="test@gmail.com",
                provider="gmail",
            )

            # Add sample emails to cache
            backend._email_cache = {
                "msg1": {
                    "id": "msg1",
                    "date": "2024-11-25T10:00:00-08:00",
                },
                "msg2": {
                    "id": "msg2",
                    "date": "2024-11-25T15:00:00-08:00",
                },
                "msg3": {
                    "id": "msg3",
                    "date": "2024-11-26T10:00:00-08:00",
                },
            }
            return backend

    @pytest.fixture
    def context(self):
        """Create operation context."""
        return OperationContext(
            user="test@gmail.com",
            user_id="test@gmail.com",
            groups=[],
            tenant_id="default",
            subject_type="user",
            subject_id="test@gmail.com",
        )

    def test_list_dir_root(self, connector, context):
        """Test listing root directory shows years."""
        with patch.object(connector, "_sync_emails"):  # Skip actual sync
            entries = connector.list_dir("", context)
            assert "2024/" in entries

    def test_list_dir_year(self, connector, context):
        """Test listing year directory shows months."""
        with patch.object(connector, "_sync_emails"):
            entries = connector.list_dir("emails/2024", context)
            assert "11/" in entries

    def test_list_dir_month(self, connector, context):
        """Test listing month directory shows days."""
        with patch.object(connector, "_sync_emails"):
            entries = connector.list_dir("emails/2024/11", context)
            assert "25/" in entries
            assert "26/" in entries

    def test_list_dir_day(self, connector, context):
        """Test listing day directory shows email files."""
        with patch.object(connector, "_sync_emails"):
            entries = connector.list_dir("emails/2024/11/25", context)
            assert "email-msg1.yaml" in entries
            assert "email-msg2.yaml" in entries

    def test_is_directory(self, connector):
        """Test is_directory correctly identifies directories."""
        assert connector.is_directory("") is True  # Root
        assert connector.is_directory("emails") is True
        assert connector.is_directory("emails/2024") is True
        assert connector.is_directory("emails/2024/11") is True
        assert connector.is_directory("emails/2024/11/25") is True
        assert connector.is_directory("emails/2024/11/25/email-msg1.yaml") is False


class TestGmailConnectorReadOperations:
    """Test Gmail connector read operations."""

    @pytest.fixture
    def connector(self):
        """Create a GmailConnectorBackend with sample email cache."""
        with patch("nexus.backends.gmail_connector.TokenManager"):
            backend = GmailConnectorBackend(
                token_manager_db=":memory:",
                user_email="test@gmail.com",
                provider="gmail",
            )

            # Add sample email to cache
            backend._email_cache = {
                "msg1": {
                    "id": "msg1",
                    "threadId": "msg1",
                    "labelIds": ["INBOX"],
                    "snippet": "Test snippet",
                    "date": "2024-11-25T10:00:00-08:00",
                    "headers": {"Subject": "Test"},
                    "subject": "Test",
                    "from": "test@example.com",
                    "to": "recipient@example.com",
                    "body_text": "test body",
                    "body_html": "<p>test body</p>",
                    "sizeEstimate": 1000,
                    "historyId": "41182853",
                }
            }
            return backend

    def test_yaml_literal_block_scalar_formatting(self):
        """Test that multiline body_text with Unicode uses literal block scalar format."""
        # Mock TokenManager at its actual import location
        mock_token_manager = Mock()
        mock_token_manager.db_path = ":memory:"
        mock_token_manager.db_url = None
        mock_token_manager.register_provider = Mock()

        with patch("nexus.server.auth.token_manager.TokenManager", return_value=mock_token_manager):
            backend = GmailConnectorBackend(
                token_manager_db=":memory:",
                user_email="test@gmail.com",
                provider="gmail",
            )

            # Add email with multiline body_text including Unicode characters (emojis)
            backend._email_cache = {
                "msg1": {
                    "id": "msg1",
                    "threadId": "msg1",
                    "labelIds": ["INBOX"],
                    "snippet": "Test snippet",
                    "date": "2024-11-25T10:00:00-08:00",
                    "headers": {"Subject": "Hilton Rewards"},
                    "subject": "Hilton Rewards",
                    "from": "rewards@hilton.com",
                    "to": "recipient@example.com",
                    "body_text": "\n\n🎁125,000 Hilton Honors Points Bonus after $1,000 spent\nPlus up to $100 statement credit",
                    "body_html": "<html><body><p>Rewards info</p></body></html>",
                    "sizeEstimate": 1000,
                    "historyId": "41182853",
                }
            }

            # Read the YAML file
            context = OperationContext(
                user="test@gmail.com",
                user_id="test@gmail.com",
                groups=[],
                tenant_id="default",
                subject_type="user",
                subject_id="test@gmail.com",
                backend_path="email-msg1.yaml",
            )

            content = backend.read_content("", context)
            yaml_str = content.decode("utf-8")

            # Print actual output for debugging
            print("\n=== Actual YAML Output ===")
            print(yaml_str)
            print("=== End YAML Output ===\n")

            # Verify literal block scalar format is used
            # The YAML should use |- or | style for multiline strings
            assert "body_text: |-" in yaml_str or "body_text: |" in yaml_str, (
                f"Expected literal block scalar format (|- or |), but got inline format. YAML:\n{yaml_str}"
            )

            # Should not have escaped newlines in the YAML output
            assert "\\n" not in yaml_str, (
                f"Found escaped newlines (\\n) in YAML output. Expected literal block format. YAML:\n{yaml_str}"
            )

            # Should not have inline quoted string format
            assert 'body_text: "' not in yaml_str, (
                f"Found inline quoted string format. Expected literal block format. YAML:\n{yaml_str}"
            )

            # Content should be present and properly formatted
            assert "🎁125,000 Hilton Honors Points" in yaml_str, (
                "Unicode characters (emojis) should be present in output"
            )

    def test_read_content_from_cache(self, connector):
        """Test reading email content from cache."""
        context = OperationContext(
            user="test@gmail.com",
            user_id="test@gmail.com",
            groups=[],
            tenant_id="default",
            subject_type="user",
            subject_id="test@gmail.com",
            backend_path="emails/2024/11/25/email-msg1.yaml",
        )

        content = connector.read_content("", context)

        # Should be YAML format
        assert b"id: msg1" in content
        assert b"labelIds:" in content
        assert b"body_text:" in content

    def test_read_content_not_found(self, connector):
        """Test reading non-existent email raises error."""
        context = OperationContext(
            user="test@gmail.com",
            user_id="test@gmail.com",
            groups=[],
            tenant_id="default",
            subject_type="user",
            subject_id="test@gmail.com",
            backend_path="emails/2024/11/25/email-nonexistent.yaml",
        )

        with pytest.raises(NexusFileNotFoundError):
            connector.read_content("", context)

    def test_content_exists(self, connector):
        """Test content_exists checks email in cache."""
        context = OperationContext(
            user="test@gmail.com",
            user_id="test@gmail.com",
            groups=[],
            tenant_id="default",
            subject_type="user",
            subject_id="test@gmail.com",
            backend_path="emails/2024/11/25/email-msg1.yaml",
        )

        assert connector.content_exists("", context) is True

        context.backend_path = "emails/2024/11/25/email-nonexistent.yaml"
        assert connector.content_exists("", context) is False


class TestGmailConnectorWriteOperations:
    """Test Gmail connector write operations (should be read-only)."""

    @pytest.fixture
    def connector(self):
        """Create a GmailConnectorBackend instance."""
        with patch("nexus.backends.gmail_connector.TokenManager"):
            return GmailConnectorBackend(
                token_manager_db=":memory:",
                user_email="test@gmail.com",
                provider="gmail",
            )

    def test_write_content_raises_error(self, connector):
        """Test that write_content raises error (read-only backend)."""
        context = OperationContext(
            user="test@gmail.com",
            user_id="test@gmail.com",
            groups=[],
            tenant_id="default",
            subject_type="user",
            subject_id="test@gmail.com",
        )

        with pytest.raises(BackendError) as exc_info:
            connector.write_content(b"test", context)
        assert "read-only" in str(exc_info.value).lower()

    def test_delete_content_raises_error(self, connector):
        """Test that delete_content raises error (read-only backend)."""
        context = OperationContext(
            user="test@gmail.com",
            user_id="test@gmail.com",
            groups=[],
            tenant_id="default",
            subject_type="user",
            subject_id="test@gmail.com",
        )

        with pytest.raises(BackendError) as exc_info:
            connector.delete_content("hash", context)
        assert "read-only" in str(exc_info.value).lower()

    def test_mkdir_raises_error(self, connector):
        """Test that mkdir raises error (read-only backend)."""
        context = OperationContext(
            user="test@gmail.com",
            user_id="test@gmail.com",
            groups=[],
            tenant_id="default",
            subject_type="user",
            subject_id="test@gmail.com",
        )

        with pytest.raises(BackendError) as exc_info:
            connector.mkdir("test", context=context)
        assert "read-only" in str(exc_info.value).lower()


class TestGmailConnectorHistoryId:
    """Test Gmail connector historyId handling."""

    @pytest.fixture
    def connector(self):
        """Create a GmailConnectorBackend instance."""
        with patch("nexus.backends.gmail_connector.TokenManager"):
            return GmailConnectorBackend(
                token_manager_db=":memory:",
                user_email="test@gmail.com",
                last_history_id="41182850",
                provider="gmail",
            )

    def test_get_last_history_id_from_config(self, connector):
        """Test get_last_history_id returns configured historyId if no sync."""
        connector.last_history_id = "41182850"
        assert connector.get_last_history_id() == "41182850"

    def test_get_last_history_id_from_sync(self, connector):
        """Test get_last_history_id returns current historyId after sync."""
        connector._current_history_id = "41182853"
        assert connector.get_last_history_id() == "41182853"

    def test_get_last_history_id_prefers_current(self, connector):
        """Test get_last_history_id prefers _current_history_id over last_history_id."""
        connector.last_history_id = "41182850"
        connector._current_history_id = "41182853"
        assert connector.get_last_history_id() == "41182853"

    def test_get_last_history_id_returns_none_if_no_history(self, connector):
        """Test get_last_history_id returns None if no historyId set."""
        connector.last_history_id = None
        connector._current_history_id = None
        assert connector.get_last_history_id() is None

    def test_get_updated_config_includes_history_id(self, connector):
        """Test get_updated_config includes new historyId."""
        connector._current_history_id = "41182853"
        config = connector.get_updated_config()
        assert config is not None
        assert config["last_history_id"] == "41182853"
        assert config["last_history_id"] != "41182850"  # Should be updated

    def test_get_updated_config_preserves_other_fields(self, connector):
        """Test get_updated_config preserves other configuration fields."""
        connector._current_history_id = "41182853"
        config = connector.get_updated_config()
        assert config["token_manager_db"] == ":memory:"
        assert config["provider"] == "gmail"
        assert config["user_email"] == "test@gmail.com"


class TestGmailConnectorEmailPath:
    """Test Gmail connector email path generation."""

    @pytest.fixture
    def connector(self):
        """Create a GmailConnectorBackend instance."""
        with patch("nexus.backends.gmail_connector.TokenManager"):
            return GmailConnectorBackend(
                token_manager_db=":memory:",
                user_email="test@gmail.com",
                provider="gmail",
            )

    def test_get_email_path(self, connector):
        """Test _get_email_path generates correct path."""
        date = datetime(2024, 11, 25, 10, 0, 0, tzinfo=UTC)
        path = connector._get_email_path("msg123", date)
        assert path == "emails/2024/11/25/email-msg123.yaml"

    def test_get_email_path_different_dates(self, connector):
        """Test _get_email_path handles different dates correctly."""
        date1 = datetime(2024, 1, 5, 10, 0, 0, tzinfo=UTC)
        date2 = datetime(2024, 12, 31, 23, 59, 59, tzinfo=UTC)

        path1 = connector._get_email_path("msg1", date1)
        path2 = connector._get_email_path("msg2", date2)

        assert path1 == "emails/2024/01/05/email-msg1.yaml"
        assert path2 == "emails/2024/12/31/email-msg2.yaml"
