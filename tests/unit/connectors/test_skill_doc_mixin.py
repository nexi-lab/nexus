"""Tests for SkillDocMixin public API — generate_skill_doc and format_error_with_skill_ref.

Private method tests (schema_to_yaml_lines, is_nested_model, format_type_hint,
generate_errors_section, get_field_example) have been migrated to
test_schema_generator.py which tests SkillDocGenerator directly.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from nexus.backends.connectors.base import (
    ConfirmLevel,
    ErrorDef,
    OpTraits,
    Reversibility,
    SkillDocMixin,
)

# ---------------------------------------------------------------------------
# Test schemas
# ---------------------------------------------------------------------------


class SimpleSchema(BaseModel):
    summary: str
    count: int
    active: bool


class OptionalSchema(BaseModel):
    title: str
    description: str = "default description"
    color_id: int = 1
    notify: bool = False


# ---------------------------------------------------------------------------
# Fixture: a configured SkillDocMixin instance
# ---------------------------------------------------------------------------


class FakeConnector(SkillDocMixin):
    SKILL_NAME = "test_skill"
    SKILL_DIR = ".skill"
    SCHEMAS = {
        "create_event": SimpleSchema,
        "update_event": OptionalSchema,
    }
    OPERATION_TRAITS = {
        "create_event": OpTraits(
            reversibility=Reversibility.FULL,
            confirm=ConfirmLevel.INTENT,
        ),
        "delete_event": OpTraits(
            reversibility=Reversibility.NONE,
            confirm=ConfirmLevel.USER,
            warnings=["THIS ACTION CANNOT BE UNDONE"],
        ),
        "update_event": OpTraits(
            reversibility=Reversibility.FULL,
            confirm=ConfirmLevel.EXPLICIT,
        ),
    }
    ERROR_REGISTRY = {
        "MISSING_AGENT_INTENT": ErrorDef(
            message="Operations require agent_intent",
            skill_section="required-format",
            fix_example="# agent_intent: User requested meeting",
        ),
    }
    EXAMPLES = {"create_meeting.yaml": "summary: Team Standup\n"}


@pytest.fixture()
def mixin() -> FakeConnector:
    conn = FakeConnector()
    conn.set_mount_path("/mnt/calendar")
    return conn


# ---------------------------------------------------------------------------
# generate_skill_doc (public API)
# ---------------------------------------------------------------------------


class TestGenerateSkillDoc:
    def test_structure(self, mixin: FakeConnector) -> None:
        doc = mixin.generate_skill_doc("/mnt/calendar")
        assert "# Test Skill Connector" in doc
        assert "## Mount Path" in doc
        assert "`/mnt/calendar`" in doc
        assert "## Operations" in doc
        assert "## Required Format" in doc
        assert "## Error Codes" in doc


# ---------------------------------------------------------------------------
# format_error_with_skill_ref (public API)
# ---------------------------------------------------------------------------


class TestFormatErrorWithSkillRef:
    def test_from_registry(self, mixin: FakeConnector) -> None:
        err = mixin.format_error_with_skill_ref(
            code="MISSING_AGENT_INTENT",
            message="",
        )
        assert err.code == "MISSING_AGENT_INTENT"
        assert "required-format" in (err.skill_section or "")
        assert "/mnt/calendar/.skill/SKILL.md" in (err.skill_path or "")

    def test_custom_error(self, mixin: FakeConnector) -> None:
        err = mixin.format_error_with_skill_ref(
            code="CUSTOM_ERROR",
            message="Something went wrong",
            section="operations",
            fix_example="# fix: do this",
        )
        assert err.code == "CUSTOM_ERROR"
        # NexusError.__init__ overwrites self.message with format_message() output
        assert "[CUSTOM_ERROR] Something went wrong" in err.message
        assert "operations" in err.message
