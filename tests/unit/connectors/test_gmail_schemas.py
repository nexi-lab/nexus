"""Tests for Gmail connector schemas.

Tests Pydantic schema validation for:
- SendEmailSchema
- ReplyEmailSchema
- ForwardEmailSchema
- DraftEmailSchema
- Attachment
- Recipient
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError as PydanticValidationError

from nexus.connectors.gmail.schemas import (
    Attachment,
    DraftEmailSchema,
    ForwardEmailSchema,
    Recipient,
    ReplyEmailSchema,
    SendEmailSchema,
)

# =============================================================================
# Recipient Tests
# =============================================================================


class TestRecipient:
    """Tests for Recipient schema."""

    def test_valid_email(self):
        """Test valid email address."""
        recipient = Recipient(email="user@example.com")

        assert recipient.email == "user@example.com"
        assert recipient.name is None

    def test_email_normalized_lowercase(self):
        """Test that email is normalized to lowercase."""
        recipient = Recipient(email="User@Example.COM")

        assert recipient.email == "user@example.com"

    def test_email_with_display_name(self):
        """Test email with display name."""
        recipient = Recipient(email="user@example.com", name="Test User")

        assert recipient.email == "user@example.com"
        assert recipient.name == "Test User"

    def test_invalid_email_format(self):
        """Test that invalid email format is rejected."""
        with pytest.raises(PydanticValidationError) as exc_info:
            Recipient(email="not-an-email")

        errors = exc_info.value.errors()
        assert any("email" in str(e["loc"]) for e in errors)

    def test_invalid_email_missing_domain(self):
        """Test that email without domain is rejected."""
        with pytest.raises(PydanticValidationError):
            Recipient(email="user@")

    def test_invalid_email_missing_local(self):
        """Test that email without local part is rejected."""
        with pytest.raises(PydanticValidationError):
            Recipient(email="@example.com")


# =============================================================================
# Attachment Tests
# =============================================================================


class TestAttachment:
    """Tests for Attachment schema."""

    def test_minimal_attachment(self):
        """Test attachment with only path."""
        attachment = Attachment(path="/mnt/storage/report.pdf")

        assert attachment.path == "/mnt/storage/report.pdf"
        assert attachment.filename is None
        assert attachment.content_type is None

    def test_full_attachment(self):
        """Test attachment with all fields."""
        attachment = Attachment(
            path="/mnt/storage/report.pdf",
            filename="quarterly-report.pdf",
            content_type="application/pdf",
        )

        assert attachment.path == "/mnt/storage/report.pdf"
        assert attachment.filename == "quarterly-report.pdf"
        assert attachment.content_type == "application/pdf"


# =============================================================================
# SendEmailSchema Tests
# =============================================================================


class TestSendEmailSchema:
    """Tests for SendEmailSchema."""

    def test_valid_email(self):
        """Test valid email with all required fields."""
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

    def test_emails_normalized_lowercase(self):
        """Test that email addresses are normalized to lowercase."""
        email = SendEmailSchema(
            agent_intent="User requested to send email",
            to=["Alice@Example.COM"],
            subject="Test",
            body="Body",
            confirm=True,
        )

        assert email.to == ["alice@example.com"]

    def test_with_cc_and_bcc(self):
        """Test email with CC and BCC recipients."""
        email = SendEmailSchema(
            agent_intent="User requested to send email with CC",
            to=["alice@example.com"],
            cc=["manager@example.com"],
            bcc=["archive@example.com"],
            subject="Test",
            body="Body",
            confirm=True,
        )

        assert email.cc == ["manager@example.com"]
        assert email.bcc == ["archive@example.com"]

    def test_with_attachments(self):
        """Test email with attachments."""
        email = SendEmailSchema(
            agent_intent="User requested to send email with attachment",
            to=["alice@example.com"],
            subject="Report Attached",
            body="Please find the report attached.",
            attachments=[
                Attachment(path="/mnt/storage/report.pdf"),
            ],
            confirm=True,
        )

        assert len(email.attachments) == 1
        assert email.attachments[0].path == "/mnt/storage/report.pdf"

    def test_with_priority(self):
        """Test email with priority header."""
        email = SendEmailSchema(
            agent_intent="User requested high priority email",
            to=["alice@example.com"],
            subject="Urgent",
            body="This is urgent.",
            priority="high",
            confirm=True,
        )

        assert email.priority == "high"

    def test_agent_intent_required(self):
        """Test that agent_intent is required."""
        with pytest.raises(PydanticValidationError) as exc_info:
            SendEmailSchema(
                to=["alice@example.com"],
                subject="Test",
                body="Body",
                confirm=True,
            )

        errors = exc_info.value.errors()
        assert any("agent_intent" in str(e["loc"]) for e in errors)

    def test_agent_intent_min_length(self):
        """Test that agent_intent must be at least 10 characters."""
        with pytest.raises(PydanticValidationError) as exc_info:
            SendEmailSchema(
                agent_intent="short",  # Less than 10 chars
                to=["alice@example.com"],
                subject="Test",
                body="Body",
                confirm=True,
            )

        errors = exc_info.value.errors()
        assert any("agent_intent" in str(e["loc"]) for e in errors)

    def test_to_required(self):
        """Test that 'to' recipients are required."""
        with pytest.raises(PydanticValidationError) as exc_info:
            SendEmailSchema(
                agent_intent="User requested to send email",
                subject="Test",
                body="Body",
                confirm=True,
            )

        errors = exc_info.value.errors()
        assert any("to" in str(e["loc"]) for e in errors)

    def test_to_not_empty(self):
        """Test that 'to' cannot be empty."""
        with pytest.raises(PydanticValidationError):
            SendEmailSchema(
                agent_intent="User requested to send email",
                to=[],  # Empty list
                subject="Test",
                body="Body",
                confirm=True,
            )

    def test_subject_required(self):
        """Test that subject is required."""
        with pytest.raises(PydanticValidationError) as exc_info:
            SendEmailSchema(
                agent_intent="User requested to send email",
                to=["alice@example.com"],
                body="Body",
                confirm=True,
            )

        errors = exc_info.value.errors()
        assert any("subject" in str(e["loc"]) for e in errors)

    def test_subject_not_empty(self):
        """Test that subject cannot be empty."""
        with pytest.raises(PydanticValidationError):
            SendEmailSchema(
                agent_intent="User requested to send email",
                to=["alice@example.com"],
                subject="",  # Empty
                body="Body",
                confirm=True,
            )

    def test_body_required(self):
        """Test that body is required."""
        with pytest.raises(PydanticValidationError) as exc_info:
            SendEmailSchema(
                agent_intent="User requested to send email",
                to=["alice@example.com"],
                subject="Test",
                confirm=True,
            )

        errors = exc_info.value.errors()
        assert any("body" in str(e["loc"]) for e in errors)

    def test_confirm_required(self):
        """Test that confirm: true is required for sending."""
        with pytest.raises(PydanticValidationError) as exc_info:
            SendEmailSchema(
                agent_intent="User requested to send email",
                to=["alice@example.com"],
                subject="Test",
                body="Body",
                # Missing confirm: true
            )

        errors = exc_info.value.errors()
        # The model validator raises an error about confirm
        assert any("confirm" in str(e) for e in errors)

    def test_invalid_email_address(self):
        """Test that invalid email addresses are rejected."""
        with pytest.raises(PydanticValidationError):
            SendEmailSchema(
                agent_intent="User requested to send email",
                to=["not-an-email"],
                subject="Test",
                body="Body",
                confirm=True,
            )


# =============================================================================
# ReplyEmailSchema Tests
# =============================================================================


class TestReplyEmailSchema:
    """Tests for ReplyEmailSchema."""

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
        assert reply.message_id == "18c1234567890xyz"
        assert reply.reply_all is False  # Default

    def test_reply_all(self):
        """Test reply to all recipients."""
        reply = ReplyEmailSchema(
            agent_intent="User wants to reply all",
            thread_id="18c1234567890abc",
            message_id="18c1234567890xyz",
            body="Reply body",
            reply_all=True,
            confirm=True,
        )

        assert reply.reply_all is True

    def test_with_additional_recipients(self):
        """Test reply with additional recipients."""
        reply = ReplyEmailSchema(
            agent_intent="User wants to add recipients",
            thread_id="18c1234567890abc",
            message_id="18c1234567890xyz",
            body="Reply body",
            additional_to=["extra@example.com"],
            confirm=True,
        )

        assert reply.additional_to == ["extra@example.com"]

    def test_thread_id_required(self):
        """Test that thread_id is required."""
        with pytest.raises(PydanticValidationError) as exc_info:
            ReplyEmailSchema(
                agent_intent="User wants to reply",
                message_id="18c1234567890xyz",
                body="Reply body",
                confirm=True,
            )

        errors = exc_info.value.errors()
        assert any("thread_id" in str(e["loc"]) for e in errors)

    def test_message_id_required(self):
        """Test that message_id is required."""
        with pytest.raises(PydanticValidationError) as exc_info:
            ReplyEmailSchema(
                agent_intent="User wants to reply",
                thread_id="18c1234567890abc",
                body="Reply body",
                confirm=True,
            )

        errors = exc_info.value.errors()
        assert any("message_id" in str(e["loc"]) for e in errors)

    def test_confirm_required(self):
        """Test that confirm: true is required for reply."""
        with pytest.raises(PydanticValidationError):
            ReplyEmailSchema(
                agent_intent="User wants to reply",
                thread_id="18c1234567890abc",
                message_id="18c1234567890xyz",
                body="Reply body",
                # Missing confirm: true
            )


# =============================================================================
# ForwardEmailSchema Tests
# =============================================================================


class TestForwardEmailSchema:
    """Tests for ForwardEmailSchema."""

    def test_valid_forward(self):
        """Test valid forward with all required fields."""
        forward = ForwardEmailSchema(
            agent_intent="User wants to forward the report to partner",
            message_id="18c1234567890abc",
            to=["partner@external.com"],
            confirm=True,
        )

        assert forward.message_id == "18c1234567890abc"
        assert forward.to == ["partner@external.com"]
        assert forward.include_attachments is True  # Default

    def test_with_comment(self):
        """Test forward with comment."""
        forward = ForwardEmailSchema(
            agent_intent="User wants to forward with comment",
            message_id="18c1234567890abc",
            to=["partner@external.com"],
            comment="FYI - Here's the report we discussed.",
            confirm=True,
        )

        assert forward.comment == "FYI - Here's the report we discussed."

    def test_without_attachments(self):
        """Test forward without including attachments."""
        forward = ForwardEmailSchema(
            agent_intent="User wants to forward text only",
            message_id="18c1234567890abc",
            to=["partner@external.com"],
            include_attachments=False,
            confirm=True,
        )

        assert forward.include_attachments is False

    def test_with_cc(self):
        """Test forward with CC recipients."""
        forward = ForwardEmailSchema(
            agent_intent="User wants to forward with CC",
            message_id="18c1234567890abc",
            to=["partner@external.com"],
            cc=["manager@example.com"],
            confirm=True,
        )

        assert forward.cc == ["manager@example.com"]

    def test_message_id_required(self):
        """Test that message_id is required."""
        with pytest.raises(PydanticValidationError) as exc_info:
            ForwardEmailSchema(
                agent_intent="User wants to forward email",
                to=["partner@external.com"],
                confirm=True,
            )

        errors = exc_info.value.errors()
        assert any("message_id" in str(e["loc"]) for e in errors)

    def test_to_required(self):
        """Test that 'to' recipients are required."""
        with pytest.raises(PydanticValidationError) as exc_info:
            ForwardEmailSchema(
                agent_intent="User wants to forward email",
                message_id="18c1234567890abc",
                confirm=True,
            )

        errors = exc_info.value.errors()
        assert any("to" in str(e["loc"]) for e in errors)

    def test_confirm_required(self):
        """Test that confirm: true is required for forward."""
        with pytest.raises(PydanticValidationError):
            ForwardEmailSchema(
                agent_intent="User wants to forward email",
                message_id="18c1234567890abc",
                to=["partner@external.com"],
                # Missing confirm: true
            )


# =============================================================================
# DraftEmailSchema Tests
# =============================================================================


class TestDraftEmailSchema:
    """Tests for DraftEmailSchema."""

    def test_minimal_draft(self):
        """Test draft with minimal fields (body only)."""
        draft = DraftEmailSchema(
            agent_intent="User wants to draft a response for later",
            body="Draft content to be completed...",
        )

        assert draft.body == "Draft content to be completed..."
        assert draft.to is None
        assert draft.subject is None

    def test_full_draft(self):
        """Test draft with all fields."""
        draft = DraftEmailSchema(
            agent_intent="User wants to draft a reply",
            to=["client@example.com"],
            cc=["team@example.com"],
            subject="Re: Project Proposal",
            body="Thank you for your proposal...",
            html_body="<p>Thank you for your proposal...</p>",
            thread_id="18c1234567890abc",
        )

        assert draft.to == ["client@example.com"]
        assert draft.subject == "Re: Project Proposal"
        assert draft.thread_id == "18c1234567890abc"

    def test_draft_no_confirm_required(self):
        """Test that drafts don't require confirm: true."""
        # This should not raise - drafts don't need confirmation
        draft = DraftEmailSchema(
            agent_intent="User wants to create a draft",
            body="Draft content",
        )

        assert draft.body == "Draft content"

    def test_agent_intent_required(self):
        """Test that agent_intent is still required for drafts."""
        with pytest.raises(PydanticValidationError) as exc_info:
            DraftEmailSchema(
                body="Draft content",
            )

        errors = exc_info.value.errors()
        assert any("agent_intent" in str(e["loc"]) for e in errors)

    def test_body_required(self):
        """Test that body is required."""
        with pytest.raises(PydanticValidationError) as exc_info:
            DraftEmailSchema(
                agent_intent="User wants to create a draft",
            )

        errors = exc_info.value.errors()
        assert any("body" in str(e["loc"]) for e in errors)

    def test_invalid_email_in_to(self):
        """Test that invalid email addresses in 'to' are rejected."""
        with pytest.raises(PydanticValidationError):
            DraftEmailSchema(
                agent_intent="User wants to create a draft",
                to=["not-an-email"],
                body="Draft content",
            )
