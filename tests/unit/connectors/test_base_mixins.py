"""Tests for connector base mixins.

Tests the core mixin functionality:
- OpTraits and enums
- SkillDocMixin (SKILL.md generation)
- ValidatedMixin (Pydantic validation)
- TraitBasedMixin (operation trait validation)
- CheckpointMixin (rollback support)
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel, Field

from nexus.connectors.base import (
    CheckpointMixin,
    ConfirmLevel,
    ErrorDef,
    OpTraits,
    Reversibility,
    SkillDocMixin,
    TraitBasedMixin,
    ValidatedMixin,
    ValidationError,
)

# =============================================================================
# Test Fixtures
# =============================================================================


class SampleSchema(BaseModel):
    """Test schema for validation."""

    agent_intent: str = Field(..., min_length=10)
    summary: str = Field(..., min_length=1)
    value: int = Field(default=0, ge=0)


class TestConnector(SkillDocMixin, ValidatedMixin, TraitBasedMixin, CheckpointMixin):
    """Test connector with all mixins."""

    SKILL_NAME = "test-connector"
    SKILL_DIR = ".skill"

    SCHEMAS = {
        "create": SampleSchema,
    }

    OPERATION_TRAITS = {
        "create": OpTraits(
            reversibility=Reversibility.FULL,
            confirm=ConfirmLevel.INTENT,
            checkpoint=True,
        ),
        "delete": OpTraits(
            reversibility=Reversibility.FULL,
            confirm=ConfirmLevel.EXPLICIT,
            checkpoint=True,
        ),
        "send": OpTraits(
            reversibility=Reversibility.NONE,
            confirm=ConfirmLevel.USER,
            checkpoint=False,
            warnings=["This action cannot be undone"],
        ),
    }

    ERROR_REGISTRY = {
        "TEST_ERROR": ErrorDef(
            message="Test error message",
            skill_section="test-section",
            fix_example="# Fix example",
        ),
    }


# =============================================================================
# OpTraits Tests
# =============================================================================


class TestOpTraits:
    """Tests for OpTraits dataclass."""

    def test_default_values(self):
        """Test OpTraits default values."""
        traits = OpTraits()

        assert traits.reversibility == Reversibility.FULL
        assert traits.confirm == ConfirmLevel.INTENT
        assert traits.checkpoint is True
        assert traits.intent_min_length == 10
        assert traits.warnings == []

    def test_custom_values(self):
        """Test OpTraits with custom values."""
        traits = OpTraits(
            reversibility=Reversibility.NONE,
            confirm=ConfirmLevel.USER,
            checkpoint=False,
            warnings=["Warning 1", "Warning 2"],
        )

        assert traits.reversibility == Reversibility.NONE
        assert traits.confirm == ConfirmLevel.USER
        assert traits.checkpoint is False
        assert len(traits.warnings) == 2


class TestReversibility:
    """Tests for Reversibility enum."""

    def test_values(self):
        """Test Reversibility enum values."""
        assert Reversibility.FULL == "full"
        assert Reversibility.PARTIAL == "partial"
        assert Reversibility.NONE == "none"


class TestConfirmLevel:
    """Tests for ConfirmLevel enum."""

    def test_values(self):
        """Test ConfirmLevel enum values."""
        assert ConfirmLevel.NONE == "none"
        assert ConfirmLevel.INTENT == "intent"
        assert ConfirmLevel.EXPLICIT == "explicit"
        assert ConfirmLevel.USER == "user"

    def test_ordering(self):
        """Test ConfirmLevel ordering (by value for logic checks)."""
        # Define expected order for documentation
        order = [ConfirmLevel.NONE, ConfirmLevel.INTENT, ConfirmLevel.EXPLICIT, ConfirmLevel.USER]
        assert len(order) == 4

        # Verify string values are distinct
        values = {c.value for c in ConfirmLevel}
        assert len(values) == 4


# =============================================================================
# ValidationError Tests
# =============================================================================


class TestValidationError:
    """Tests for ValidationError."""

    def test_basic_error(self):
        """Test basic validation error."""
        error = ValidationError(
            code="TEST_CODE",
            message="Test message",
        )

        assert error.code == "TEST_CODE"
        assert error.message == "Test message"
        assert "TEST_CODE" in str(error)
        assert "Test message" in str(error)

    def test_error_with_skill_reference(self):
        """Test error with skill reference."""
        error = ValidationError(
            code="TEST_CODE",
            message="Test message",
            skill_path="/skill/test/SKILL.md",
            skill_section="test-section",
        )

        formatted = error.format_message()
        assert "/skill/test/SKILL.md#test-section" in formatted

    def test_error_with_fix_example(self):
        """Test error with fix example."""
        error = ValidationError(
            code="TEST_CODE",
            message="Test message",
            fix_example="# agent_intent: Fix this",
        )

        formatted = error.format_message()
        assert "Fix:" in formatted
        assert "agent_intent" in formatted

    def test_error_with_field_errors(self):
        """Test error with field-level errors."""
        error = ValidationError(
            code="SCHEMA_ERROR",
            message="Validation failed",
            field_errors={
                "summary": "Field required",
                "value": "Must be >= 0",
            },
        )

        formatted = error.format_message()
        assert "summary: Field required" in formatted
        assert "value: Must be >= 0" in formatted


# =============================================================================
# SkillDocMixin Tests
# =============================================================================


class TestSkillDocMixin:
    """Tests for SkillDocMixin."""

    def test_generate_skill_doc(self):
        """Test SKILL.md generation."""
        connector = TestConnector()
        doc = connector.generate_skill_doc("/mnt/test/")

        # Check header
        assert "# Test Connector Connector" in doc

        # Check mount path
        assert "`/mnt/test/`" in doc

        # Check operations section
        assert "## Operations" in doc
        assert "### Create" in doc

        # Check required format
        assert "## Required Format" in doc
        assert "agent_intent" in doc

        # Check error codes
        assert "## Error Codes" in doc
        assert "TEST_ERROR" in doc

    def test_generate_skill_doc_with_explicit_confirm(self):
        """Test that explicit confirm operations are documented."""
        connector = TestConnector()
        doc = connector.generate_skill_doc("/mnt/test/")

        # Delete requires explicit confirmation
        assert "explicit confirmation" in doc.lower() or "confirm: true" in doc

    def test_format_display_name(self):
        """Test display name formatting."""
        connector = TestConnector()
        name = connector._format_display_name()

        assert name == "Test Connector"

    def test_format_error_with_skill_ref(self):
        """Test error formatting with skill reference."""
        connector = TestConnector()
        # Set mount path so skill_md_path is computed correctly
        connector.set_mount_path("/mnt/test")

        error = connector.format_error_with_skill_ref(
            code="TEST_ERROR",
            message="Custom message",
        )

        assert error.code == "TEST_ERROR"
        assert error.skill_path == "/mnt/test/.skill/SKILL.md"
        assert error.skill_section == "test-section"


# =============================================================================
# ValidatedMixin Tests
# =============================================================================


class TestValidatedMixin:
    """Tests for ValidatedMixin."""

    def test_validate_schema_success(self):
        """Test successful schema validation."""
        connector = TestConnector()

        data = {
            "agent_intent": "Test intent for validation",
            "summary": "Test summary",
            "value": 10,
        }

        result = connector.validate_schema("create", data)

        assert result.agent_intent == "Test intent for validation"
        assert result.summary == "Test summary"
        assert result.value == 10

    def test_validate_schema_missing_field(self):
        """Test validation with missing required field."""
        connector = TestConnector()

        data = {
            "agent_intent": "Test intent for validation",
            # Missing 'summary'
        }

        with pytest.raises(ValidationError) as exc_info:
            connector.validate_schema("create", data)

        assert exc_info.value.code == "SCHEMA_VALIDATION_ERROR"
        assert "summary" in exc_info.value.field_errors

    def test_validate_schema_invalid_value(self):
        """Test validation with invalid value."""
        connector = TestConnector()

        data = {
            "agent_intent": "Test intent for validation",
            "summary": "Test",
            "value": -1,  # Invalid: must be >= 0
        }

        with pytest.raises(ValidationError) as exc_info:
            connector.validate_schema("create", data)

        assert "value" in exc_info.value.field_errors

    def test_validate_schema_no_schema_defined(self):
        """Test validation when no schema is defined for operation."""
        connector = TestConnector()

        data = {"any": "data"}
        result = connector.validate_schema("unknown_operation", data)

        # Should return data unchanged
        assert result == data


# =============================================================================
# TraitBasedMixin Tests
# =============================================================================


class TestTraitBasedMixin:
    """Tests for TraitBasedMixin."""

    def test_validate_traits_intent_required(self):
        """Test that agent_intent is required for INTENT level."""
        connector = TestConnector()

        # Missing agent_intent
        data = {"summary": "Test"}

        with pytest.raises(ValidationError) as exc_info:
            connector.validate_traits("create", data)

        assert exc_info.value.code == "MISSING_AGENT_INTENT"

    def test_validate_traits_intent_too_short(self):
        """Test that agent_intent must meet minimum length."""
        connector = TestConnector()

        data = {"agent_intent": "short"}  # Less than 10 chars

        with pytest.raises(ValidationError) as exc_info:
            connector.validate_traits("create", data)

        assert exc_info.value.code == "AGENT_INTENT_TOO_SHORT"

    def test_validate_traits_explicit_confirm_required(self):
        """Test that confirm is required for EXPLICIT level."""
        connector = TestConnector()

        data = {"agent_intent": "Deleting this item because user requested"}
        # Missing confirm: true

        with pytest.raises(ValidationError) as exc_info:
            connector.validate_traits("delete", data)

        assert exc_info.value.code == "MISSING_CONFIRM"

    def test_validate_traits_explicit_confirm_success(self):
        """Test successful validation with explicit confirm."""
        connector = TestConnector()

        data = {
            "agent_intent": "Deleting this item because user requested",
            "confirm": True,
        }

        warnings = connector.validate_traits("delete", data)
        assert warnings == []  # No warnings

    def test_validate_traits_user_confirm_required(self):
        """Test that user_confirmed is required for USER level."""
        connector = TestConnector()

        data = {
            "agent_intent": "Sending email as requested",
            "confirm": True,
            # Missing user_confirmed
        }

        with pytest.raises(ValidationError) as exc_info:
            connector.validate_traits("send", data)

        assert exc_info.value.code == "MISSING_USER_CONFIRMATION"

    def test_validate_traits_returns_warnings(self):
        """Test that warnings are returned."""
        connector = TestConnector()

        data = {
            "agent_intent": "Sending email as requested by user",
            "confirm": True,
            "user_confirmed": True,
        }

        warnings = connector.validate_traits("send", data)

        assert len(warnings) == 1
        assert "cannot be undone" in warnings[0]

    def test_validate_traits_unknown_operation(self):
        """Test validation for unknown operation (no traits defined)."""
        connector = TestConnector()

        data = {"any": "data"}
        warnings = connector.validate_traits("unknown", data)

        assert warnings == []

    def test_get_operation_traits(self):
        """Test getting traits for an operation."""
        connector = TestConnector()

        traits = connector.get_operation_traits("create")
        assert traits is not None
        assert traits.reversibility == Reversibility.FULL

        traits = connector.get_operation_traits("unknown")
        assert traits is None


# =============================================================================
# CheckpointMixin Tests
# =============================================================================


class TestCheckpointMixin:
    """Tests for CheckpointMixin."""

    def test_create_checkpoint(self):
        """Test checkpoint creation."""
        connector = TestConnector()

        checkpoint = connector.create_checkpoint(
            "create",
            metadata={"test": "value"},
        )

        assert checkpoint is not None
        assert checkpoint.operation == "create"
        assert checkpoint.metadata == {"test": "value"}
        assert checkpoint.checkpoint_id is not None

    def test_create_checkpoint_disabled(self):
        """Test that checkpoint is not created when disabled."""
        connector = TestConnector()

        # 'send' has checkpoint=False
        checkpoint = connector.create_checkpoint("send")

        assert checkpoint is None

    def test_complete_checkpoint(self):
        """Test completing a checkpoint."""
        connector = TestConnector()

        checkpoint = connector.create_checkpoint("create")
        connector.complete_checkpoint(
            checkpoint.checkpoint_id,
            {"event_id": "123"},
        )

        stored = connector.get_checkpoint(checkpoint.checkpoint_id)
        assert stored.created_state == {"event_id": "123"}

    def test_get_checkpoint(self):
        """Test getting a checkpoint."""
        connector = TestConnector()

        checkpoint = connector.create_checkpoint("create")
        retrieved = connector.get_checkpoint(checkpoint.checkpoint_id)

        assert retrieved == checkpoint

    def test_get_checkpoint_not_found(self):
        """Test getting non-existent checkpoint."""
        connector = TestConnector()

        result = connector.get_checkpoint("non-existent-id")
        assert result is None

    def test_clear_checkpoint(self):
        """Test clearing a checkpoint."""
        connector = TestConnector()

        checkpoint = connector.create_checkpoint("create")
        connector.clear_checkpoint(checkpoint.checkpoint_id)

        assert connector.get_checkpoint(checkpoint.checkpoint_id) is None

    def test_rollback_not_found(self):
        """Test rollback with non-existent checkpoint."""
        connector = TestConnector()

        with pytest.raises(ValidationError) as exc_info:
            connector.rollback("non-existent-id")

        assert exc_info.value.code == "CHECKPOINT_NOT_FOUND"
