"""Integration tests for Google Calendar connector.

Tests the Calendar connector end-to-end including:
- Schema validation
- Trait-based validation
- Error formatting with README.md references
- README.md auto-generation
- YAML parsing

Note: These tests mock the Google Calendar API since we can't
use real OAuth tokens in CI. For full E2E testing with real
Google API, use the manual test script in scripts/test_gcalendar.py.
"""

from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError as PydanticValidationError

from nexus.backends.connectors.base import ValidationError
from nexus.backends.connectors.calendar.schemas import (
    CreateEventSchema,
    DeleteEventSchema,
    TimeSlot,
    UpdateEventSchema,
)
from nexus.backends.connectors.calendar.transport import CalendarTransport
from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.contracts.types import OperationContext

# ============================================================================
# FIXTURES
# ============================================================================


@pytest.fixture
def mock_calendar_service():
    """Create a mock Google Calendar service."""
    service = MagicMock()

    # Mock events().insert()
    service.events().insert().execute.return_value = {
        "id": "test_event_123",
        "summary": "Test Event",
        "htmlLink": "https://calendar.google.com/calendar/event?eid=test",
    }

    # Mock events().get()
    service.events().get().execute.return_value = {
        "id": "test_event_123",
        "summary": "Existing Event",
        "start": {"dateTime": "2024-01-15T09:00:00-08:00", "timeZone": "America/Los_Angeles"},
        "end": {"dateTime": "2024-01-15T10:00:00-08:00", "timeZone": "America/Los_Angeles"},
    }

    # Mock events().update()
    service.events().update().execute.return_value = {
        "id": "test_event_123",
        "summary": "Updated Event",
    }

    # Mock events().delete()
    service.events().delete().execute.return_value = None

    # Mock events().list()
    service.events().list().execute.return_value = {
        "items": [
            {"id": "event1", "summary": "Event 1"},
            {"id": "event2", "summary": "Event 2"},
        ]
    }

    # Mock calendarList().list()
    service.calendarList().list().execute.return_value = {
        "items": [
            {"id": "primary", "summary": "Primary Calendar"},
            {"id": "work@example.com", "summary": "Work Calendar"},
        ]
    }

    return service


@pytest.fixture
def calendar_backend(mock_calendar_service, tmp_path):
    """Create a Calendar backend with mocked Google service."""
    from nexus.backends.connectors.calendar.connector import PathCalendarBackend

    # Create a mock token manager
    with patch(
        "nexus.backends.connectors.calendar.connector.PathCalendarBackend._register_oauth_provider"
    ):
        backend = PathCalendarBackend(
            token_manager_db=str(tmp_path / "tokens.db"),
            user_email="test@example.com",
        )

    # Replace _get_calendar_service on the transport (OAuth calls)
    backend._cal_transport._get_calendar_service = MagicMock(return_value=mock_calendar_service)

    return backend


@pytest.fixture
def operation_context():
    """Create an operation context for testing."""
    return OperationContext(
        user_id="test@example.com",
        groups=[],
        zone_id=ROOT_ZONE_ID,
    )


# ============================================================================
# SCHEMA VALIDATION TESTS
# ============================================================================


class TestCreateEventSchema:
    """Test CreateEventSchema validation."""

    def test_valid_minimal_event(self):
        """Test creating event with minimal required fields."""
        event = CreateEventSchema(
            agent_intent="User requested a meeting to discuss the project",
            summary="Project Discussion",
            start=TimeSlot(dateTime="2024-01-15T09:00:00-08:00"),
            end=TimeSlot(dateTime="2024-01-15T10:00:00-08:00"),
        )

        assert event.summary == "Project Discussion"
        assert event.start.dateTime == "2024-01-15T09:00:00-08:00"

    def test_missing_agent_intent_fails(self):
        """Test that missing agent_intent raises validation error."""
        with pytest.raises(PydanticValidationError) as exc_info:
            CreateEventSchema(
                summary="Test Event",
                start=TimeSlot(dateTime="2024-01-15T09:00:00-08:00"),
                end=TimeSlot(dateTime="2024-01-15T10:00:00-08:00"),
            )

        # Should mention agent_intent in the error
        assert "agent_intent" in str(exc_info.value).lower()

    def test_short_agent_intent_fails(self):
        """Test that short agent_intent raises validation error."""
        with pytest.raises(PydanticValidationError):
            CreateEventSchema(
                agent_intent="short",  # Less than 10 chars
                summary="Test Event",
                start=TimeSlot(dateTime="2024-01-15T09:00:00-08:00"),
                end=TimeSlot(dateTime="2024-01-15T10:00:00-08:00"),
            )

    def test_invalid_datetime_format_fails(self):
        """Test that invalid datetime format raises error."""
        with pytest.raises(PydanticValidationError):
            CreateEventSchema(
                agent_intent="Creating event for user request",
                summary="Test Event",
                start=TimeSlot(dateTime="2024-01-15 09:00:00"),  # Missing T and offset
                end=TimeSlot(dateTime="2024-01-15T10:00:00-08:00"),
            )


