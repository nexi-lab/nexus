"""Integration tests for Gmail connector.

Tests the Gmail connector end-to-end including:
- Schema validation
- Trait-based validation
- Error formatting with README.md references
- README.md auto-generation from static file
- YAML parsing
- Write operations (send, reply, forward, draft)
- Delete operations (trash)
- MIME message building

Note: These tests mock the Gmail API since we can't
use real OAuth tokens in CI. For full E2E testing with real
Google API, use OAuth authentication via the integrations page.
"""

import base64
from email import message_from_bytes
from unittest.mock import MagicMock, patch

import pytest
import yaml
from pydantic import ValidationError as PydanticValidationError

from nexus.backends.connectors.base import ValidationError
from nexus.backends.connectors.gmail.schemas import (
    DraftEmailSchema,
    ReplyEmailSchema,
    SendEmailSchema,
)
from nexus.backends.connectors.gmail.transport import LABEL_FOLDERS, GmailTransport
from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.contracts.exceptions import BackendError
from nexus.contracts.types import OperationContext

# ============================================================================
# FIXTURES
# ============================================================================


@pytest.fixture
def mock_gmail_service():
    """Create a mock Gmail service."""
    service = MagicMock()

    # Mock messages().send()
    service.users().messages().send().execute.return_value = {
        "id": "sent_msg_123",
        "threadId": "thread_abc",
        "labelIds": ["SENT"],
    }

    # Mock messages().get() — includes Message-ID header for reply/forward tests
    service.users().messages().get().execute.return_value = {
        "id": "msg_123",
        "threadId": "thread_abc",
        "snippet": "This is a test email...",
        "payload": {
            "headers": [
                {"name": "From", "value": "sender@example.com"},
                {"name": "To", "value": "recipient@example.com"},
                {"name": "Subject", "value": "Test Subject"},
                {"name": "Date", "value": "Mon, 15 Jan 2024 09:00:00 -0800"},
                {"name": "Message-ID", "value": "<original-msg-id@example.com>"},
            ],
            "body": {"data": "VGVzdCBlbWFpbCBib2R5"},  # Base64: "Test email body"
        },
        "labelIds": ["INBOX"],
    }

    # Mock messages().list()
    service.users().messages().list().execute.return_value = {
        "messages": [
            {"id": "msg_1", "threadId": "thread_1"},
            {"id": "msg_2", "threadId": "thread_2"},
        ]
    }

    # Mock messages().trash()
    service.users().messages().trash().execute.return_value = {
        "id": "msg_123",
        "labelIds": ["TRASH"],
    }

    # Mock drafts().create()
    service.users().drafts().create().execute.return_value = {
        "id": "draft_123",
        "message": {"id": "msg_draft_123", "threadId": "thread_draft"},
    }

    # Mock drafts().list()
    service.users().drafts().list().execute.return_value = {
        "drafts": [
            {
                "id": "draft_1",
                "message": {"id": "msg_draft_1", "threadId": "thread_d1"},
            },
            {
                "id": "draft_2",
                "message": {"id": "msg_draft_2", "threadId": "thread_d2"},
            },
        ]
    }

    return service


@pytest.fixture
def gmail_backend(mock_gmail_service, tmp_path):
    """Create a Gmail backend with mocked Google service."""
    from nexus.backends.connectors.gmail.connector import PathGmailBackend

    # Create a mock token manager
    with patch(
        "nexus.backends.connectors.gmail.connector.PathGmailBackend._register_oauth_provider"
    ):
        backend = PathGmailBackend(
            token_manager_db=str(tmp_path / "tokens.db"),
            user_email="test@example.com",
        )

    # Replace _get_gmail_service on the transport (OAuth calls)
    backend._gmail_transport._get_gmail_service = MagicMock(return_value=mock_gmail_service)

    return backend


@pytest.fixture
def operation_context():
    """Create an operation context for testing."""
    return OperationContext(
        user_id="test@example.com",
        groups=[],
        zone_id=ROOT_ZONE_ID,
    )


# ============================================================================
# SCHEMA VALIDATION TESTS
# ============================================================================


