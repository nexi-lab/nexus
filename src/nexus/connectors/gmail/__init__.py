"""Gmail connector schemas and error definitions.

This module provides Pydantic schemas for Gmail operations:
- SendEmailSchema: For composing and sending new emails
- ReplyEmailSchema: For replying to email threads
- ForwardEmailSchema: For forwarding emails
- DraftEmailSchema: For creating email drafts

Error definitions are used by TraitBasedMixin for helpful error messages.
"""

from nexus.connectors.gmail.errors import ERROR_REGISTRY
from nexus.connectors.gmail.schemas import (
    Attachment,
    DraftEmailSchema,
    ForwardEmailSchema,
    Recipient,
    ReplyEmailSchema,
    SendEmailSchema,
)

__all__ = [
    "Attachment",
    "DraftEmailSchema",
    "ERROR_REGISTRY",
    "ForwardEmailSchema",
    "Recipient",
    "ReplyEmailSchema",
    "SendEmailSchema",
]