class TestUpdateEventSchema:
    """Test UpdateEventSchema validation."""

    def test_partial_update(self):
        """Test that partial updates are allowed."""
        update = UpdateEventSchema(
            agent_intent="Updating title per user request",
            summary="New Title",
        )

        assert update.summary == "New Title"
        assert update.start is None
        assert update.end is None


class TestDeleteEventSchema:
    """Test DeleteEventSchema validation."""

    def test_delete_requires_confirm(self):
        """Test that delete requires confirm=true."""
        with pytest.raises(PydanticValidationError):
            DeleteEventSchema(
                agent_intent="Deleting event per user request",
                # Missing confirm=True
            )

    def test_valid_delete(self):
        """Test valid delete schema."""
        delete = DeleteEventSchema(
            agent_intent="User wants to cancel this meeting",
            confirm=True,
        )

        assert delete.confirm is True


# ============================================================================
# TRAIT VALIDATION TESTS
# ============================================================================


class TestTraitValidation:
    """Test trait-based validation."""

    def test_create_requires_intent(self, calendar_backend):
        """Test that create_event requires agent_intent."""
        data = {"summary": "Test"}  # Missing agent_intent

        with pytest.raises(ValidationError) as exc_info:
            calendar_backend.validate_traits("create_event", data)

        assert exc_info.value.code == "MISSING_AGENT_INTENT"
        assert "README.md" in str(exc_info.value)

    def test_delete_requires_explicit_confirm(self, calendar_backend):
        """Test that delete_event requires confirm=true."""
        data = {
            "agent_intent": "Deleting this event per user request",
            # Missing confirm=True
        }

        with pytest.raises(ValidationError) as exc_info:
            calendar_backend.validate_traits("delete_event", data)

        assert exc_info.value.code == "MISSING_CONFIRM"

    def test_valid_create_passes(self, calendar_backend):
        """Test that valid create data passes trait validation."""
        data = {
            "agent_intent": "Creating event for user's team meeting",
        }

        warnings = calendar_backend.validate_traits("create_event", data)
        assert warnings == []

    def test_valid_delete_passes(self, calendar_backend):
        """Test that valid delete data passes trait validation."""
        data = {
            "agent_intent": "Deleting event per user request",
            "confirm": True,
        }

        warnings = calendar_backend.validate_traits("delete_event", data)
        assert warnings == []


# ============================================================================
# SKILL.MD GENERATION TESTS
# ============================================================================


class TestReadmeDocGeneration:
    """Test README.md auto-generation."""

    def test_generate_readme(self, calendar_backend):
        """Test that README.md is generated correctly."""
        doc = calendar_backend.generate_readme("/mnt/calendar/")

        # Check header
        assert "# Gcalendar Connector" in doc

        # Check mount path
        assert "`/mnt/calendar/`" in doc

        # Check operations section
        assert "## Operations" in doc
        assert "Create Event" in doc
        assert "Update Event" in doc
        assert "Delete Event" in doc

        # Check required format
        assert "agent_intent" in doc

        # Check error codes section
        assert "## Error Codes" in doc
        assert "MISSING_AGENT_INTENT" in doc

    def test_readme_doc_includes_examples(self, calendar_backend):
        """Test that README.md includes YAML examples."""
        doc = calendar_backend.generate_readme("/mnt/calendar/")

        # Should include YAML code blocks
        assert "```yaml" in doc
        assert "# agent_intent:" in doc


# ============================================================================
# YAML PARSING TESTS
# ============================================================================


class TestYAMLParsing:
    """Test YAML content parsing."""

    def test_parse_yaml_with_comments(self, calendar_backend):
        """Test parsing YAML with agent_intent and confirm comments."""
        content = b"""# agent_intent: User wants to create a team meeting
# confirm: true
summary: Team Meeting
start:
  dateTime: "2024-01-15T09:00:00-08:00"
  timeZone: America/Los_Angeles
end:
  dateTime: "2024-01-15T10:00:00-08:00"
  timeZone: America/Los_Angeles
"""

        data = CalendarTransport._parse_yaml_content(content)

        assert data["agent_intent"] == "User wants to create a team meeting"
        assert data["confirm"] is True
        assert data["summary"] == "Team Meeting"

    def test_parse_yaml_extracts_agent_intent(self, calendar_backend):
        """Test that agent_intent is extracted from comment."""
        content = b"""# agent_intent: Creating weekly standup for the team
summary: Weekly Standup
start:
  dateTime: "2024-01-15T09:00:00-08:00"
"""

        data = CalendarTransport._parse_yaml_content(content)

        assert "agent_intent" in data
        assert "weekly standup" in data["agent_intent"].lower()