class TestSendEmailSchema:
    """Test SendEmailSchema validation."""

    def test_valid_email(self):
        """Test creating email with all required fields."""
        email = SendEmailSchema(
            agent_intent="User requested to send project update to the team",
            to=["alice@example.com", "bob@example.com"],
            subject="Project Update",
            body="Hi team,\n\nHere's the update...",
            confirm=True,
        )

        assert len(email.to) == 2
        assert email.subject == "Project Update"
        assert email.confirm is True

    def test_missing_agent_intent_fails(self):
        """Test that missing agent_intent raises validation error."""
        with pytest.raises(PydanticValidationError) as exc_info:
            SendEmailSchema(
                to=["alice@example.com"],
                subject="Test",
                body="Body",
                confirm=True,
            )

        assert "agent_intent" in str(exc_info.value).lower()

    def test_short_agent_intent_fails(self):
        """Test that short agent_intent raises validation error."""
        with pytest.raises(PydanticValidationError):
            SendEmailSchema(
                agent_intent="short",  # Less than 10 chars
                to=["alice@example.com"],
                subject="Test",
                body="Body",
                confirm=True,
            )

    def test_missing_confirm_fails(self):
        """Test that missing confirm raises error."""
        with pytest.raises(PydanticValidationError):
            SendEmailSchema(
                agent_intent="User requested to send this email",
                to=["alice@example.com"],
                subject="Test",
                body="Body",
                # Missing confirm=True
            )

    def test_invalid_email_format_fails(self):
        """Test that invalid email format raises error."""
        with pytest.raises(PydanticValidationError):
            SendEmailSchema(
                agent_intent="User requested to send this email",
                to=["not-an-email"],
                subject="Test",
                body="Body",
                confirm=True,
            )


class TestReplyEmailSchema:
    """Test ReplyEmailSchema validation."""

    def test_valid_reply(self):
        """Test valid reply with all required fields."""
        reply = ReplyEmailSchema(
            agent_intent="User wants to reply to the project thread",
            thread_id="18c1234567890abc",
            message_id="18c1234567890xyz",
            body="Thanks for the update!",
            confirm=True,
        )

        assert reply.thread_id == "18c1234567890abc"
        assert reply.confirm is True

    def test_reply_requires_thread_id(self):
        """Test that reply requires thread_id."""
        with pytest.raises(PydanticValidationError):
            ReplyEmailSchema(
                agent_intent="User wants to reply",
                message_id="18c1234567890xyz",
                body="Reply body",
                confirm=True,
            )


class TestDraftEmailSchema:
    """Test DraftEmailSchema validation."""

    def test_draft_no_confirm_required(self):
        """Test that drafts don't require confirm=true."""
        draft = DraftEmailSchema(
            agent_intent="User wants to create a draft for later",
            body="Draft content...",
        )

        assert draft.body == "Draft content..."


# ============================================================================
# TRAIT VALIDATION TESTS
# ============================================================================


class TestTraitValidation:
    """Test trait-based validation."""

    def test_send_requires_intent(self, gmail_backend):
        """Test that send_email requires agent_intent."""
        data = {"to": ["test@example.com"]}  # Missing agent_intent

        with pytest.raises(ValidationError) as exc_info:
            gmail_backend.validate_traits("send_email", data)

        assert exc_info.value.code == "MISSING_AGENT_INTENT"
        assert "README.md" in str(exc_info.value)

    def test_send_requires_explicit_confirm(self, gmail_backend):
        """Test that send_email requires confirm=true."""
        data = {
            "agent_intent": "Sending email as requested by user",
            # Missing confirm=True
        }

        with pytest.raises(ValidationError) as exc_info:
            gmail_backend.validate_traits("send_email", data)

        assert exc_info.value.code == "MISSING_CONFIRM"

    def test_valid_send_passes(self, gmail_backend):
        """Test that valid send data passes trait validation."""
        data = {
            "agent_intent": "Sending email as requested by user",
            "confirm": True,
        }

        warnings = gmail_backend.validate_traits("send_email", data)
        assert warnings == []

    def test_draft_only_requires_intent(self, gmail_backend):
        """Test that create_draft only requires intent (no confirm)."""
        data = {
            "agent_intent": "Creating draft for user to review later",
            # No confirm needed for drafts
        }

        warnings = gmail_backend.validate_traits("create_draft", data)
        assert warnings == []


# ============================================================================
# SKILL.MD GENERATION TESTS
# ============================================================================


