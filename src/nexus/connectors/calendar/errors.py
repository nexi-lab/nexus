"""Error registry for Google Calendar connector.

Each error definition includes:
- message: Human-readable error description
- skill_section: SKILL.md section anchor for reference
- fix_example: Example of how to fix the error

These are used by SkillDocMixin to generate self-correcting error messages
that help agents fix their requests.
"""

from __future__ import annotations

from nexus.connectors.base import ErrorDef

# Error registry for Calendar connector
# Used by TraitBasedMixin and ValidatedMixin for error formatting
ERROR_REGISTRY: dict[str, ErrorDef] = {
    # ==========================================================================
    # Trait Validation Errors
    # ==========================================================================
    "MISSING_AGENT_INTENT": ErrorDef(
        message="Calendar operations require agent_intent explaining why you're performing this action",
        skill_section="required-format",
        fix_example="# agent_intent: User requested to schedule a team meeting for Monday",
    ),
    "AGENT_INTENT_TOO_SHORT": ErrorDef(
        message="agent_intent must be at least 10 characters to provide meaningful context",
        skill_section="required-format",
        fix_example="# agent_intent: User asked to create weekly standup meeting with the team",
    ),
    "MISSING_CONFIRM": ErrorDef(
        message="Delete operations require explicit confirmation",
        skill_section="delete-operation",
        fix_example="# agent_intent: User wants to cancel the meeting\n# confirm: true",
    ),
    "MISSING_USER_CONFIRMATION": ErrorDef(
        message="This operation requires explicit user confirmation before proceeding",
        skill_section="irreversible-operations",
        fix_example="# agent_intent: <reason>\n# user_confirmed: true  # Only after user explicitly approves",
    ),
    # ==========================================================================
    # Schema Validation Errors
    # ==========================================================================
    "INVALID_DATETIME_FORMAT": ErrorDef(
        message="Invalid datetime format. Use ISO 8601 with timezone offset",
        skill_section="time-format",
        fix_example='start:\n  dateTime: "2024-01-15T09:00:00-08:00"\n  timeZone: America/Los_Angeles',
    ),
    "MISSING_REQUIRED_FIELD": ErrorDef(
        message="Missing required field for this operation",
        skill_section="create-event",
        fix_example="summary: Meeting Title\nstart:\n  dateTime: ...\nend:\n  dateTime: ...",
    ),
    "INVALID_TIMEZONE": ErrorDef(
        message="Invalid timezone. Use IANA timezone format",
        skill_section="time-format",
        fix_example="timeZone: America/Los_Angeles  # or Europe/London, Asia/Tokyo, etc.",
    ),
    "INVALID_RECURRENCE_RULE": ErrorDef(
        message="Invalid recurrence rule. Use RFC 5545 RRULE format",
        skill_section="recurrence",
        fix_example='recurrence:\n  - "RRULE:FREQ=WEEKLY;BYDAY=MO,WE,FR"',
    ),
    "INVALID_VISIBILITY": ErrorDef(
        message="Invalid visibility value",
        skill_section="create-event",
        fix_example="visibility: private  # Options: default, public, private, confidential",
    ),
    "INVALID_ATTENDEE_EMAIL": ErrorDef(
        message="Invalid attendee email address",
        skill_section="create-event",
        fix_example="attendees:\n  - email: user@example.com\n    displayName: User Name",
    ),
    "END_BEFORE_START": ErrorDef(
        message="Event end time must be after start time",
        skill_section="time-format",
        fix_example="start:\n  dateTime: 2024-01-15T09:00:00-08:00\nend:\n  dateTime: 2024-01-15T10:00:00-08:00  # Must be after start",
    ),
    # ==========================================================================
    # Operation Errors
    # ==========================================================================
    "EVENT_NOT_FOUND": ErrorDef(
        message="Event not found. It may have been deleted or you may not have access",
        skill_section="operations",
        fix_example="# List events first to get valid event IDs:\n# nexus ls /mnt/calendar/primary/",
    ),
    "CALENDAR_NOT_FOUND": ErrorDef(
        message="Calendar not found. Check the calendar ID",
        skill_section="mount-path",
        fix_example="# Use 'primary' for the user's main calendar:\n# /mnt/calendar/primary/",
    ),
    "PERMISSION_DENIED": ErrorDef(
        message="You don't have permission to modify this event",
        skill_section="operations",
        fix_example="# You can only modify events you own or have edit access to",
    ),
    "QUOTA_EXCEEDED": ErrorDef(
        message="Google Calendar API quota exceeded. Try again later",
        skill_section="operations",
        fix_example="# Wait a few minutes before retrying",
    ),
    # ==========================================================================
    # Checkpoint Errors
    # ==========================================================================
    "CHECKPOINT_NOT_FOUND": ErrorDef(
        message="Checkpoint not found. It may have expired or been cleared",
        skill_section="rollback",
        fix_example="# Checkpoints expire after the operation completes successfully",
    ),
    "ROLLBACK_NOT_POSSIBLE": ErrorDef(
        message="Cannot rollback this operation",
        skill_section="rollback",
        fix_example="# Some operations (like notifications sent) cannot be undone",
    ),
}


def get_error(code: str) -> ErrorDef | None:
    """Get error definition by code.

    Args:
        code: Error code (e.g., "MISSING_AGENT_INTENT")

    Returns:
        ErrorDef if found, None otherwise
    """
    return ERROR_REGISTRY.get(code)


def format_error_message(
    code: str,
    skill_path: str = "/skill/services/gcalendar/SKILL.md",
    **context: str,
) -> str:
    """Format error message with skill reference.

    Args:
        code: Error code
        skill_path: Path to SKILL.md
        **context: Additional context to include in message

    Returns:
        Formatted error message with skill reference and fix example
    """
    error_def = ERROR_REGISTRY.get(code)
    if not error_def:
        return f"[{code}] Unknown error"

    lines = [f"[{code}] {error_def.message}"]

    # Add context
    for key, value in context.items():
        lines.append(f"  {key}: {value}")

    # Add skill reference
    lines.append(f"\nSee: {skill_path}#{error_def.skill_section}")

    # Add fix example
    if error_def.fix_example:
        lines.append(f"\nFix:\n```yaml\n{error_def.fix_example}\n```")

    return "\n".join(lines)
