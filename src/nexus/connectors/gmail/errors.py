"""Error definitions for Gmail connector.

These errors are used by TraitBasedMixin to provide helpful
error messages that reference the SKILL.md documentation.
"""

from __future__ import annotations

from nexus.connectors.base import ErrorDef

# Error registry for Gmail operations
# Each error has a message, skill_section (SKILL.md anchor), and optional fix_example
ERROR_REGISTRY: dict[str, ErrorDef] = {
    "MISSING_AGENT_INTENT": ErrorDef(
        message="Missing required 'agent_intent' comment explaining why this action is needed",
        skill_section="required-format",
        fix_example="# agent_intent: User requested to send project update to the team",
    ),
    "AGENT_INTENT_TOO_SHORT": ErrorDef(
        message="agent_intent must be at least 10 characters - provide meaningful context",
        skill_section="required-format",
        fix_example="# agent_intent: Sending weekly status report as requested by user",
    ),
    "MISSING_CONFIRM": ErrorDef(
        message="Sending emails requires explicit confirmation with 'confirm: true'",
        skill_section="send-email",
        fix_example="confirm: true  # Add this to confirm email should be sent",
    ),
    "MISSING_RECIPIENTS": ErrorDef(
        message="Email must have at least one recipient in 'to' field",
        skill_section="send-email",
        fix_example="to:\n  - recipient@example.com",
    ),
    "INVALID_EMAIL_ADDRESS": ErrorDef(
        message="One or more email addresses are invalid",
        skill_section="send-email",
        fix_example="to:\n  - valid@example.com  # Use proper email format",
    ),
    "MISSING_SUBJECT": ErrorDef(
        message="Email subject is required",
        skill_section="send-email",
        fix_example="subject: Meeting Follow-up",
    ),
    "MISSING_BODY": ErrorDef(
        message="Email body is required",
        skill_section="send-email",
        fix_example="body: |\n  Hello,\n\n  Here is the content...",
    ),
    "THREAD_NOT_FOUND": ErrorDef(
        message="The specified thread_id does not exist or is not accessible",
        skill_section="reply-email",
        fix_example="thread_id: '18c1234567890abc'  # Use a valid thread ID from email listing",
    ),
    "MESSAGE_NOT_FOUND": ErrorDef(
        message="The specified message_id does not exist or is not accessible",
        skill_section="forward-email",
        fix_example="message_id: '18c1234567890abc'  # Use a valid message ID",
    ),
    "ATTACHMENT_NOT_FOUND": ErrorDef(
        message="Attachment file not found at specified path",
        skill_section="attachments",
        fix_example="attachments:\n  - path: /mnt/storage/report.pdf",
    ),
    "ATTACHMENT_TOO_LARGE": ErrorDef(
        message="Attachment exceeds Gmail's 25MB size limit",
        skill_section="attachments",
        fix_example="# Use Google Drive for larger files",
    ),
    "QUOTA_EXCEEDED": ErrorDef(
        message="Gmail API quota exceeded - please wait before sending more emails",
        skill_section="rate-limits",
        fix_example="# Wait a few minutes and try again",
    ),
    "OAUTH_TOKEN_EXPIRED": ErrorDef(
        message="OAuth token has expired - re-authentication required",
        skill_section="authentication",
        fix_example="# Run: nexus oauth login gmail",
    ),
    "EXTERNAL_RECIPIENT_WARNING": ErrorDef(
        message="Email contains external recipients (outside your organization)",
        skill_section="security",
        fix_example="# This is a warning - ensure external sharing is intended",
    ),
}