class TestReadmeDocGeneration:
    """Test README.md loading from static file."""

    def test_generate_readme(self, gmail_backend):
        """Test that README.md is loaded correctly from static file."""
        doc = gmail_backend.generate_readme("/mnt/gmail/")

        # Check header - static README.md
        assert doc.startswith("---\n")
        assert "title: Gmail" in doc
        assert "description:" in doc
        assert "# Gmail Connector" in doc

        # Check mount path is replaced
        assert "`/mnt/gmail/`" in doc

        # Check operations section
        assert "## Operations" in doc
        assert "Send Email" in doc
        assert "Reply" in doc
        assert "Forward" in doc
        assert "Draft" in doc

        # Check required format
        assert "agent_intent" in doc
        assert "confirm: true" in doc

        # Check error codes section
        assert "## Error Codes" in doc
        assert "MISSING_AGENT_INTENT" in doc
        assert "MISSING_CONFIRM" in doc

    def test_readme_doc_includes_examples(self, gmail_backend):
        """Test that README.md includes YAML examples."""
        doc = gmail_backend.generate_readme("/mnt/gmail/")

        # Should include YAML code blocks
        assert "```yaml" in doc
        assert "# agent_intent:" in doc

    def test_readme_doc_mount_path_replacement(self, gmail_backend):
        """Test that mount path is correctly replaced."""
        doc = gmail_backend.generate_readme("/custom/mount/path/")

        # Default path should be replaced
        assert "/mnt/gmail/" not in doc
        assert "/custom/mount/path/" in doc


# ============================================================================
# ERROR FORMATTING TESTS
# ============================================================================


class TestErrorFormatting:
    """Test error message formatting with README.md references."""

    def test_error_includes_readme_path(self, gmail_backend):
        """Test that errors include README.md path."""
        # Set mount path so readme_md_path is computed correctly
        gmail_backend.set_mount_path("/mnt/gmail")

        error = gmail_backend.format_error_with_skill_ref(
            code="MISSING_AGENT_INTENT",
            message="Missing required field",
        )

        assert "/mnt/gmail/.readme/README.md" in str(error)

    def test_error_includes_section_anchor(self, gmail_backend):
        """Test that errors include section anchor."""
        error = gmail_backend.format_error_with_skill_ref(
            code="MISSING_AGENT_INTENT",
            message="Missing required field",
            section="required-format",
        )

        assert "#required-format" in str(error)

    def test_error_includes_fix_example(self, gmail_backend):
        """Test that errors from registry include fix example."""
        error = gmail_backend.format_error_with_skill_ref(
            code="MISSING_CONFIRM",
            message="",
        )

        # Should include fix example from ERROR_REGISTRY
        assert "confirm" in str(error).lower()


# ============================================================================
# CHECKPOINT TESTS
# ============================================================================


class TestCheckpoints:
    """Test checkpoint/rollback functionality."""

    def test_create_checkpoint_for_send(self, gmail_backend):
        """Test that checkpoint is created for send operations."""
        checkpoint = gmail_backend.create_checkpoint(
            "send_email",
            metadata={"to": ["test@example.com"]},
        )

        assert checkpoint is not None
        assert checkpoint.operation == "send_email"

    def test_draft_has_checkpoint(self, gmail_backend):
        """Test that drafts support checkpoints (can be deleted)."""
        checkpoint = gmail_backend.create_checkpoint("create_draft")

        assert checkpoint is not None
        assert checkpoint.operation == "create_draft"

    def test_complete_and_clear_checkpoint(self, gmail_backend):
        """Test completing and clearing checkpoints."""
        checkpoint = gmail_backend.create_checkpoint("send_email")

        # Complete checkpoint
        gmail_backend.complete_checkpoint(
            checkpoint.checkpoint_id,
            {"message_id": "sent_123"},
        )

        stored = gmail_backend.get_checkpoint(checkpoint.checkpoint_id)
        assert stored.created_state["message_id"] == "sent_123"

        # Clear checkpoint
        gmail_backend.clear_checkpoint(checkpoint.checkpoint_id)
        assert gmail_backend.get_checkpoint(checkpoint.checkpoint_id) is None


# ============================================================================
# OPERATION TRAITS TESTS
# ============================================================================


