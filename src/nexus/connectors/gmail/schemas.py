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

from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

# Email address pattern (simplified RFC 5322)
EMAIL_PATTERN = re.compile(r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$")


class Recipient(BaseModel):
    """Email recipient with optional display name.

    Can be specified as just an email string or with full details.
    """

    email: Annotated[str, Field(description="Recipient email address")]
    name: Annotated[str | None, Field(default=None, description="Display name (optional)")]

    @field_validator("email")
    @classmethod
    def validate_email(cls, v: str) -> str:
        """Validate email format."""
        if not EMAIL_PATTERN.match(v):
            raise ValueError(f"Invalid email address format: {v}")
        return v.lower()


class Attachment(BaseModel):
    """Email attachment reference.

    For sending, attachments can reference files from the mounted filesystem.
    """

    path: Annotated[str, Field(description="Path to attachment file in mounted filesystem")]
    filename: Annotated[
        str | None, Field(default=None, description="Override filename (uses basename if not set)")
    ]
    content_type: Annotated[
        str | None,
        Field(default=None, description="MIME type (auto-detected if not set)"),
    ]


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
    to: Annotated[
        list[str],
        Field(min_length=1, description="List of recipient email addresses"),
    ]
    cc: Annotated[
        list[str] | None,
        Field(default=None, description="CC recipients (optional)"),
    ]
    bcc: Annotated[
        list[str] | None,
        Field(default=None, description="BCC recipients (optional)"),
    ]
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

    @field_validator("to", "cc", "bcc", mode="before")
    @classmethod
    def validate_email_list(cls, v: list[str] | None) -> list[str] | None:
        """Validate all emails in list."""
        if v is None:
            return None
        validated = []
        for email in v:
            if not EMAIL_PATTERN.match(email):
                raise ValueError(f"Invalid email address: {email}")
            validated.append(email.lower())
        return validated

    @model_validator(mode="after")
    def validate_confirm_required(self) -> SendEmailSchema:
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
        list[str] | None,
        Field(default=None, description="Additional recipients to add"),
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
    def validate_confirm_required(self) -> ReplyEmailSchema:
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
    to: Annotated[
        list[str],
        Field(min_length=1, description="Forward recipients"),
    ]
    cc: Annotated[
        list[str] | None,
        Field(default=None, description="CC recipients"),
    ]
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

    @field_validator("to", "cc", mode="before")
    @classmethod
    def validate_email_list(cls, v: list[str] | None) -> list[str] | None:
        """Validate all emails in list."""
        if v is None:
            return None
        validated = []
        for email in v:
            if not EMAIL_PATTERN.match(email):
                raise ValueError(f"Invalid email address: {email}")
            validated.append(email.lower())
        return validated

    @model_validator(mode="after")
    def validate_confirm_required(self) -> ForwardEmailSchema:
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
    to: Annotated[
        list[str] | None,
        Field(default=None, description="Recipients (optional for drafts)"),
    ]
    cc: Annotated[
        list[str] | None,
        Field(default=None, description="CC recipients"),
    ]
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

    @field_validator("to", "cc", mode="before")
    @classmethod
    def validate_email_list(cls, v: list[str] | None) -> list[str] | None:
        """Validate all emails in list."""
        if v is None:
            return None
        validated = []
        for email in v:
            if not EMAIL_PATTERN.match(email):
                raise ValueError(f"Invalid email address: {email}")
            validated.append(email.lower())
        return validated
