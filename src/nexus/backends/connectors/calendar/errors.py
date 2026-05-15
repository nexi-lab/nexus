"""Error definitions for Google Calendar connector.

Shared trait/checkpoint errors are inherited from ``base_errors``.
Domain-specific errors are defined here.
"""

from nexus.backends.connectors.base import ErrorDef
from nexus.backends.connectors.base_errors import CHECKPOINT_ERRORS, TRAIT_ERRORS

# Domain-specific errors for Calendar operations
_DOMAIN_ERRORS: dict[str, ErrorDef] = {
    # Schema validation errors
    "INVALID_DATETIME_FORMAT": ErrorDef(
        message="Invalid datetime format. Use ISO 8601 with timezone offset",
        readme_section="time-format",
        fix_example='start:\n  dateTime: "2024-01-15T09:00:00-08:00"\n  timeZone: America/Los_Angeles',
    ),
    "MISSING_REQUIRED_FIELD": ErrorDef(
        message="Missing required field for this operation",
        readme_section="create-event",
        fix_example="summary: Meeting Title\nstart:\n  dateTime: ...\nend:\n  dateTime: ...",
    ),
    "INVALID_TIMEZONE": ErrorDef(
        message="Invalid timezone. Use IANA timezone format",
        readme_section="time-format",
        fix_example="timeZone: America/Los_Angeles  # or Europe/London, Asia/Tokyo, etc.",
    ),
    "INVALID_RECURRENCE_RULE": ErrorDef(
        message="Invalid recurrence rule. Use RFC 5545 RRULE format",
        readme_section="recurrence",
        fix_example='recurrence:\n  - "RRULE:FREQ=WEEKLY;BYDAY=MO,WE,FR"',
    ),
    "INVALID_VISIBILITY": ErrorDef(
        message="Invalid visibility value",
        readme_section="create-event",
        fix_example="visibility: private  # Options: default, public, private, confidential",
    ),
    "INVALID_ATTENDEE_EMAIL": ErrorDef(
        message="Invalid attendee email address",
        readme_section="create-event",
        fix_example="attendees:\n  - email: user@example.com\n    displayName: User Name",
    ),
    "END_BEFORE_START": ErrorDef(
        message="Event end time must be after start time",
        readme_section="time-format",
        fix_example="start:\n  dateTime: 2024-01-15T09:00:00-08:00\nend:\n  dateTime: 2024-01-15T10:00:00-08:00  # Must be after start",
    ),
    # Operation errors
    "EVENT_NOT_FOUND": ErrorDef(
        message="Event not found. It may have been deleted or you may not have access",
        readme_section="operations",
        fix_example="# List events first to get valid event IDs:\n# nexus ls /mnt/calendar/primary/",
    ),
    "CALENDAR_NOT_FOUND": ErrorDef(
        message="Calendar not found. Check the calendar ID",
        readme_section="mount-path",
        fix_example="# Use 'primary' for the user's main calendar:\n# /mnt/calendar/primary/",
    ),
    "PERMISSION_DENIED": ErrorDef(
        message="You don't have permission to modify this event",
        readme_section="operations",
        fix_example="# You can only modify events you own or have edit access to",
    ),
    "QUOTA_EXCEEDED": ErrorDef(
        message="Google Calendar API quota exceeded. Try again later",
        readme_section="operations",
        fix_example="# Wait a few minutes before retrying",
    ),
}

# Merged registry: shared trait + checkpoint + domain-specific
ERROR_REGISTRY: dict[str, ErrorDef] = {
    **TRAIT_ERRORS,
    **CHECKPOINT_ERRORS,
    **_DOMAIN_ERRORS,
}