class TestOperationTraits:
    """Test operation trait configuration."""

    def test_send_email_traits(self, gmail_backend):
        """Test send_email has correct traits."""
        from nexus.backends.connectors.base import ConfirmLevel, Reversibility

        traits = gmail_backend.get_operation_traits("send_email")

        assert traits is not None
        assert traits.reversibility == Reversibility.NONE  # Cannot unsend
        assert traits.confirm == ConfirmLevel.EXPLICIT  # Requires confirm
        assert traits.checkpoint is True

    def test_reply_email_traits(self, gmail_backend):
        """Test reply_email has correct traits."""
        from nexus.backends.connectors.base import ConfirmLevel, Reversibility

        traits = gmail_backend.get_operation_traits("reply_email")

        assert traits is not None
        assert traits.reversibility == Reversibility.NONE
        assert traits.confirm == ConfirmLevel.EXPLICIT

    def test_forward_email_traits(self, gmail_backend):
        """Test forward_email has correct traits."""
        from nexus.backends.connectors.base import ConfirmLevel, Reversibility

        traits = gmail_backend.get_operation_traits("forward_email")

        assert traits is not None
        assert traits.reversibility == Reversibility.NONE
        assert traits.confirm == ConfirmLevel.EXPLICIT

    def test_draft_traits(self, gmail_backend):
        """Test create_draft has correct traits."""
        from nexus.backends.connectors.base import ConfirmLevel, Reversibility

        traits = gmail_backend.get_operation_traits("create_draft")

        assert traits is not None
        assert traits.reversibility == Reversibility.FULL  # Can delete draft
        assert traits.confirm == ConfirmLevel.INTENT  # Only needs intent


# ============================================================================
# WRITE OPERATIONS TESTS
# ============================================================================


class TestWriteOperations:
    """Test Gmail write operations (send, reply, forward, draft)."""

    def test_send_email(self, gmail_backend, mock_gmail_service, operation_context):
        """Test sending a new email via write_content."""
        content = yaml.dump(
            {
                "agent_intent": "User requested to send a project update to the team",
                "to": ["alice@example.com"],
                "subject": "Project Update",
                "body": "Hi team, here is the update.",
                "confirm": True,
            }
        ).encode("utf-8")

        ctx = OperationContext(
            user_id="test@example.com",
            groups=[],
            zone_id=ROOT_ZONE_ID,
            backend_path="SENT/_new.yaml",
        )

        result = gmail_backend.write_content(content, context=ctx)

        assert result.content_id == "sent_msg_123"
        assert result.size == len(content)
        mock_gmail_service.users().messages().send.assert_called()

    def test_reply_email(self, gmail_backend, mock_gmail_service, operation_context):
        """Test replying to an email thread via write_content."""
        content = yaml.dump(
            {
                "agent_intent": "User wants to reply to the project thread with feedback",
                "thread_id": "thread_abc",
                "message_id": "msg_123",
                "body": "Thanks for the update! Looks good.",
                "confirm": True,
            }
        ).encode("utf-8")

        ctx = OperationContext(
            user_id="test@example.com",
            groups=[],
            zone_id=ROOT_ZONE_ID,
            backend_path="SENT/_reply.yaml",
        )

        result = gmail_backend.write_content(content, context=ctx)

        assert result.content_id == "sent_msg_123"
        mock_gmail_service.users().messages().send.assert_called()

    def test_forward_email(self, gmail_backend, mock_gmail_service, operation_context):
        """Test forwarding an email via write_content."""
        content = yaml.dump(
            {
                "agent_intent": "User wants to forward the report to the external partner",
                "message_id": "msg_123",
                "to": ["partner@external.com"],
                "comment": "FYI - Here's the report we discussed.",
                "confirm": True,
            }
        ).encode("utf-8")

        ctx = OperationContext(
            user_id="test@example.com",
            groups=[],
            zone_id=ROOT_ZONE_ID,
            backend_path="SENT/_forward.yaml",
        )

        result = gmail_backend.write_content(content, context=ctx)

        assert result.content_id == "sent_msg_123"
        mock_gmail_service.users().messages().send.assert_called()

    def test_create_draft(self, gmail_backend, mock_gmail_service, operation_context):
        """Test creating a draft via write_content."""
        content = yaml.dump(
            {
                "agent_intent": "User wants to create a draft for later review and editing",
                "to": ["client@example.com"],
                "subject": "Re: Project Proposal",
                "body": "Thank you for your proposal...",
            }
        ).encode("utf-8")

        ctx = OperationContext(
            user_id="test@example.com",
            groups=[],
            zone_id=ROOT_ZONE_ID,
            backend_path="DRAFTS/_new.yaml",
        )

        result = gmail_backend.write_content(content, context=ctx)

        assert result.content_id == "draft_123"
        mock_gmail_service.users().drafts().create.assert_called()

    def test_invalid_write_path(self, gmail_backend, operation_context):
        """Test that writing to an invalid path raises BackendError."""
        content = yaml.dump(
            {
                "agent_intent": "This should fail because path is invalid for writing",
                "body": "test",
                "confirm": True,
            }
        ).encode("utf-8")

        ctx = OperationContext(
            user_id="test@example.com",
            groups=[],
            zone_id=ROOT_ZONE_ID,
            backend_path="INBOX/_new.yaml",  # INBOX doesn't support writes
        )

        with pytest.raises(BackendError):
            gmail_backend.write_content(content, context=ctx)