# ============================================================================
# ERROR FORMATTING TESTS
# ============================================================================


class TestErrorFormatting:
    """Test error message formatting with README.md references."""

    def test_error_includes_readme_path(self, calendar_backend):
        """Test that errors include README.md path."""
        # Set mount path so readme_md_path is computed correctly
        calendar_backend.set_mount_path("/mnt/calendar")

        error = calendar_backend.format_error_with_skill_ref(
            code="MISSING_AGENT_INTENT",
            message="Missing required field",
        )

        assert "/mnt/calendar/.readme/README.md" in str(error)

    def test_error_includes_section_anchor(self, calendar_backend):
        """Test that errors include section anchor."""
        error = calendar_backend.format_error_with_skill_ref(
            code="MISSING_AGENT_INTENT",
            message="Missing required field",
            section="required-format",
        )

        assert "#required-format" in str(error)

    def test_error_includes_fix_example(self, calendar_backend):
        """Test that errors from registry include fix example."""
        error = calendar_backend.format_error_with_skill_ref(
            code="MISSING_AGENT_INTENT",
            message="",
        )

        # Should include fix example from ERROR_REGISTRY
        assert "agent_intent" in str(error)


# ============================================================================
# MOCK API CALL TESTS
# ============================================================================


class TestMockedAPICalls:
    """Test connector operations with mocked Google API."""

    def test_create_event_success(self, calendar_backend, operation_context):
        """Test successful event creation."""
        content = b"""# agent_intent: Creating team meeting for project discussion
summary: Project Discussion
start:
  dateTime: "2024-01-15T09:00:00-08:00"
  timeZone: America/Los_Angeles
end:
  dateTime: "2024-01-15T10:00:00-08:00"
  timeZone: America/Los_Angeles
"""

        # Set backend path in context
        operation_context.backend_path = "primary/_new.yaml"

        response = calendar_backend.write_content(content, context=operation_context)

        assert response.content_id == "test_event_123"

    def test_update_event_success(self, calendar_backend, operation_context):
        """Test successful event update."""
        content = b"""# agent_intent: Updating meeting title per user request
summary: Updated Project Discussion
"""

        operation_context.backend_path = "primary/test_event_123.yaml"

        response = calendar_backend.write_content(content, context=operation_context)

        assert response.content_id == "updated"

    def test_list_calendars(self, calendar_backend, operation_context):
        """Test listing calendars."""
        calendars = calendar_backend.list_dir("", context=operation_context)

        assert "primary/" in calendars
        assert len(calendars) >= 1

    def test_list_events(self, calendar_backend, operation_context):
        """Test listing events in a calendar."""
        events = calendar_backend.list_dir("primary", context=operation_context)

        # Human-readable key format: "{summary-slug}__{eventId}.yaml".
        # Events are ordered newest-first by default (reverse-chronological).
        assert any(e.endswith("__event1.yaml") for e in events)
        assert any(e.endswith("__event2.yaml") for e in events)


# ============================================================================
# CHECKPOINT TESTS
# ============================================================================


class TestCheckpoints:
    """Test checkpoint/rollback functionality."""

    def test_create_checkpoint_for_create(self, calendar_backend):
        """Test that checkpoint is created for create operations."""
        checkpoint = calendar_backend.create_checkpoint(
            "create_event",
            metadata={"calendar_id": "primary"},
        )

        assert checkpoint is not None
        assert checkpoint.operation == "create_event"
        assert checkpoint.metadata["calendar_id"] == "primary"

    def test_no_checkpoint_for_non_checkpoint_operations(self, calendar_backend):
        """Test that checkpoint is not created when disabled."""
        # Temporarily set checkpoint=False
        original_traits = calendar_backend.OPERATION_TRAITS["create_event"]
        calendar_backend.OPERATION_TRAITS["test_no_checkpoint"] = type(original_traits)(
            checkpoint=False
        )

        checkpoint = calendar_backend.create_checkpoint("test_no_checkpoint")

        assert checkpoint is None

    def test_complete_and_clear_checkpoint(self, calendar_backend):
        """Test completing and clearing checkpoints."""
        checkpoint = calendar_backend.create_checkpoint("create_event")

        # Complete checkpoint
        calendar_backend.complete_checkpoint(
            checkpoint.checkpoint_id,
            {"event_id": "created_123"},
        )

        stored = calendar_backend.get_checkpoint(checkpoint.checkpoint_id)
        assert stored.created_state["event_id"] == "created_123"

        # Clear checkpoint
        calendar_backend.clear_checkpoint(checkpoint.checkpoint_id)
        assert calendar_backend.get_checkpoint(checkpoint.checkpoint_id) is None
