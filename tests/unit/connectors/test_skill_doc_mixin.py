"""Characterization tests for SkillDocMixin — captures current behavior before refactoring."""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from nexus.connectors.base import (
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


class NestedChild(BaseModel):
    date_time: str
    time_zone: str


class NestedSchema(BaseModel):
    summary: str
    start: NestedChild
    end: NestedChild


class ListSchema(BaseModel):
    summary: str
    attendees: list[str]
    tags: list[int] = []


class OptionalModelSchema(BaseModel):
    summary: str
    start: NestedChild | None = None


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
# _schema_to_yaml_lines
# ---------------------------------------------------------------------------


class TestSchemaToYamlLines:
    def test_simple_fields(self, mixin: FakeConnector) -> None:
        lines = mixin._schema_to_yaml_lines(SimpleSchema)
        text = "\n".join(lines)
        assert "summary:" in text
        assert "count:" in text
        assert "active:" in text

    def test_optional_fields(self, mixin: FakeConnector) -> None:
        lines = mixin._schema_to_yaml_lines(OptionalSchema)
        text = "\n".join(lines)
        # Fields with defaults should show the default
        assert "description: default description" in text
        assert "color_id: 1" in text
        assert "notify: false" in text

    def test_nested_model(self, mixin: FakeConnector) -> None:
        lines = mixin._schema_to_yaml_lines(NestedSchema)
        text = "\n".join(lines)
        assert "start:" in text
        assert "end:" in text
        # Nested example lines should be indented
        assert "  dateTime:" in text or "  date_time:" in text or '  dateTime: "2024' in text

    def test_list_field(self, mixin: FakeConnector) -> None:
        lines = mixin._schema_to_yaml_lines(ListSchema)
        text = "\n".join(lines)
        # tags has default [] so should show []
        assert "tags: []" in text


# ---------------------------------------------------------------------------
# _is_nested_model
# ---------------------------------------------------------------------------


class TestIsNestedModel:
    def test_pydantic_model(self, mixin: FakeConnector) -> None:
        assert mixin._is_nested_model(NestedChild) is True

    def test_primitive(self, mixin: FakeConnector) -> None:
        assert mixin._is_nested_model(str) is False
        assert mixin._is_nested_model(int) is False

    def test_optional_model(self, mixin: FakeConnector) -> None:
        # NestedChild | None should still be detected as nested
        assert mixin._is_nested_model(NestedChild | None) is True


# ---------------------------------------------------------------------------
# _format_type_hint
# ---------------------------------------------------------------------------


class TestFormatTypeHint:
    def test_str(self, mixin: FakeConnector) -> None:
        assert mixin._format_type_hint(str) == "string"

    def test_int(self, mixin: FakeConnector) -> None:
        assert mixin._format_type_hint(int) == "integer"

    def test_bool(self, mixin: FakeConnector) -> None:
        assert mixin._format_type_hint(bool) == "boolean"

    def test_list(self, mixin: FakeConnector) -> None:
        assert mixin._format_type_hint(list) == "list"


# ---------------------------------------------------------------------------
# generate_skill_doc
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
# _generate_errors_section
# ---------------------------------------------------------------------------


class TestGenerateErrorsSection:
    def test_errors_section(self, mixin: FakeConnector) -> None:
        lines = mixin._generate_errors_section()
        text = "\n".join(lines)
        assert "## Error Codes" in text
        assert "### MISSING_AGENT_INTENT" in text
        assert "Operations require agent_intent" in text
        assert "# agent_intent: User requested meeting" in text


# ---------------------------------------------------------------------------
# format_error_with_skill_ref
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


# ---------------------------------------------------------------------------
# _get_field_example
# ---------------------------------------------------------------------------


class TestGetFieldExample:
    def test_known_field_summary(self, mixin: FakeConnector) -> None:
        result = mixin._get_field_example("summary", None, str, True)
        assert result == '"Meeting Title"'

    def test_unknown_field(self, mixin: FakeConnector) -> None:
        result = mixin._get_field_example("custom_field", None, str, True)
        assert "string" in result
        assert "required" in result