# ============================================================================
# DELETE (TRASH) OPERATIONS TESTS
# ============================================================================


class TestDeleteOperations:
    """Test Gmail delete (trash) operations."""

    def test_trash_message(self, gmail_backend, mock_gmail_service, operation_context):
        """Test trashing a message via delete_content."""
        ctx = OperationContext(
            user_id="test@example.com",
            groups=[],
            zone_id=ROOT_ZONE_ID,
            backend_path="INBOX/thread_abc-msg_123.yaml",
        )

        gmail_backend.delete_content("", context=ctx)

        mock_gmail_service.users().messages().trash.assert_called()

    def test_trash_invalid_path(self, gmail_backend, operation_context):
        """Test that trashing with a sentinel path raises error."""
        ctx = OperationContext(
            user_id="test@example.com",
            groups=[],
            zone_id=ROOT_ZONE_ID,
            backend_path="SENT/_new.yaml",
        )

        with pytest.raises(BackendError):
            gmail_backend.delete_content("", context=ctx)


# ============================================================================
# MIME BUILDING TESTS
# ============================================================================


class TestMimeBuilding:
    """Test MIME message building helpers on GmailTransport."""

    @pytest.fixture
    def transport(self):
        """Create a GmailTransport with test config (no real auth)."""
        transport = GmailTransport(
            token_manager=MagicMock(),
            provider="gmail",
            user_email="me@example.com",
        )
        return transport

    def test_build_basic_mime(self, transport):
        """Test basic MIME message structure."""
        data = {
            "to": ["alice@example.com", "bob@example.com"],
            "subject": "Test Subject",
            "body": "Hello, this is a test.",
        }

        msg = transport._build_mime_message(data)

        assert msg["From"] == "me@example.com"
        assert "alice@example.com" in msg["To"]
        assert "bob@example.com" in msg["To"]
        assert msg["Subject"] == "Test Subject"

        # Verify body content
        body = msg.get_content()
        assert "Hello, this is a test." in body

    def test_build_mime_with_cc_bcc(self, transport):
        """Test MIME message with CC and BCC."""
        data = {
            "to": ["alice@example.com"],
            "cc": ["cc@example.com"],
            "bcc": ["bcc@example.com"],
            "subject": "With CC/BCC",
            "body": "Content",
        }

        msg = transport._build_mime_message(data)

        assert "cc@example.com" in msg["Cc"]
        assert "bcc@example.com" in msg["Bcc"]

    def test_build_mime_with_html(self, transport):
        """Test MIME message with HTML alternative."""
        data = {
            "to": ["alice@example.com"],
            "subject": "HTML Email",
            "body": "Plain text version",
            "html_body": "<h1>HTML version</h1>",
        }

        msg = transport._build_mime_message(data)

        # Should be multipart/alternative
        assert msg.get_content_type() == "multipart/alternative"

        # Verify both parts exist
        parts = list(msg.iter_parts())
        assert len(parts) == 2
        assert parts[0].get_content_type() == "text/plain"
        assert parts[1].get_content_type() == "text/html"

    def test_build_reply_threading_headers(self, transport):
        """Test reply MIME has correct threading headers."""
        data = {
            "body": "This is my reply.",
        }
        original = {
            "from": "sender@example.com",
            "to": "me@example.com",
            "subject": "Original Subject",
            "headers": {
                "From": "sender@example.com",
                "To": "me@example.com",
                "Subject": "Original Subject",
                "Message-ID": "<original-123@example.com>",
            },
        }

        msg = transport._build_reply_mime(data, original)

        assert msg["Subject"] == "Re: Original Subject"
        assert msg["In-Reply-To"] == "<original-123@example.com>"
        assert msg["References"] == "<original-123@example.com>"
        assert "sender@example.com" in msg["To"]

    def test_build_reply_already_re_prefix(self, transport):
        """Test reply does not double the Re: prefix."""
        data = {"body": "Reply text"}
        original = {
            "from": "sender@example.com",
            "subject": "Re: Already replied",
            "headers": {
                "From": "sender@example.com",
                "Subject": "Re: Already replied",
            },
        }

        msg = transport._build_reply_mime(data, original)

        assert msg["Subject"] == "Re: Already replied"
        assert not msg["Subject"].startswith("Re: Re:")

    def test_build_forward_separator(self, transport):
        """Test forward message includes forwarded separator."""
        data = {
            "to": ["forward@example.com"],
            "comment": "FYI - see below.",
        }
        original = {
            "from": "sender@example.com",
            "to": "me@example.com",
            "subject": "Important Report",
            "date": "2024-01-15T09:00:00-08:00",
            "body_text": "Here is the report content.",
            "headers": {
                "From": "sender@example.com",
                "To": "me@example.com",
                "Subject": "Important Report",
                "Date": "2024-01-15T09:00:00-08:00",
            },
        }

        msg = transport._build_forward_mime(data, original)

        assert msg["Subject"] == "Fwd: Important Report"
        assert "forward@example.com" in msg["To"]

        body = msg.get_content()
        assert "---------- Forwarded message ----------" in body
        assert "From: sender@example.com" in body
        assert "FYI - see below." in body
        assert "Here is the report content." in body

    def test_encode_mime_raw(self, transport):
        """Test base64url encoding of MIME message."""
        data = {
            "to": ["test@example.com"],
            "subject": "Encoding Test",
            "body": "Hello",
        }

        msg = transport._build_mime_message(data)
        raw = GmailTransport._encode_mime_raw(msg)

        # Should be valid base64url
        decoded = base64.urlsafe_b64decode(raw)
        parsed = message_from_bytes(decoded)
        assert parsed["Subject"] == "Encoding Test"

    def test_build_mime_with_inline_attachment(self, transport):
        """Test MIME message with inline base64 attachment."""
        file_content = b"Hello, this is a test file."
        data = {
            "to": ["test@example.com"],
            "subject": "With Attachment",
            "body": "See attached.",
            "attachments": [
                {
                    "data": base64.b64encode(file_content).decode(),
                    "filename": "test.txt",
                    "content_type": "text/plain",
                },
            ],
        }

        msg = transport._build_mime_message(data)
        raw_bytes = msg.as_bytes()
        parsed = message_from_bytes(raw_bytes)

        # Should be multipart/mixed (body + attachment)
        assert parsed.is_multipart()
        parts = list(parsed.walk())
        # Find the attachment part
        attachment_parts = [p for p in parts if p.get_filename() == "test.txt"]
        assert len(attachment_parts) == 1
        assert attachment_parts[0].get_payload(decode=True) == file_content

    def test_build_mime_with_multiple_attachments(self, transport):
        """Test MIME message with multiple attachments."""
        data = {
            "to": ["test@example.com"],
            "subject": "Multi Attach",
            "body": "Two files.",
            "attachments": [
                {
                    "data": base64.b64encode(b"file1").decode(),
                    "filename": "a.txt",
                },
                {
                    "data": base64.b64encode(b"file2").decode(),
                    "filename": "b.pdf",
                    "content_type": "application/pdf",
                },
            ],
        }

        msg = transport._build_mime_message(data)
        parts = list(msg.walk())
        filenames = [p.get_filename() for p in parts if p.get_filename()]
        assert "a.txt" in filenames
        assert "b.pdf" in filenames

    def test_build_mime_skips_path_only_attachment(self, transport):
        """Test that path-only attachments (no data) are skipped gracefully."""
        data = {
            "to": ["test@example.com"],
            "subject": "Path Only",
            "body": "No inline data.",
            "attachments": [
                {"path": "/mnt/storage/file.pdf", "filename": "file.pdf"},
            ],
        }

        msg = transport._build_mime_message(data)
        # Should not crash, attachment skipped (path-based not yet supported)
        parts = list(msg.walk())
        filenames = [p.get_filename() for p in parts if p.get_filename()]
        assert len(filenames) == 0


