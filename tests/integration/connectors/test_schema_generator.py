"""Tests for ReadmeDocGenerator — schema-to-doc generation extracted from ReadmeDocMixin."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import BaseModel

from nexus.backends.connectors.base import ConfirmLevel, ErrorDef, OpTraits, Reversibility
from nexus.backends.connectors.schema_generator import ReadmeDocGenerator

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
        readme_section="required-format",
        fix_example="# agent_intent: User requested meeting",
    ),
}

_DEFAULT_EXAMPLES: dict[str, str] = {
    "create_meeting.yaml": "summary: Team Standup\n",
}


@pytest.fixture()
def generator() -> ReadmeDocGenerator:
    return ReadmeDocGenerator(
        skill_name="test_skill",
        schemas=_DEFAULT_SCHEMAS,
        operation_traits=_DEFAULT_TRAITS,
        error_registry=_DEFAULT_ERRORS,
        examples=_DEFAULT_EXAMPLES,
    )


@pytest.fixture()
def empty_generator() -> ReadmeDocGenerator:
    """Generator with no schemas, traits, or errors."""
    return ReadmeDocGenerator(
        skill_name="empty_skill",
        schemas={},
        operation_traits={},
        error_registry={},
        examples={},
    )


@pytest.fixture()
def mock_filesystem() -> MagicMock:
    fs = MagicMock()
    fs.mkdir = AsyncMock()
    fs.write = AsyncMock()
    return fs


# ===========================================================================
# generate_readme
# ===========================================================================


class TestGenerateReadme:
    def test_structure(self, generator: ReadmeDocGenerator) -> None:
        doc = generator.generate_readme("/mnt/calendar")
        assert "# Test Skill Connector" in doc
        assert "## Mount Path" in doc
        assert "`/mnt/calendar`" in doc
        assert "## Operations" in doc
        assert "## Required Format" in doc
        assert "## Error Codes" in doc

    def test_sections_present_with_full_config(self, generator: ReadmeDocGenerator) -> None:
        doc = generator.generate_readme("/mnt/cal")
        # Operations section lists each schema operation
        assert "### Create Event" in doc
        assert "### Update Event" in doc
        # Error Codes section lists each error
        assert "### MISSING_AGENT_INTENT" in doc

    def test_empty_schemas_omits_operations(self, empty_generator: ReadmeDocGenerator) -> None:
        doc = empty_generator.generate_readme("/mnt/empty")
        assert "## Operations" not in doc

    def test_empty_traits_omits_required_format(self, empty_generator: ReadmeDocGenerator) -> None:
        doc = empty_generator.generate_readme("/mnt/empty")
        assert "## Required Format" not in doc

    def test_empty_errors_omits_error_codes(self, empty_generator: ReadmeDocGenerator) -> None:
        doc = empty_generator.generate_readme("/mnt/empty")
        assert "## Error Codes" not in doc

    def test_empty_config_has_header_and_mount(self, empty_generator: ReadmeDocGenerator) -> None:
        doc = empty_generator.generate_readme("/mnt/empty")
        assert "# Empty Skill Connector" in doc
        assert "## Mount Path" in doc
        assert "`/mnt/empty`" in doc

    def test_display_name_formatting(self) -> None:
        gen = ReadmeDocGenerator(
            skill_name="my-cool_skill",
            schemas={},
            operation_traits={},
            error_registry={},
            examples={},
        )
        doc = gen.generate_readme("/mnt/x")
        assert "# My Cool Skill Connector" in doc


# ===========================================================================
# write_readme
# ===========================================================================


class TestWriteReadme:
    @pytest.mark.asyncio
    async def test_writes_readme_md(
        self, generator: ReadmeDocGenerator, mock_filesystem: MagicMock
    ) -> None:
        result = await generator.write_readme("/mnt/calendar", filesystem=mock_filesystem)

        assert result["readme_md"] == "/mnt/calendar/.readme/README.md"
        mock_filesystem.mkdir.assert_any_call("/mnt/calendar/.readme", parents=True, exist_ok=True)
        # README.md is written as bytes
        write_calls = mock_filesystem.write.call_args_list
        readme_md_call = write_calls[0]
        assert readme_md_call[0][0] == "/mnt/calendar/.readme/README.md"
        assert isinstance(readme_md_call[0][1], bytes)

    @pytest.mark.asyncio
    async def test_writes_examples(
        self, generator: ReadmeDocGenerator, mock_filesystem: MagicMock
    ) -> None:
        result = await generator.write_readme("/mnt/calendar", filesystem=mock_filesystem)

        assert "/mnt/calendar/.readme/examples/create_meeting.yaml" in result["examples"]
        mock_filesystem.mkdir.assert_any_call(
            "/mnt/calendar/.readme/examples", parents=True, exist_ok=True
        )
        # Verify example content written
        example_call = mock_filesystem.write.call_args_list[1]
        assert example_call[0][0] == "/mnt/calendar/.readme/examples/create_meeting.yaml"
        assert example_call[0][1] == b"summary: Team Standup\n"

    @pytest.mark.asyncio
    async def test_no_filesystem_returns_empty_result(self, generator: ReadmeDocGenerator) -> None:
        result = await generator.write_readme("/mnt/calendar", filesystem=None)
        assert result["readme_md"] is None
        assert result["examples"] == []

    @pytest.mark.asyncio
    async def test_empty_skill_name_returns_empty_result(self, mock_filesystem: MagicMock) -> None:
        gen = ReadmeDocGenerator(
            skill_name="",
            schemas={},
            operation_traits={},
            error_registry={},
            examples={},
        )
        result = await gen.write_readme("/mnt/x", filesystem=mock_filesystem)
        assert result["readme_md"] is None
        assert result["examples"] == []
        mock_filesystem.write.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_examples_skips_examples_dir(self, mock_filesystem: MagicMock) -> None:
        gen = ReadmeDocGenerator(
            skill_name="test",
            schemas=_DEFAULT_SCHEMAS,
            operation_traits=_DEFAULT_TRAITS,
            error_registry=_DEFAULT_ERRORS,
            examples={},
        )
        result = await gen.write_readme("/mnt/x", filesystem=mock_filesystem)
        assert result["readme_md"] is not None
        assert result["examples"] == []
        # Only the .readme dir mkdir, not examples/
        mkdir_paths = [c[0][0] for c in mock_filesystem.mkdir.call_args_list]
        assert "/mnt/x/.readme/examples" not in mkdir_paths

    @pytest.mark.asyncio
    async def test_filesystem_error_returns_partial_result(
        self, mock_filesystem: MagicMock
    ) -> None:
        mock_filesystem.mkdir.side_effect = OSError("permission denied")
        gen = ReadmeDocGenerator(
            skill_name="test",
            schemas={},
            operation_traits={},
            error_registry={},
            examples={},
        )
        result = await gen.write_readme("/mnt/x", filesystem=mock_filesystem)
        assert result["readme_md"] is None
        assert result["examples"] == []

    @pytest.mark.asyncio
    async def test_custom_readme_dir(self, mock_filesystem: MagicMock) -> None:
        gen = ReadmeDocGenerator(
            skill_name="test",
            schemas={},
            operation_traits={},
            error_registry={},
            examples={},
            readme_dir=".docs",
        )
        await gen.write_readme("/mnt/x", filesystem=mock_filesystem)
        mock_filesystem.mkdir.assert_any_call("/mnt/x/.docs", parents=True, exist_ok=True)


# ===========================================================================
# _schema_to_yaml_lines (migrated from TestSchemaToYamlLines)
# ===========================================================================


class TestSchemaToYamlLines:
    def test_simple_fields(self, generator: ReadmeDocGenerator) -> None:
        lines = generator._schema_to_yaml_lines(SimpleSchema)
        text = "\n".join(lines)
        assert "summary:" in text
        assert "count:" in text
        assert "active:" in text

    def test_optional_fields(self, generator: ReadmeDocGenerator) -> None:
        lines = generator._schema_to_yaml_lines(OptionalSchema)
        text = "\n".join(lines)
        # Fields with defaults should show the default
        assert "description: default description" in text
        assert "color_id: 1" in text
        assert "notify: false" in text

    def test_nested_model(self, generator: ReadmeDocGenerator) -> None:
        lines = generator._schema_to_yaml_lines(NestedSchema)
        text = "\n".join(lines)
        assert "start:" in text
        assert "end:" in text
        # Nested example lines should be indented
        assert "  " in text  # at least some indented content

    def test_list_field(self, generator: ReadmeDocGenerator) -> None:
        lines = generator._schema_to_yaml_lines(ListSchema)
        text = "\n".join(lines)
        # tags has default [] so should show []
        assert "tags: []" in text

    def test_attendees_list_field(self, generator: ReadmeDocGenerator) -> None:
        lines = generator._schema_to_yaml_lines(ListSchema)
        text = "\n".join(lines)
        # attendees is a required list[str] with no default
        assert "attendees:" in text

    def test_skips_agent_intent_and_confirm(self) -> None:
        class SchemaWithMeta(BaseModel):
            agent_intent: str = ""
            confirm: bool = False
            real_field: str

        gen = ReadmeDocGenerator(
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
    def test_pydantic_model(self, generator: ReadmeDocGenerator) -> None:
        assert generator._is_nested_model(NestedChild) is True

    def test_primitive(self, generator: ReadmeDocGenerator) -> None:
        assert generator._is_nested_model(str) is False
        assert generator._is_nested_model(int) is False

    def test_optional_model(self, generator: ReadmeDocGenerator) -> None:
        # NestedChild | None should still be detected as nested
        assert generator._is_nested_model(NestedChild | None) is True

    def test_optional_primitive(self, generator: ReadmeDocGenerator) -> None:
        assert generator._is_nested_model(str | None) is False

    def test_bool_not_nested(self, generator: ReadmeDocGenerator) -> None:
        assert generator._is_nested_model(bool) is False

    def test_list_not_nested(self, generator: ReadmeDocGenerator) -> None:
        assert generator._is_nested_model(list[str]) is False

    def test_none_annotation(self, generator: ReadmeDocGenerator) -> None:
        assert generator._is_nested_model(None) is False


# ===========================================================================
# _format_type_hint (migrated from TestFormatTypeHint)
# ===========================================================================


class TestFormatTypeHint:
    def test_str(self, generator: ReadmeDocGenerator) -> None:
        assert generator._format_type_hint(str) == "string"

    def test_int(self, generator: ReadmeDocGenerator) -> None:
        assert generator._format_type_hint(int) == "integer"

    def test_bool(self, generator: ReadmeDocGenerator) -> None:
        assert generator._format_type_hint(bool) == "boolean"

    def test_list(self, generator: ReadmeDocGenerator) -> None:
        assert generator._format_type_hint(list) == "list"

    def test_dict(self, generator: ReadmeDocGenerator) -> None:
        assert generator._format_type_hint(dict) == "object"

    def test_none_returns_any(self, generator: ReadmeDocGenerator) -> None:
        assert generator._format_type_hint(None) == "any"


# ===========================================================================
# _generate_errors_section (migrated from TestGenerateErrorsSection)
# ===========================================================================


class TestGenerateErrorsSection:
    def test_errors_section(self, generator: ReadmeDocGenerator) -> None:
        lines = generator._generate_errors_section()
        text = "\n".join(lines)
        assert "## Error Codes" in text
        assert "### MISSING_AGENT_INTENT" in text
        assert "Operations require agent_intent" in text
        assert "# agent_intent: User requested meeting" in text

    def test_error_without_fix_example(self) -> None:
        gen = ReadmeDocGenerator(
            skill_name="test",
            schemas={},
            operation_traits={},
            error_registry={
                "SOME_ERROR": ErrorDef(
                    message="Something went wrong",
                    readme_section="operations",
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
        gen = ReadmeDocGenerator(
            skill_name="test",
            schemas={},
            operation_traits={},
            error_registry={
                "FIX_ME": ErrorDef(
                    message="Broken",
                    readme_section="ops",
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
        gen = ReadmeDocGenerator(
            skill_name="test",
            schemas={},
            operation_traits={},
            error_registry={},
            examples={},
            field_examples={"summary": '"Meeting Title"'},
        )
        result = gen._get_field_example("summary", None, str, True)
        assert result == '"Meeting Title"'

    def test_unknown_field(self, generator: ReadmeDocGenerator) -> None:
        result = generator._get_field_example("custom_field", None, str, True)
        assert "string" in result
        assert "required" in result

    def test_unknown_optional_field(self, generator: ReadmeDocGenerator) -> None:
        result = generator._get_field_example("custom_field", None, str, False)
        assert "string" in result
        assert "optional" in result

    def test_bool_field_returns_true(self, generator: ReadmeDocGenerator) -> None:
        result = generator._get_field_example("flag", None, bool, True)
        assert result == "true"

    def test_int_field_returns_zero(self, generator: ReadmeDocGenerator) -> None:
        result = generator._get_field_example("count", None, int, True)
        assert result == "0"

    def test_list_field_returns_empty_list(self, generator: ReadmeDocGenerator) -> None:
        result = generator._get_field_example("items", None, list, True)
        assert result == "[]"

    def test_field_examples_override_type_based(self) -> None:
        """field_examples should override even type-based defaults."""
        gen = ReadmeDocGenerator(
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
        gen = ReadmeDocGenerator(
            skill_name="test",
            schemas={},
            operation_traits={},
            error_registry={},
            examples={},
            nested_examples={"start": ['dateTime: "2024-01-01T09:00:00"', 'timeZone: "UTC"']},
        )
        lines = gen._get_nested_example("start", NestedChild, required=True)
        assert lines == ['dateTime: "2024-01-01T09:00:00"', 'timeZone: "UTC"']

    def test_fallback_required(self, generator: ReadmeDocGenerator) -> None:
        lines = generator._get_nested_example("start", NestedChild, required=True)
        assert len(lines) == 1
        assert "nested object" in lines[0]
        assert "required" in lines[0]

    def test_fallback_optional(self, generator: ReadmeDocGenerator) -> None:
        lines = generator._get_nested_example("start", NestedChild, required=False)
        assert len(lines) == 1
        assert "nested object" in lines[0]
        assert "optional" in lines[0]

    def test_returns_copy_not_original(self) -> None:
        """Returned list should be a copy so callers cannot mutate the config."""
        originals = ["a: 1", "b: 2"]
        gen = ReadmeDocGenerator(
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
# get_readme_path
# ===========================================================================


class TestGetSkillPath:
    def test_path_construction(self, generator: ReadmeDocGenerator) -> None:
        assert generator.get_readme_path("/mnt/calendar") == "/mnt/calendar/.readme"

    def test_path_with_trailing_slash(self, generator: ReadmeDocGenerator) -> None:
        assert generator.get_readme_path("/mnt/calendar/") == "/mnt/calendar/.readme"

    def test_custom_readme_dir(self) -> None:
        gen = ReadmeDocGenerator(
            skill_name="test",
            schemas={},
            operation_traits={},
            error_registry={},
            examples={},
            readme_dir=".docs",
        )
        assert gen.get_readme_path("/mnt/x") == "/mnt/x/.docs"

    def test_root_mount(self, generator: ReadmeDocGenerator) -> None:
        # posixpath.join("", ".readme") = ".readme" after rstrip("/") on "/"
        assert generator.get_readme_path("/") == ".readme"
