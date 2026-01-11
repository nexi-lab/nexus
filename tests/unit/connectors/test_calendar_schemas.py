"""Tests for Google Calendar connector schemas.

Tests Pydantic schema validation for:
- CreateEventSchema
- UpdateEventSchema
- DeleteEventSchema
- TimeSlot validation
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError as PydanticValidationError

from nexus.connectors.calendar.schemas import (
    Attendee,
    CreateEventSchema,
    DeleteEventSchema,
    TimeSlot,
    UpdateEventSchema,
)


# =============================================================================
# TimeSlot Tests
# =============================================================================


class TestTimeSlot:
    """Tests for TimeSlot schema."""

    def test_valid_datetime_with_offset(self):
        """Test valid ISO 8601 datetime with offset."""
        slot = TimeSlot(
            dateTime="2024-01-15T09:00:00-08:00",
            timeZone="America/Los_Angeles",
        )

        assert slot.dateTime == "2024-01-15T09:00:00-08:00"
        assert slot.timeZone == "America/Los_Angeles"

    def test_valid_datetime_with_utc(self):
        """Test valid ISO 8601 datetime with Z (UTC)."""
        slot = TimeSlot(dateTime="2024-01-15T17:00:00Z")

        assert slot.dateTime == "2024-01-15T17:00:00Z"
        assert slot.timeZone == "UTC"  # Default

    def test_valid_datetime_positive_offset(self):
        """Test valid datetime with positive offset."""
        slot = TimeSlot(dateTime="2024-01-15T09:00:00+05:30")

        assert slot.dateTime == "2024-01-15T09:00:00+05:30"

    def test_invalid_datetime_no_offset(self):
        """Test that datetime without offset is rejected."""
        with pytest.raises(PydanticValidationError) as exc_info:
            TimeSlot(dateTime="2024-01-15T09:00:00")

        errors = exc_info.value.errors()
        assert any("dateTime" in str(e["loc"]) for e in errors)

    def test_invalid_datetime_format(self):
        """Test that invalid datetime format is rejected."""
        with pytest.raises(PydanticValidationError):
            TimeSlot(dateTime="2024/01/15 09:00:00")

    def test_invalid_datetime_date_only(self):
        """Test that date-only value is rejected."""
        with pytest.raises(PydanticValidationError):
            TimeSlot(dateTime="2024-01-15")


# =============================================================================
# Attendee Tests
# =============================================================================


class TestAttendee:
    """Tests for Attendee schema."""

    def test_minimal_attendee(self):
        """Test attendee with only email."""
        attendee = Attendee(email="user@example.com")

        assert attendee.email == "user@example.com"
        assert attendee.displayName is None
        assert attendee.optional is False

    def test_full_attendee(self):
        """Test attendee with all fields."""
        attendee = Attendee(
            email="user@example.com",
            displayName="Test User",
            optional=True,
            responseStatus="accepted",
        )

        assert attendee.email == "user@example.com"
        assert attendee.displayName == "Test User"
        assert attendee.optional is True
        assert attendee.responseStatus == "accepted"


# =============================================================================
# CreateEventSchema Tests
# =============================================================================


class TestCreateEventSchema:
    """Tests for CreateEventSchema."""

    def test_minimal_valid_event(self):
        """Test creating event with minimal required fields."""
        event = CreateEventSchema(
            agent_intent="User requested a team meeting for project discussion",
            summary="Team Meeting",
            start=TimeSlot(dateTime="2024-01-15T09:00:00-08:00"),
            end=TimeSlot(dateTime="2024-01-15T10:00:00-08:00"),
        )

        assert event.summary == "Team Meeting"
        assert event.description is None
        assert event.attendees == []

    def test_full_event(self):
        """Test creating event with all fields."""
        event = CreateEventSchema(
            agent_intent="User requested weekly standup with the team",
            summary="Weekly Standup",
            start=TimeSlot(
                dateTime="2024-01-15T09:00:00-08:00",
                timeZone="America/Los_Angeles",
            ),
            end=TimeSlot(
                dateTime="2024-01-15T09:30:00-08:00",
                timeZone="America/Los_Angeles",
            ),
            description="Weekly team sync to discuss progress",
            location="Conference Room A",
            attendees=[
                Attendee(email="alice@example.com"),
                Attendee(email="bob@example.com", optional=True),
            ],
            visibility="private",
        )

        assert event.summary == "Weekly Standup"
        assert event.description == "Weekly team sync to discuss progress"
        assert len(event.attendees) == 2
        assert event.visibility == "private"

    def test_agent_intent_required(self):
        """Test that agent_intent is required."""
        with pytest.raises(PydanticValidationError) as exc_info:
            CreateEventSchema(
                summary="Test",
                start=TimeSlot(dateTime="2024-01-15T09:00:00-08:00"),
                end=TimeSlot(dateTime="2024-01-15T10:00:00-08:00"),
            )

        errors = exc_info.value.errors()
        assert any("agent_intent" in str(e["loc"]) for e in errors)

    def test_agent_intent_min_length(self):
        """Test that agent_intent must be at least 10 characters."""
        with pytest.raises(PydanticValidationError) as exc_info:
            CreateEventSchema(
                agent_intent="short",  # Less than 10 chars
                summary="Test",
                start=TimeSlot(dateTime="2024-01-15T09:00:00-08:00"),
                end=TimeSlot(dateTime="2024-01-15T10:00:00-08:00"),
            )

        errors = exc_info.value.errors()
        assert any("agent_intent" in str(e["loc"]) for e in errors)

    def test_summary_required(self):
        """Test that summary is required."""
        with pytest.raises(PydanticValidationError) as exc_info:
            CreateEventSchema(
                agent_intent="Creating event for user request",
                start=TimeSlot(dateTime="2024-01-15T09:00:00-08:00"),
                end=TimeSlot(dateTime="2024-01-15T10:00:00-08:00"),
            )

        errors = exc_info.value.errors()
        assert any("summary" in str(e["loc"]) for e in errors)

    def test_summary_not_empty(self):
        """Test that summary cannot be empty."""
        with pytest.raises(PydanticValidationError):
            CreateEventSchema(
                agent_intent="Creating event for user request",
                summary="",  # Empty
                start=TimeSlot(dateTime="2024-01-15T09:00:00-08:00"),
                end=TimeSlot(dateTime="2024-01-15T10:00:00-08:00"),
            )

    def test_start_required(self):
        """Test that start time is required."""
        with pytest.raises(PydanticValidationError) as exc_info:
            CreateEventSchema(
                agent_intent="Creating event for user request",
                summary="Test",
                end=TimeSlot(dateTime="2024-01-15T10:00:00-08:00"),
            )

        errors = exc_info.value.errors()
        assert any("start" in str(e["loc"]) for e in errors)

    def test_recurrence_rules(self):
        """Test event with recurrence rules."""
        event = CreateEventSchema(
            agent_intent="Creating recurring weekly meeting",
            summary="Weekly Sync",
            start=TimeSlot(dateTime="2024-01-15T09:00:00-08:00"),
            end=TimeSlot(dateTime="2024-01-15T10:00:00-08:00"),
            recurrence=["RRULE:FREQ=WEEKLY;BYDAY=MO"],
        )

        assert event.recurrence == ["RRULE:FREQ=WEEKLY;BYDAY=MO"]


# =============================================================================
# UpdateEventSchema Tests
# =============================================================================


class TestUpdateEventSchema:
    """Tests for UpdateEventSchema."""

    def test_partial_update(self):
        """Test that update allows partial data."""
        update = UpdateEventSchema(
            agent_intent="Rescheduling meeting per user request",
            summary="New Title",
            # Other fields optional
        )

        assert update.summary == "New Title"
        assert update.start is None
        assert update.end is None

    def test_update_time_only(self):
        """Test updating only time fields."""
        update = UpdateEventSchema(
            agent_intent="Moving meeting to afternoon",
            start=TimeSlot(dateTime="2024-01-15T14:00:00-08:00"),
            end=TimeSlot(dateTime="2024-01-15T15:00:00-08:00"),
        )

        assert update.start is not None
        assert update.summary is None

    def test_agent_intent_required_for_update(self):
        """Test that agent_intent is required for updates."""
        with pytest.raises(PydanticValidationError):
            UpdateEventSchema(
                summary="New Title",
                # Missing agent_intent
            )


# =============================================================================
# DeleteEventSchema Tests
# =============================================================================


class TestDeleteEventSchema:
    """Tests for DeleteEventSchema."""

    def test_valid_delete(self):
        """Test valid delete request."""
        delete = DeleteEventSchema(
            agent_intent="User wants to cancel this meeting",
            confirm=True,
        )

        assert delete.confirm is True
        assert delete.send_notifications is True  # Default

    def test_delete_without_notifications(self):
        """Test delete without sending notifications."""
        delete = DeleteEventSchema(
            agent_intent="Silently removing old event",
            confirm=True,
            send_notifications=False,
        )

        assert delete.send_notifications is False

    def test_agent_intent_required_for_delete(self):
        """Test that agent_intent is required for delete."""
        with pytest.raises(PydanticValidationError):
            DeleteEventSchema(
                confirm=True,
                # Missing agent_intent
            )

    def test_confirm_required_for_delete(self):
        """Test that confirm is required for delete."""
        with pytest.raises(PydanticValidationError):
            DeleteEventSchema(
                agent_intent="Deleting event per user request",
                # Missing confirm
            )