# ============================================================================
# LIST DIR WITH DRAFTS / TRASH TESTS
# ============================================================================


class TestListDirWithDraftsTrash:
    """Test that directory listing includes DRAFTS/ and TRASH/."""

    def test_root_listing_includes_drafts_and_trash(self, gmail_backend, operation_context):
        """Test that root listing includes DRAFTS/ and TRASH/ folders."""
        dirs = gmail_backend.list_dir("/", context=operation_context)

        assert "DRAFTS/" in dirs
        assert "TRASH/" in dirs
        assert "INBOX/" in dirs
        assert "SENT/" in dirs

    def test_label_folders_constant(self):
        """Test LABEL_FOLDERS includes DRAFTS and TRASH."""
        assert "DRAFTS" in LABEL_FOLDERS
        assert "TRASH" in LABEL_FOLDERS
        assert "INBOX" in LABEL_FOLDERS
        assert "SENT" in LABEL_FOLDERS

    def test_list_drafts_folder(self, gmail_backend, mock_gmail_service, operation_context):
        """Test listing DRAFTS folder returns draft entries."""
        dirs = gmail_backend.list_dir("DRAFTS", context=operation_context)

        # Should have 2 drafts from mock
        assert len(dirs) == 2
        mock_gmail_service.users().drafts().list.assert_called()

    def test_list_trash_folder(self, gmail_backend, mock_gmail_service, operation_context):
        """Test listing TRASH folder returns trashed entries."""
        dirs = gmail_backend.list_dir("TRASH", context=operation_context)

        # Should have 2 messages from mock (messages.list with TRASH label)
        assert len(dirs) == 2
        mock_gmail_service.users().messages().list.assert_called()


