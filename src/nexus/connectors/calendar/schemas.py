"""Pydantic schemas for Google Calendar operations.

These schemas validate the YAML content that agents write to create,
update, or delete calendar events.

Based on:
- Google Calendar API v3: https://developers.google.com/calendar/api/v3/reference
- MCP Server patterns: https://github.com/nspady/google-calendar-mcp
- RFC3339 datetime format
- RFC5545 recurrence rules

Example event creation:
    ```yaml
    # agent_intent: User requested team standup meeting
    summary: Daily Standup
    start:
      dateTime: "2024-01-15T09:00:00-08:00"
      timeZone: America/Los_Angeles
    end:
      dateTime: "2024-01-15T09:30:00-08:00"
      timeZone: America/Los_Angeles
    attendees:
      - email: alice@example.com
      - email: bob@example.com
    ```
"""

from __future__ import annotations

import re
from typing import Annotated

from pydantic import BaseModel, Field, field_validator

# ISO 8601 datetime pattern with timezone offset
# Examples: 2024-01-15T09:00:00-08:00, 2024-01-15T09:00:00Z, 2024-01-15T09:00:00+05:30
ISO8601_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}([+-]\d{2}:\d{2}|Z)$")


class TimeSlot(BaseModel):
    """Time slot with datetime and timezone.

    The dateTime must be in ISO 8601 format with timezone offset.
    Google Calendar API requires this format.
    """

    dateTime: Annotated[
        str,
        Field(
            description="ISO 8601 datetime with timezone offset (e.g., 2024-01-15T09:00:00-08:00)"
        ),
    ]
    timeZone: Annotated[
        str,
        Field(
            default="UTC",
            description="IANA timezone (e.g., America/Los_Angeles, Europe/London)",
        ),
    ]

    @field_validator("dateTime")
    @classmethod
    def validate_datetime_format(cls, v: str) -> str:
        """Validate ISO 8601 format with timezone."""
        if not ISO8601_PATTERN.match(v):
            raise ValueError(
                f"Invalid datetime format: {v}. "
                "Use ISO 8601 with timezone offset (e.g., 2024-01-15T09:00:00-08:00)"
            )
        return v


class Attendee(BaseModel):
    """Event attendee."""

    email: Annotated[str, Field(description="Attendee email address")]
    displayName: Annotated[str | None, Field(default=None, description="Display name (optional)")]
    optional: Annotated[bool, Field(default=False, description="Whether attendance is optional")]
    responseStatus: Annotated[
        str | None,
        Field(
            default=None,
            description="Response status: needsAction, declined, tentative, accepted",
        ),
    ]


class Reminder(BaseModel):
    """Event reminder."""

    method: Annotated[str, Field(description="Reminder method: email or popup")]
    minutes: Annotated[int, Field(ge=0, le=40320, description="Minutes before event (0-40320)")]


class Recurrence(BaseModel):
    """Recurrence rule (RFC 5545 RRULE format)."""

    rule: Annotated[
        str,
        Field(description="RRULE string (e.g., RRULE:FREQ=WEEKLY;BYDAY=MO,WE,FR)"),
    ]


class CreateEventSchema(BaseModel):
    """Schema for creating a new calendar event.

    Required fields:
    - agent_intent: Why the agent is creating this event
    - summary: Event title
    - start: Start time
    - end: End time

    Example:
        ```yaml
        # agent_intent: User requested weekly team sync
        summary: Weekly Team Sync
        start:
          dateTime: "2024-01-15T10:00:00-08:00"
          timeZone: America/Los_Angeles
        end:
          dateTime: "2024-01-15T11:00:00-08:00"
          timeZone: America/Los_Angeles
        description: Weekly sync to discuss project progress
        attendees:
          - email: team@example.com
        ```
    """

    agent_intent: Annotated[
        str,
        Field(
            min_length=10,
            max_length=500,
            description="Why the agent is performing this operation",
        ),
    ]
    summary: Annotated[
        str,
        Field(
            min_length=1,
            max_length=1024,
            description="Event title/summary",
        ),
    ]
    start: Annotated[TimeSlot, Field(description="Event start time")]
    end: Annotated[TimeSlot, Field(description="Event end time")]
    description: Annotated[
        str | None,
        Field(default=None, max_length=8192, description="Event description"),
    ]
    location: Annotated[
        str | None,
        Field(default=None, max_length=1024, description="Event location"),
    ]
    attendees: Annotated[
        list[Attendee],
        Field(default_factory=list, description="List of attendees"),
    ]
    reminders: Annotated[
        list[Reminder] | None,
        Field(default=None, description="Custom reminders"),
    ]
    recurrence: Annotated[
        list[str] | None,
        Field(default=None, description="Recurrence rules (RRULE format)"),
    ]
    visibility: Annotated[
        str | None,
        Field(
            default=None,
            description="Visibility: default, public, private, confidential",
        ),
    ]
    colorId: Annotated[
        str | None,
        Field(default=None, description="Color ID (1-11)"),
    ]


class UpdateEventSchema(BaseModel):
    """Schema for updating an existing calendar event.

    All fields except agent_intent are optional (partial update).

    Example:
        ```yaml
        # agent_intent: User wants to reschedule meeting to 2pm
        start:
          dateTime: "2024-01-15T14:00:00-08:00"
          timeZone: America/Los_Angeles
        end:
          dateTime: "2024-01-15T15:00:00-08:00"
          timeZone: America/Los_Angeles
        ```
    """

    agent_intent: Annotated[
        str,
        Field(
            min_length=10,
            max_length=500,
            description="Why the agent is performing this operation",
        ),
    ]
    summary: Annotated[
        str | None,
        Field(default=None, min_length=1, max_length=1024, description="Event title"),
    ]
    start: Annotated[TimeSlot | None, Field(default=None, description="Start time")]
    end: Annotated[TimeSlot | None, Field(default=None, description="End time")]
    description: Annotated[
        str | None,
        Field(default=None, max_length=8192, description="Event description"),
    ]
    location: Annotated[
        str | None,
        Field(default=None, max_length=1024, description="Event location"),
    ]
    attendees: Annotated[
        list[Attendee] | None,
        Field(default=None, description="List of attendees"),
    ]
    reminders: Annotated[
        list[Reminder] | None,
        Field(default=None, description="Custom reminders"),
    ]
    recurrence: Annotated[
        list[str] | None,
        Field(default=None, description="Recurrence rules"),
    ]
    visibility: Annotated[
        str | None,
        Field(default=None, description="Visibility setting"),
    ]
    colorId: Annotated[
        str | None,
        Field(default=None, description="Color ID"),
    ]


class DeleteEventSchema(BaseModel):
    """Schema for deleting a calendar event.

    Requires both agent_intent and explicit confirmation.

    Example:
        ```yaml
        # agent_intent: User wants to cancel the meeting
        # confirm: true
        ```
    """

    agent_intent: Annotated[
        str,
        Field(
            min_length=10,
            max_length=500,
            description="Why the agent is deleting this event",
        ),
    ]
    confirm: Annotated[
        bool,
        Field(description="Explicit confirmation required for delete operations"),
    ]
    send_notifications: Annotated[
        bool,
        Field(
            default=True,
            description="Whether to send cancellation notifications to attendees",
        ),
    ]
