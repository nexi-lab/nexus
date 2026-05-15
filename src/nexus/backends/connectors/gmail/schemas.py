"""Pydantic schemas for Gmail operations.

These schemas validate the YAML content that agents write to send emails,
reply to threads, or forward messages.

Based on:
- Gmail API v1: https://developers.google.com/gmail/api/reference/rest
- MCP Server patterns: https://github.com/anthropics/anthropic-cookbook
- RFC 2822 email format

Example email composition:
    ```yaml
    # agent_intent: User requested to send a project update to the team
    to:
      - alice@example.com
      - bob@example.com
    subject: Weekly Project Update
    body: |
      Hi team,

      Here's the weekly update on Project X...

      Best regards
    ```
"""

from typing import Annotated, Literal

from pydantic import BaseModel, Field, model_validator

from nexus.lib.validators import EmailAddress, EmailList, EmailListRequired


class Recipient(BaseModel):
    """Email recipient with optional display name.

    Can be specified as just an email string or with full details.
    """

    email: Annotated[EmailAddress, Field(description="Recipient email address")]
    name: Annotated[str | None, Field(default=None, description="Display name (optional)")]


class Attachment(BaseModel):
    """Email attachment — inline base64 data or filesystem path reference.

    Inline mode (preferred):
        data: base64-encoded file content
        filename: required when using data

    Path mode (requires kernel VFS access — not yet supported):
        path: VFS path to attachment file
    """

    path: Annotated[
        str | None,
        Field(default=None, description="Path to attachment file in mounted filesystem (future)"),
    ]
    data: Annotated[
        str | None,
        Field(default=None, description="Base64-encoded file content (inline attachment)"),
    ]
    filename: Annotated[
        str | None,
        Field(default=None, description="Filename (required for inline, optional for path)"),
    ]
    content_type: Annotated[
        str | None,
        Field(default=None, description="MIME type (auto-detected from filename if not set)"),
    ]

    @model_validator(mode="after")
    def validate_attachment_source(self) -> "Attachment":
        """Ensure either data or path is provided, and filename is set for inline."""
        if not self.data and not self.path:
            raise ValueError("Attachment requires either 'data' (base64) or 'path'")
        if self.data and not self.filename:
            raise ValueError("Attachment with inline 'data' requires 'filename'")
        return self


class SendEmailSchema(BaseModel):
    """Schema for composing and sending a new email.

    Example:
        ```yaml
        # agent_intent: User wants to send meeting notes to the team
        to:
          - alice@example.com
          - bob@example.com
        cc:
          - manager@example.com
        subject: Meeting Notes - Project Review
        body: |
          Hi team,

          Here are the notes from today's meeting...

          Action items:
          1. Alice - Review design docs
          2. Bob - Update timeline

          Best regards
        ```
    """

    agent_intent: Annotated[
        str,
        Field(
            min_length=10,
            description="Explanation of why this email is being sent (required for audit)",
        ),
    ]
    to: Annotated[EmailListRequired, Field(description="List of recipient email addresses")]
    cc: Annotated[EmailList, Field(default=None, description="CC recipients (optional)")]
    bcc: Annotated[EmailList, Field(default=None, description="BCC recipients (optional)")]
    subject: Annotated[
        str,
        Field(min_length=1, max_length=998, description="Email subject line"),
    ]
    body: Annotated[
        str,
        Field(min_length=1, description="Email body (plain text)"),
    ]
    html_body: Annotated[
        str | None,
        Field(default=None, description="HTML body (optional, for rich formatting)"),
    ]
    attachments: Annotated[
        list[Attachment] | None,
        Field(default=None, description="List of attachments (optional)"),
    ]
    priority: Annotated[
        Literal["high", "normal", "low"] | None,
        Field(default=None, description="Email priority header"),
    ]
    confirm: Annotated[
        bool,
        Field(default=False, description="Set to true to confirm sending"),
    ]

    @model_validator(mode="after")
    def validate_confirm_required(self) -> "SendEmailSchema":
        """Ensure confirm=true is set before sending."""
        if not self.confirm:
            raise ValueError(
                "Sending email requires confirm: true. "
                "This ensures the agent has explicit user approval."
            )
        return self