# ============================================================================
# PARSE KEY TESTS (sentinel support)
# ============================================================================


class TestParseKey:
    """Test _parse_key with sentinel filenames."""

    def test_parse_standard_key(self):
        """Test parsing standard message key."""
        label, thread_id, msg_id = GmailTransport._parse_key("INBOX/thread123-msg456.yaml")

        assert label == "INBOX"
        assert thread_id == "thread123"
        assert msg_id == "msg456"

    def test_parse_sentinel_new(self):
        """Test parsing _new sentinel key."""
        label, thread_id, sentinel = GmailTransport._parse_key("SENT/_new.yaml")

        assert label == "SENT"
        assert thread_id is None
        assert sentinel == "_new"

    def test_parse_sentinel_reply(self):
        """Test parsing _reply sentinel key."""
        label, thread_id, sentinel = GmailTransport._parse_key("SENT/_reply.yaml")

        assert label == "SENT"
        assert thread_id is None
        assert sentinel == "_reply"

    def test_parse_sentinel_forward(self):
        """Test parsing _forward sentinel key."""
        label, thread_id, sentinel = GmailTransport._parse_key("SENT/_forward.yaml")

        assert label == "SENT"
        assert thread_id is None
        assert sentinel == "_forward"

    def test_parse_drafts_new(self):
        """Test parsing DRAFTS/_new sentinel key."""
        label, thread_id, sentinel = GmailTransport._parse_key("DRAFTS/_new.yaml")

        assert label == "DRAFTS"
        assert thread_id is None
        assert sentinel == "_new"

    def test_parse_invalid_key(self):
        """Test parsing invalid key returns None tuple."""
        label, thread_id, msg_id = GmailTransport._parse_key("invalid/path/too/deep.yaml")

        assert label is None
        assert thread_id is None
        assert msg_id is None

    def test_parse_inbox_category_legacy(self):
        """``INBOX/<CATEGORY>/thr-msg.yaml`` → nested label + id anchor."""
        label, thread_id, msg_id = GmailTransport._parse_key("INBOX/SOCIAL/thr_abc-msg_def.yaml")
        assert label == "INBOX/SOCIAL"
        assert thread_id == "thr_abc"
        assert msg_id == "msg_def"

    def test_parse_inbox_category_readable(self):
        """Readable form under a category preserves the ``__`` id anchor."""
        label, thread_id, msg_id = GmailTransport._parse_key(
            "INBOX/PRIMARY/2026-04-21_Hello-World__thr1-msg1.yaml"
        )
        assert label == "INBOX/PRIMARY"
        assert thread_id == "thr1"
        assert msg_id == "msg1"

    def test_parse_inbox_unknown_category_rejected(self):
        """Only PRIMARY/SOCIAL/UPDATES/PROMOTIONS/FORUMS are virtual
        subfolders — any other second segment must fall through to
        ``None`` rather than be silently accepted as a label."""
        label, thread_id, msg_id = GmailTransport._parse_key("INBOX/BOGUS/thr-msg.yaml")
        assert (label, thread_id, msg_id) == (None, None, None)


