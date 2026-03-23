"""Tests for SkillDocGenerator — schema-to-doc generation extracted from SkillDocMixin."""

from unittest.mock import MagicMock

import pytest
from pydantic import BaseModel

from nexus.backends.connectors.base import ConfirmLevel, ErrorDef, OpTraits, Reversibility
from nexus.backends.connectors.schema_generator import SkillDocGenerator

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
# Fixtures
# ---------------------------------------------------------------------------

_DEFAULT_SCHEMAS: dict[str, type[BaseModel]] = {
    "create_event": SimpleSchema,
    "update_event": OptionalSchema,
}

_DEFAULT_TRAITS: dict[str, OpTraits] = {
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

_DEFAULT_ERRORS: dict[str, ErrorDef] = {
    "MISSING_AGENT_INTENT": ErrorDef(
        message="Operations require agent_intent",
        skill_section="required-format",
        fix_example="# agent_intent: User requested meeting",
    ),
}

_DEFAULT_EXAMPLES: dict[str, str] = {
    "create_meeting.yaml": "summary: Team Standup\n",
}


@pytest.fixture()
def generator() -> SkillDocGenerator:
    return SkillDocGenerator(
        skill_name="test_skill",
        schemas=_DEFAULT_SCHEMAS,
        operation_traits=_DEFAULT_TRAITS,
        error_registry=_DEFAULT_ERRORS,
        examples=_DEFAULT_EXAMPLES,
    )


@pytest.fixture()
def empty_generator() -> SkillDocGenerator:
    """Generator with no schemas, traits, or errors."""
    return SkillDocGenerator(
        skill_name="empty_skill",
        schemas={},
        operation_traits={},
        error_registry={},
        examples={},
    )


@pytest.fixture()
def mock_filesystem() -> MagicMock:
    from unittest.mock import AsyncMock

    fs = MagicMock()
    fs.mkdir = AsyncMock()
    fs.write = AsyncMock()
    return fs


# ===========================================================================
# generate_skill_doc
# ===========================================================================


class TestGenerateSkillDoc:
    def test_structure(self, generator: SkillDocGenerator) -> None:
        doc = generator.generate_skill_doc("/mnt/calendar")
        assert "# Test Skill Connector" in doc
        assert "## Mount Path" in doc
        assert "`/mnt/calendar`" in doc
        assert "## Operations" in doc
        assert "## Required Format" in doc
        assert "## Error Codes" in doc

    def test_sections_present_with_full_config(self, generator: SkillDocGenerator) -> None:
        doc = generator.generate_skill_doc("/mnt/cal")
        # Operations section lists each schema operation
        assert "### Create Event" in doc
        assert "### Update Event" in doc
        # Error Codes section lists each error
        assert "### MISSING_AGENT_INTENT" in doc

    def test_empty_schemas_omits_operations(self, empty_generator: SkillDocGenerator) -> None:
        doc = empty_generator.generate_skill_doc("/mnt/empty")
        assert "## Operations" not in doc

    def test_empty_traits_omits_required_format(self, empty_generator: SkillDocGenerator) -> None:
        doc = empty_generator.generate_skill_doc("/mnt/empty")
        assert "## Required Format" not in doc

    def test_empty_errors_omits_error_codes(self, empty_generator: SkillDocGenerator) -> None:
        doc = empty_generator.generate_skill_doc("/mnt/empty")
        assert "## Error Codes" not in doc

    def test_empty_config_has_header_and_mount(self, empty_generator: SkillDocGenerator) -> None:
        doc = empty_generator.generate_skill_doc("/mnt/empty")
        assert "# Empty Skill Connector" in doc
        assert "## Mount Path" in doc
        assert "`/mnt/empty`" in doc

    def test_display_name_formatting(self) -> None:
        gen = SkillDocGenerator(
            skill_name="my-cool_skill",
            schemas={},
            operation_traits={},
            error_registry={},
            examples={},
        )
        doc = gen.generate_skill_doc("/mnt/x")
        assert "# My Cool Skill Connector" in doc


# ===========================================================================
# write_skill_docs
# ===========================================================================


class TestWriteSkillDocs:
    async def test_writes_skill_md(
        self, generator: SkillDocGenerator, mock_filesystem: MagicMock
    ) -> None:
        result = await generator.write_skill_docs("/mnt/calendar", filesystem=mock_filesystem)

        assert result["skill_md"] == "/mnt/calendar/.skill/SKILL.md"
        mock_filesystem.mkdir.assert_any_call("/mnt/calendar/.skill", parents=True, exist_ok=True)
        # SKILL.md is written as bytes
        write_calls = mock_filesystem.write.call_args_list
        skill_md_call = write_calls[0]
        assert skill_md_call[0][0] == "/mnt/calendar/.skill/SKILL.md"
        assert isinstance(skill_md_call[0][1], bytes)

    async def test_writes_examples(
        self, generator: SkillDocGenerator, mock_filesystem: MagicMock
    ) -> None:
        result = await generator.write_skill_docs("/mnt/calendar", filesystem=mock_filesystem)

        assert "/mnt/calendar/.skill/examples/create_meeting.yaml" in result["examples"]
        mock_filesystem.mkdir.assert_any_call(
            "/mnt/calendar/.skill/examples", parents=True, exist_ok=True
        )
        # Verify example content written
        example_call = mock_filesystem.write.call_args_list[1]
        assert example_call[0][0] == "/mnt/calendar/.skill/examples/create_meeting.yaml"
        assert example_call[0][1] == b"summary: Team Standup\n"

    async def test_no_filesystem_returns_empty_result(self, generator: SkillDocGenerator) -> None:
        result = await generator.write_skill_docs("/mnt/calendar", filesystem=None)
        assert result["skill_md"] is None
        assert result["examples"] == []

    async def test_empty_skill_name_returns_empty_result(self, mock_filesystem: MagicMock) -> None:
        gen = SkillDocGenerator(
            skill_name="",
            schemas={},
            operation_traits={},
            error_registry={},
            examples={},
        )
        result = await gen.write_skill_docs("/mnt/x", filesystem=mock_filesystem)
        assert result["skill_md"] is None
        assert result["examples"] == []
        mock_filesystem.write.assert_not_called()

    async def test_no_examples_skips_examples_dir(self, mock_filesystem: MagicMock) -> None:
        gen = SkillDocGenerator(
            skill_name="test",
            schemas=_DEFAULT_SCHEMAS,
            operation_traits=_DEFAULT_TRAITS,
            error_registry=_DEFAULT_ERRORS,
            examples={},
        )
        result = await gen.write_skill_docs("/mnt/x", filesystem=mock_filesystem)
        assert result["skill_md"] is not None
        assert result["examples"] == []
        # Only the .skill dir mkdir, not examples/
        mkdir_paths = [c[0][0] for c in mock_filesystem.mkdir.call_args_list]
        assert "/mnt/x/.skill/examples" not in mkdir_paths

    async def test_filesystem_error_returns_partial_result(
        self, mock_filesystem: MagicMock
    ) -> None:
        mock_filesystem.mkdir.side_effect = OSError("permission denied")
        gen = SkillDocGenerator(
            skill_name="test",
            schemas={},
            operation_traits={},
            error_registry={},
            examples={},
        )
        result = await gen.write_skill_docs("/mnt/x", filesystem=mock_filesystem)
        assert result["skill_md"] is None
        assert result["examples"] == []

    async def test_custom_skill_dir(self, mock_filesystem: MagicMock) -> None:
        gen = SkillDocGenerator(
            skill_name="test",
            schemas={},
            operation_traits={},
            error_registry={},
            examples={},
            skill_dir=".docs",
        )
        await gen.write_skill_docs("/mnt/x", filesystem=mock_filesystem)
        mock_filesystem.mkdir.assert_any_call("/mnt/x/.docs", parents=True, exist_ok=True)


# ===========================================================================
# _schema_to_yaml_lines (migrated from TestSchemaToYamlLines)
# ===========================================================================


class TestSchemaToYamlLines:
    def test_simple_fields(self, generator: SkillDocGenerator) -> None:
        lines = generator._schema_to_yaml_lines(SimpleSchema)
        text = "\n".join(lines)
        assert "summary:" in text
        assert "count:" in text
        assert "active:" in text

    def test_optional_fields(self, generator: SkillDocGenerator) -> None:
        lines = generator._schema_to_yaml_lines(OptionalSchema)
        text = "\n".join(lines)
        # Fields with defaults should show the default
        assert "description: default description" in text
        assert "color_id: 1" in text
        assert "notify: false" in text

    def test_nested_model(self, generator: SkillDocGenerator) -> None:
        lines = generator._schema_to_yaml_lines(NestedSchema)
        text = "\n".join(lines)
        assert "start:" in text
        assert "end:" in text
        # Nested example lines should be indented
        assert "  " in text  # at least some indented content

    def test_list_field(self, generator: SkillDocGenerator) -> None:
        lines = generator._schema_to_yaml_lines(ListSchema)
        text = "\n".join(lines)
        # tags has default [] so should show []
        assert "tags: []" in text

    def test_attendees_list_field(self, generator: SkillDocGenerator) -> None:
        lines = generator._schema_to_yaml_lines(ListSchema)
        text = "\n".join(lines)
        # attendees is a required list[str] with no default
        assert "attendees:" in text

    def test_skips_agent_intent_and_confirm(self) -> None:
        class SchemaWithMeta(BaseModel):
            agent_intent: str = ""
            confirm: bool = False
            real_field: str

        gen = SkillDocGenerator(
            skill_name="test",
            schemas={},
            operation_traits={},
            error_registry={},
            examples={},
        )
        lines = gen._schema_to_yaml_lines(SchemaWithMeta)
        text = "\n".join(lines)
        assert "agent_intent" not in text
        assert "confirm" not in text
        assert "real_field:" in text


# ===========================================================================
# _is_nested_model (migrated from TestIsNestedModel)
# ===========================================================================


class TestIsNestedModel:
    def test_pydantic_model(self, generator: SkillDocGenerator) -> None:
        assert generator._is_nested_model(NestedChild) is True

    def test_primitive(self, generator: SkillDocGenerator) -> None:
        assert generator._is_nested_model(str) is False
        assert generator._is_nested_model(int) is False

    def test_optional_model(self, generator: SkillDocGenerator) -> None:
        # NestedChild | None should still be detected as nested
        assert generator._is_nested_model(NestedChild | None) is True

    def test_optional_primitive(self, generator: SkillDocGenerator) -> None:
        assert generator._is_nested_model(str | None) is False

    def test_bool_not_nested(self, generator: SkillDocGenerator) -> None:
        assert generator._is_nested_model(bool) is False

    def test_list_not_nested(self, generator: SkillDocGenerator) -> None:
        assert generator._is_nested_model(list[str]) is False

    def test_none_annotation(self, generator: SkillDocGenerator) -> None:
        assert generator._is_nested_model(None) is False


# ===========================================================================
# _format_type_hint (migrated from TestFormatTypeHint)
# ===========================================================================


class TestFormatTypeHint:
    def test_str(self, generator: SkillDocGenerator) -> None:
        assert generator._format_type_hint(str) == "string"

    def test_int(self, generator: SkillDocGenerator) -> None:
        assert generator._format_type_hint(int) == "integer"

    def test_bool(self, generator: SkillDocGenerator) -> None:
        assert generator._format_type_hint(bool) == "boolean"

    def test_list(self, generator: SkillDocGenerator) -> None:
        assert generator._format_type_hint(list) == "list"

    def test_dict(self, generator: SkillDocGenerator) -> None:
        assert generator._format_type_hint(dict) == "object"

    def test_none_returns_any(self, generator: SkillDocGenerator) -> None:
        assert generator._format_type_hint(None) == "any"


# ===========================================================================
# _generate_errors_section (migrated from TestGenerateErrorsSection)
# ===========================================================================


class TestGenerateErrorsSection:
    def test_errors_section(self, generator: SkillDocGenerator) -> None:
        lines = generator._generate_errors_section()
        text = "\n".join(lines)
        assert "## Error Codes" in text
        assert "### MISSING_AGENT_INTENT" in text
        assert "Operations require agent_intent" in text
        assert "# agent_intent: User requested meeting" in text

    def test_error_without_fix_example(self) -> None:
        gen = SkillDocGenerator(
            skill_name="test",
            schemas={},
            operation_traits={},
            error_registry={
                "SOME_ERROR": ErrorDef(
                    message="Something went wrong",
                    skill_section="operations",
                    fix_example=None,
                ),
            },
            examples={},
        )
        lines = gen._generate_errors_section()
        text = "\n".join(lines)
        assert "### SOME_ERROR" in text
        assert "Something went wrong" in text
        assert "**Fix:**" not in text

    def test_error_with_fix_example(self) -> None:
        gen = SkillDocGenerator(
            skill_name="test",
            schemas={},
            operation_traits={},
            error_registry={
                "FIX_ME": ErrorDef(
                    message="Broken",
                    skill_section="ops",
                    fix_example="do_this: true",
                ),
            },
            examples={},
        )
        lines = gen._generate_errors_section()
        text = "\n".join(lines)
        assert "**Fix:**" in text
        assert "do_this: true" in text


# ===========================================================================
# _get_field_example (migrated from TestGetFieldExample)
# ===========================================================================


class TestGetFieldExample:
    def test_known_field_via_field_examples(self) -> None:
        """Connector-provided field_examples dict takes priority."""
        gen = SkillDocGenerator(
            skill_name="test",
            schemas={},
            operation_traits={},
            error_registry={},
            examples={},
            field_examples={"summary": '"Meeting Title"'},
        )
        result = gen._get_field_example("summary", None, str, True)
        assert result == '"Meeting Title"'

    def test_unknown_field(self, generator: SkillDocGenerator) -> None:
        result = generator._get_field_example("custom_field", None, str, True)
        assert "string" in result
        assert "required" in result

    def test_unknown_optional_field(self, generator: SkillDocGenerator) -> None:
        result = generator._get_field_example("custom_field", None, str, False)
        assert "string" in result
        assert "optional" in result

    def test_bool_field_returns_true(self, generator: SkillDocGenerator) -> None:
        result = generator._get_field_example("flag", None, bool, True)
        assert result == "true"

    def test_int_field_returns_zero(self, generator: SkillDocGenerator) -> None:
        result = generator._get_field_example("count", None, int, True)
        assert result == "0"

    def test_list_field_returns_empty_list(self, generator: SkillDocGenerator) -> None:
        result = generator._get_field_example("items", None, list, True)
        assert result == "[]"

    def test_field_examples_override_type_based(self) -> None:
        """field_examples should override even type-based defaults."""
        gen = SkillDocGenerator(
            skill_name="test",
            schemas={},
            operation_traits={},
            error_registry={},
            examples={},
            field_examples={"count": "42"},
        )
        result = gen._get_field_example("count", None, int, True)
        assert result == "42"


# ===========================================================================
# _get_nested_example
# ===========================================================================


class TestGetNestedExample:
    def test_uses_connector_provided_nested_examples(self) -> None:
        gen = SkillDocGenerator(
            skill_name="test",
            schemas={},
            operation_traits={},
            error_registry={},
            examples={},
            nested_examples={"start": ['dateTime: "2024-01-01T09:00:00"', 'timeZone: "UTC"']},
        )
        lines = gen._get_nested_example("start", NestedChild, required=True)
        assert lines == ['dateTime: "2024-01-01T09:00:00"', 'timeZone: "UTC"']

    def test_fallback_required(self, generator: SkillDocGenerator) -> None:
        lines = generator._get_nested_example("start", NestedChild, required=True)
        assert len(lines) == 1
        assert "nested object" in lines[0]
        assert "required" in lines[0]

    def test_fallback_optional(self, generator: SkillDocGenerator) -> None:
        lines = generator._get_nested_example("start", NestedChild, required=False)
        assert len(lines) == 1
        assert "nested object" in lines[0]
        assert "optional" in lines[0]

    def test_returns_copy_not_original(self) -> None:
        """Returned list should be a copy so callers cannot mutate the config."""
        originals = ["a: 1", "b: 2"]
        gen = SkillDocGenerator(
            skill_name="test",
            schemas={},
            operation_traits={},
            error_registry={},
            examples={},
            nested_examples={"x": originals},
        )
        result = gen._get_nested_example("x", NestedChild, required=True)
        result.append("c: 3")
        # Original config should be unmodified
        assert len(gen._nested_examples["x"]) == 2


# ===========================================================================
# get_skill_path
# ===========================================================================


class TestGetSkillPath:
    def test_path_construction(self, generator: SkillDocGenerator) -> None:
        assert generator.get_skill_path("/mnt/calendar") == "/mnt/calendar/.skill"

    def test_path_with_trailing_slash(self, generator: SkillDocGenerator) -> None:
        assert generator.get_skill_path("/mnt/calendar/") == "/mnt/calendar/.skill"

    def test_custom_skill_dir(self) -> None:
        gen = SkillDocGenerator(
            skill_name="test",
            schemas={},
            operation_traits={},
            error_registry={},
            examples={},
            skill_dir=".docs",
        )
        assert gen.get_skill_path("/mnt/x") == "/mnt/x/.docs"

    def test_root_mount(self, generator: SkillDocGenerator) -> None:
        # posixpath.join("", ".skill") = ".skill" after rstrip("/") on "/"
        assert generator.get_skill_path("/") == ".skill"