class ReplyEmailSchema(BaseModel):
    """Schema for replying to an existing email thread.

    Example:
        ```yaml
        # agent_intent: User wants to reply to the project update thread
        thread_id: "18c1234567890abc"
        message_id: "18c1234567890xyz"
        body: |
          Thanks for the update!

          I've reviewed the docs and have some feedback...
        reply_all: true
        ```
    """

    agent_intent: Annotated[
        str,
        Field(
            min_length=10,
            description="Explanation of why this reply is being sent",
        ),
    ]
    thread_id: Annotated[
        str,
        Field(description="Gmail thread ID to reply to"),
    ]
    message_id: Annotated[
        str,
        Field(description="Specific message ID to reply to (for threading)"),
    ]
    body: Annotated[
        str,
        Field(min_length=1, description="Reply body (plain text)"),
    ]
    html_body: Annotated[
        str | None,
        Field(default=None, description="HTML reply body (optional)"),
    ]
    reply_all: Annotated[
        bool,
        Field(default=False, description="Reply to all recipients"),
    ]
    additional_to: Annotated[
        EmailList, Field(default=None, description="Additional recipients to add")
    ]
    attachments: Annotated[
        list[Attachment] | None,
        Field(default=None, description="Attachments to include"),
    ]
    confirm: Annotated[
        bool,
        Field(default=False, description="Set to true to confirm sending"),
    ]

    @model_validator(mode="after")
    def validate_confirm_required(self) -> "ReplyEmailSchema":
        """Ensure confirm=true is set before sending."""
        if not self.confirm:
            raise ValueError(
                "Sending reply requires confirm: true. "
                "This ensures the agent has explicit user approval."
            )
        return self


class ForwardEmailSchema(BaseModel):
    """Schema for forwarding an email.

    Example:
        ```yaml
        # agent_intent: User wants to forward the report to external partner
        message_id: "18c1234567890abc"
        to:
          - partner@external.com
        comment: |
          FYI - Here's the report we discussed.
        confirm: true
        ```
    """

    agent_intent: Annotated[
        str,
        Field(
            min_length=10,
            description="Explanation of why this email is being forwarded",
        ),
    ]
    message_id: Annotated[
        str,
        Field(description="Gmail message ID to forward"),
    ]
    to: Annotated[EmailListRequired, Field(description="Forward recipients")]
    cc: Annotated[EmailList, Field(default=None, description="CC recipients")]
    comment: Annotated[
        str | None,
        Field(default=None, description="Comment to add before forwarded content"),
    ]
    include_attachments: Annotated[
        bool,
        Field(default=True, description="Include original attachments"),
    ]
    confirm: Annotated[
        bool,
        Field(default=False, description="Set to true to confirm forwarding"),
    ]

    @model_validator(mode="after")
    def validate_confirm_required(self) -> "ForwardEmailSchema":
        """Ensure confirm=true is set before forwarding."""
        if not self.confirm:
            raise ValueError(
                "Forwarding email requires confirm: true. "
                "This ensures the agent has explicit user approval."
            )
        return self


class DraftEmailSchema(BaseModel):
    """Schema for creating an email draft (doesn't send).

    Drafts don't require confirm: true since they're not sent.

    Example:
        ```yaml
        # agent_intent: User wants to draft a response for later review
        to:
          - client@example.com
        subject: Re: Project Proposal
        body: |
          Thank you for your proposal...
        ```
    """

    agent_intent: Annotated[
        str,
        Field(
            min_length=10,
            description="Explanation of why this draft is being created",
        ),
    ]
    to: Annotated[EmailList, Field(default=None, description="Recipients (optional for drafts)")]
    cc: Annotated[EmailList, Field(default=None, description="CC recipients")]
    subject: Annotated[
        str | None,
        Field(default=None, description="Subject line"),
    ]
    body: Annotated[
        str,
        Field(min_length=1, description="Draft body"),
    ]
    html_body: Annotated[
        str | None,
        Field(default=None, description="HTML body"),
    ]
    thread_id: Annotated[
        str | None,
        Field(default=None, description="Thread ID if this is a reply draft"),
    ]