# ============================================================================
# YAML PARSING TESTS
# ============================================================================


class TestYamlParsing:
    """Test _parse_yaml_content with comment extraction."""

    def test_parse_with_comments(self):
        """Test parsing YAML with agent_intent and confirm in comments."""
        content = (
            b"# agent_intent: User wants to send an email update\n"
            b"# confirm: true\n"
            b"to:\n"
            b"  - alice@example.com\n"
            b"subject: Test\n"
            b"body: Hello\n"
        )

        result = GmailTransport._parse_yaml_content(content)

        assert result["agent_intent"] == "User wants to send an email update"
        assert result["confirm"] is True
        assert result["to"] == ["alice@example.com"]
        assert result["subject"] == "Test"

    def test_parse_without_comments(self):
        """Test parsing YAML without comment metadata."""
        content = yaml.dump(
            {
                "agent_intent": "Inline intent field",
                "to": ["bob@example.com"],
                "body": "Content",
            }
        ).encode("utf-8")

        result = GmailTransport._parse_yaml_content(content)

        assert result["agent_intent"] == "Inline intent field"
        assert result["to"] == ["bob@example.com"]


# ============================================================================
# ATTACHMENT SCHEMA TESTS
# ============================================================================


class TestAttachmentSchema:
    """Test Attachment model validation."""

    def test_inline_attachment_valid(self):
        """Test valid inline attachment with data + filename."""
        from nexus.backends.connectors.gmail.schemas import Attachment

        att = Attachment(data="SGVsbG8=", filename="test.txt")
        assert att.data == "SGVsbG8="
        assert att.filename == "test.txt"

    def test_inline_attachment_requires_filename(self):
        """Test that inline data without filename raises."""
        from nexus.backends.connectors.gmail.schemas import Attachment

        with pytest.raises(PydanticValidationError):
            Attachment(data="SGVsbG8=")

    def test_path_attachment_valid(self):
        """Test valid path-based attachment."""
        from nexus.backends.connectors.gmail.schemas import Attachment

        att = Attachment(path="/mnt/storage/file.pdf")
        assert att.path == "/mnt/storage/file.pdf"

    def test_no_source_raises(self):
        """Test that attachment with neither data nor path raises."""
        from nexus.backends.connectors.gmail.schemas import Attachment

        with pytest.raises(PydanticValidationError):
            Attachment(filename="orphan.txt")


# ============================================================================
# BACKEND FEATURES TESTS
# ============================================================================


class TestBackendFeatures:
    """Test backend feature flags."""

    def test_readme_doc_feature(self, gmail_backend):
        """Test that Gmail connector has README_DOC feature."""
        from nexus.contracts.backend_features import BackendFeature

        assert gmail_backend.has_feature(BackendFeature.README_DOC)

    def test_oauth_features(self, gmail_backend):
        """Test that Gmail connector has OAuth features."""
        from nexus.contracts.backend_features import BackendFeature

        assert gmail_backend.has_feature(BackendFeature.USER_SCOPED)
        assert gmail_backend.has_feature(BackendFeature.TOKEN_MANAGER)
        assert gmail_backend.has_feature(BackendFeature.OAUTH)
