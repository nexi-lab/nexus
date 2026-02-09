"""Integration tests for Gmail connector.

Tests the Gmail connector end-to-end including:
- Schema validation
- Trait-based validation
- Error formatting with SKILL.md references
- SKILL.md auto-generation from static file
- YAML parsing

Note: These tests mock the Gmail API since we can't
use real OAuth tokens in CI. For full E2E testing with real
Google API, use OAuth authentication via the integrations page.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError as PydanticValidationError

from nexus.backends.local import LocalBackend
from nexus.connectors.base import ValidationError
from nexus.connectors.gmail.schemas import (
    DraftEmailSchema,
    ReplyEmailSchema,
    SendEmailSchema,
)
from nexus.core.nexus_fs import NexusFS
from nexus.core.permissions import OperationContext
from nexus.factory import create_nexus_fs
from nexus.storage.record_store import SQLAlchemyRecordStore
from nexus.storage.sqlalchemy_metadata_store import SQLAlchemyMetadataStore

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

    # Mock messages().get()
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

    # Mock drafts().create()
    service.users().drafts().create().execute.return_value = {
        "id": "draft_123",
        "message": {"id": "msg_draft_123", "threadId": "thread_draft"},
    }

    return service


@pytest.fixture
def gmail_backend(mock_gmail_service, tmp_path):
    """Create a Gmail backend with mocked Google service."""
    from nexus.backends.gmail_connector import GmailConnectorBackend

    # Create a mock token manager
    with patch("nexus.backends.gmail_connector.GmailConnectorBackend._register_oauth_provider"):
        backend = GmailConnectorBackend(
            token_manager_db=str(tmp_path / "tokens.db"),
            user_email="test@example.com",
        )

    # Replace _get_gmail_service to return our mock
    backend._get_gmail_service = MagicMock(return_value=mock_gmail_service)

    return backend


@pytest.fixture
def operation_context():
    """Create an operation context for testing."""
    return OperationContext(
        user="test@example.com",
        groups=[],
        user_id="test@example.com",
        zone_id="default",
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
        assert "SKILL.md" in str(exc_info.value)

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


class TestSkillDocGeneration:
    """Test SKILL.md loading from static file."""

    def test_generate_skill_doc(self, gmail_backend):
        """Test that SKILL.md is loaded correctly from static file."""
        doc = gmail_backend.generate_skill_doc("/mnt/gmail/")

        # Check header - static SKILL.md
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

    def test_skill_doc_includes_examples(self, gmail_backend):
        """Test that SKILL.md includes YAML examples."""
        doc = gmail_backend.generate_skill_doc("/mnt/gmail/")

        # Should include YAML code blocks
        assert "```yaml" in doc
        assert "# agent_intent:" in doc

    def test_skill_doc_mount_path_replacement(self, gmail_backend):
        """Test that mount path is correctly replaced."""
        doc = gmail_backend.generate_skill_doc("/custom/mount/path/")

        # Default path should be replaced
        assert "/mnt/gmail/" not in doc
        assert "/custom/mount/path/" in doc

    def test_write_skill_doc(self, gmail_backend, isolated_db, tmp_path):
        """Test writing SKILL.md to filesystem."""
        # Create a real NexusFS for writing
        backend = LocalBackend(root_path=str(tmp_path / "storage"))
        nx = create_nexus_fs(
            backend=backend,
            metadata_store=SQLAlchemyMetadataStore(db_path=str(isolated_db)),
            record_store=SQLAlchemyRecordStore(db_path=str(isolated_db)),
            enforce_permissions=False,
        )

        try:
            # Write SKILL.md
            skill_path = gmail_backend.write_skill_doc("/mnt/gmail/", filesystem=nx)

            if skill_path:
                # Read back and verify
                content = nx.read(skill_path)
                assert b"Gmail Connector" in content
                assert b"agent_intent" in content
                assert b"Send Email" in content
        finally:
            nx.close()


# ============================================================================
# ERROR FORMATTING TESTS
# ============================================================================


class TestErrorFormatting:
    """Test error message formatting with SKILL.md references."""

    def test_error_includes_skill_path(self, gmail_backend):
        """Test that errors include SKILL.md path."""
        # Set mount path so skill_md_path is computed correctly
        gmail_backend.set_mount_path("/mnt/gmail")

        error = gmail_backend.format_error_with_skill_ref(
            code="MISSING_AGENT_INTENT",
            message="Missing required field",
        )

        assert "/mnt/gmail/.skill/SKILL.md" in str(error)

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
        from nexus.connectors.base import ConfirmLevel, Reversibility

        traits = gmail_backend.get_operation_traits("send_email")

        assert traits is not None
        assert traits.reversibility == Reversibility.NONE  # Cannot unsend
        assert traits.confirm == ConfirmLevel.EXPLICIT  # Requires confirm
        assert traits.checkpoint is True

    def test_reply_email_traits(self, gmail_backend):
        """Test reply_email has correct traits."""
        from nexus.connectors.base import ConfirmLevel, Reversibility

        traits = gmail_backend.get_operation_traits("reply_email")

        assert traits is not None
        assert traits.reversibility == Reversibility.NONE
        assert traits.confirm == ConfirmLevel.EXPLICIT

    def test_forward_email_traits(self, gmail_backend):
        """Test forward_email has correct traits."""
        from nexus.connectors.base import ConfirmLevel, Reversibility

        traits = gmail_backend.get_operation_traits("forward_email")

        assert traits is not None
        assert traits.reversibility == Reversibility.NONE
        assert traits.confirm == ConfirmLevel.EXPLICIT

    def test_draft_traits(self, gmail_backend):
        """Test create_draft has correct traits."""
        from nexus.connectors.base import ConfirmLevel, Reversibility

        traits = gmail_backend.get_operation_traits("create_draft")

        assert traits is not None
        assert traits.reversibility == Reversibility.FULL  # Can delete draft
        assert traits.confirm == ConfirmLevel.INTENT  # Only needs intent
