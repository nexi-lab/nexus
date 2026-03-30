"""Error definitions for Gmail connector.

Shared trait/checkpoint errors are inherited from ``base_errors``.
Domain-specific errors are defined here.
"""

from nexus.backends.connectors.base import ErrorDef
from nexus.backends.connectors.base_errors import CHECKPOINT_ERRORS, TRAIT_ERRORS

# Domain-specific errors for Gmail operations
_DOMAIN_ERRORS: dict[str, ErrorDef] = {
    "MISSING_RECIPIENTS": ErrorDef(
        message="Email must have at least one recipient in 'to' field",
        readme_section="send-email",
        fix_example="to:\n  - recipient@example.com",
    ),
    "INVALID_EMAIL_ADDRESS": ErrorDef(
        message="One or more email addresses are invalid",
        readme_section="send-email",
        fix_example="to:\n  - valid@example.com  # Use proper email format",
    ),
    "MISSING_SUBJECT": ErrorDef(
        message="Email subject is required",
        readme_section="send-email",
        fix_example="subject: Meeting Follow-up",
    ),
    "MISSING_BODY": ErrorDef(
        message="Email body is required",
        readme_section="send-email",
        fix_example="body: |\n  Hello,\n\n  Here is the content...",
    ),
    "THREAD_NOT_FOUND": ErrorDef(
        message="The specified thread_id does not exist or is not accessible",
        readme_section="reply-email",
        fix_example="thread_id: '18c1234567890abc'  # Use a valid thread ID from email listing",
    ),
    "MESSAGE_NOT_FOUND": ErrorDef(
        message="The specified message_id does not exist or is not accessible",
        readme_section="forward-email",
        fix_example="message_id: '18c1234567890abc'  # Use a valid message ID",
    ),
    "ATTACHMENT_NOT_FOUND": ErrorDef(
        message="Attachment file not found at specified path",
        readme_section="attachments",
        fix_example="attachments:\n  - path: /mnt/storage/report.pdf",
    ),
    "ATTACHMENT_TOO_LARGE": ErrorDef(
        message="Attachment exceeds Gmail's 25MB size limit",
        readme_section="attachments",
        fix_example="# Use Google Drive for larger files",
    ),
    "QUOTA_EXCEEDED": ErrorDef(
        message="Gmail API quota exceeded - please wait before sending more emails",
        readme_section="rate-limits",
        fix_example="# Wait a few minutes and try again",
    ),
    "OAUTH_TOKEN_EXPIRED": ErrorDef(
        message="OAuth token has expired - re-authentication required",
        readme_section="authentication",
        fix_example="# Run: nexus oauth login gmail",
    ),
    "EXTERNAL_RECIPIENT_WARNING": ErrorDef(
        message="Email contains external recipients (outside your organization)",
        readme_section="security",
        fix_example="# This is a warning - ensure external sharing is intended",
    ),
}

# Merged registry: shared trait + checkpoint + domain-specific
ERROR_REGISTRY: dict[str, ErrorDef] = {
    **TRAIT_ERRORS,
    **CHECKPOINT_ERRORS,
    **_DOMAIN_ERRORS,
}
