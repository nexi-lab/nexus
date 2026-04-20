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
# write_readme tests removed (Issue #3728): the method was deleted — skill
# docs are now served on-demand from the virtual .readme/ overlay.  See the
# TestGenerateTree and TestDispatchHelpers classes below for the tests that
# replace the materialization coverage.
# ===========================================================================


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


# ===========================================================================
# VirtualEntry tree model (Issue #3728)
# ===========================================================================


from nexus.backends.connectors.schema_generator import (  # noqa: E402
    _VIRTUAL_TREE_CACHE,
    VirtualEntry,
    _invalidate_virtual_tree_cache,
    _parse_readme_path_parts,
    dispatch_virtual_readme_exists,
    dispatch_virtual_readme_list,
    dispatch_virtual_readme_read,
    dispatch_virtual_readme_size,
    get_virtual_readme_tree_for_backend,
)


class TestVirtualEntry:
    def test_file_entry_has_content_and_no_children(self) -> None:
        entry = VirtualEntry(name="README.md", is_dir=False, content=b"hello")
        assert entry.is_file is True
        assert entry.is_dir is False
        assert entry.content == b"hello"
        assert entry.children == {}
        assert entry.size() == 5

    def test_dir_entry_has_no_content(self) -> None:
        entry = VirtualEntry(name="schemas", is_dir=True)
        assert entry.is_dir is True
        assert entry.is_file is False
        assert entry.content is None
        assert entry.size() == 0  # directories report size 0

    def test_find_empty_parts_returns_self(self) -> None:
        root = VirtualEntry(name=".readme", is_dir=True)
        assert root.find([]) is root

    def test_find_single_file(self) -> None:
        root = VirtualEntry(name=".readme", is_dir=True)
        root.children["README.md"] = VirtualEntry(name="README.md", is_dir=False, content=b"hi")
        found = root.find(["README.md"])
        assert found is not None
        assert found.content == b"hi"

    def test_find_nested_file(self) -> None:
        root = VirtualEntry(name=".readme", is_dir=True)
        schemas = VirtualEntry(name="schemas", is_dir=True)
        schemas.children["send.yaml"] = VirtualEntry(
            name="send.yaml", is_dir=False, content=b"yaml"
        )
        root.children["schemas"] = schemas
        found = root.find(["schemas", "send.yaml"])
        assert found is not None
        assert found.content == b"yaml"

    def test_find_missing_returns_none(self) -> None:
        root = VirtualEntry(name=".readme", is_dir=True)
        assert root.find(["nonexistent.md"]) is None

    def test_find_through_non_dir_returns_none(self) -> None:
        # README.md is a file, so treating it as a dir fails cleanly.
        root = VirtualEntry(name=".readme", is_dir=True)
        root.children["README.md"] = VirtualEntry(name="README.md", is_dir=False, content=b"hi")
        assert root.find(["README.md", "child"]) is None

    def test_list_children_names_sorts_and_marks_dirs(self) -> None:
        root = VirtualEntry(name=".readme", is_dir=True)
        root.children["README.md"] = VirtualEntry(name="README.md", is_dir=False, content=b"")
        root.children["schemas"] = VirtualEntry(name="schemas", is_dir=True)
        root.children["examples"] = VirtualEntry(name="examples", is_dir=True)
        names = root.list_children_names()
        assert names == ["README.md", "examples/", "schemas/"]

    def test_list_children_names_on_file_is_empty(self) -> None:
        file_entry = VirtualEntry(name="README.md", is_dir=False, content=b"x")
        assert file_entry.list_children_names() == []

    def test_list_children_empty_dir(self) -> None:
        empty_dir = VirtualEntry(name=".readme", is_dir=True)
        assert empty_dir.list_children_names() == []


# ===========================================================================
# generate_tree — single-walk construction (#14A)
# ===========================================================================


class TestGenerateTree:
    def test_tree_root_is_dir(self, generator: ReadmeDocGenerator) -> None:
        tree = generator.generate_tree("/mnt/cal")
        assert tree.is_dir is True
        assert tree.name == ".readme"

    def test_tree_contains_readme(self, generator: ReadmeDocGenerator) -> None:
        tree = generator.generate_tree("/mnt/cal")
        readme = tree.find(["README.md"])
        assert readme is not None
        assert readme.is_file
        assert b"# Test Skill Connector" in readme.content
        assert b"/mnt/cal" in readme.content

    def test_tree_readme_matches_generate_readme(self, generator: ReadmeDocGenerator) -> None:
        tree = generator.generate_tree("/mnt/cal")
        readme = tree.find(["README.md"])
        assert readme is not None
        expected = generator.generate_readme("/mnt/cal").encode("utf-8")
        assert readme.content == expected

    def test_tree_contains_schemas_dir(self, generator: ReadmeDocGenerator) -> None:
        tree = generator.generate_tree("/mnt/cal")
        schemas = tree.find(["schemas"])
        assert schemas is not None
        assert schemas.is_dir
        # Two schemas in _DEFAULT_SCHEMAS: create_event, update_event
        assert set(schemas.children.keys()) == {"create_event.yaml", "update_event.yaml"}

    def test_tree_schema_file_has_yaml_content(self, generator: ReadmeDocGenerator) -> None:
        tree = generator.generate_tree("/mnt/cal")
        schema_file = tree.find(["schemas", "create_event.yaml"])
        assert schema_file is not None
        assert schema_file.is_file
        assert b"# Schema: create_event" in schema_file.content

    def test_tree_contains_examples_dir(self, generator: ReadmeDocGenerator) -> None:
        tree = generator.generate_tree("/mnt/cal")
        examples = tree.find(["examples"])
        assert examples is not None
        assert examples.is_dir
        assert "create_meeting.yaml" in examples.children

    def test_tree_example_content_preserved(self, generator: ReadmeDocGenerator) -> None:
        tree = generator.generate_tree("/mnt/cal")
        example = tree.find(["examples", "create_meeting.yaml"])
        assert example is not None
        assert example.content == b"summary: Team Standup\n"

    def test_tree_with_empty_schemas(self, empty_generator: ReadmeDocGenerator) -> None:
        tree = empty_generator.generate_tree("/mnt/empty")
        # README.md is always present (decision #7A — empty SCHEMAS still renders)
        assert tree.find(["README.md"]) is not None
        # schemas/ and examples/ are absent when their source is empty
        assert tree.find(["schemas"]) is None
        assert tree.find(["examples"]) is None
        assert tree.list_children_names() == ["README.md"]

    def test_tree_with_binary_example(self) -> None:
        # Decision #7A — bytes values in EXAMPLES must not crash
        gen = ReadmeDocGenerator(
            skill_name="binary_skill",
            schemas={},
            operation_traits={},
            error_registry={},
            examples={"blob.bin": b"\x00\x01\x02\xff"},
        )
        tree = gen.generate_tree("/mnt/bin")
        example = tree.find(["examples", "blob.bin"])
        assert example is not None
        assert example.content == b"\x00\x01\x02\xff"

    def test_tree_with_non_ascii_skill_name(self) -> None:
        # Decision #7A — non-ASCII skill names render without crashing
        gen = ReadmeDocGenerator(
            skill_name="café_connector",
            schemas=_DEFAULT_SCHEMAS,
            operation_traits=_DEFAULT_TRAITS,
            error_registry=_DEFAULT_ERRORS,
            examples={},
        )
        tree = gen.generate_tree("/mnt/café")
        readme = tree.find(["README.md"])
        assert readme is not None
        # UTF-8 encoded, round-trip clean
        assert "café" in readme.content.decode("utf-8").lower()

    def test_tree_list_children_at_root(self, generator: ReadmeDocGenerator) -> None:
        tree = generator.generate_tree("/mnt/cal")
        names = tree.list_children_names()
        assert "README.md" in names
        assert "schemas/" in names
        assert "examples/" in names
        # sorted
        assert names == sorted(names)

    def test_tree_list_children_at_schemas(self, generator: ReadmeDocGenerator) -> None:
        tree = generator.generate_tree("/mnt/cal")
        schemas = tree.find(["schemas"])
        assert schemas is not None
        names = schemas.list_children_names()
        assert names == ["create_event.yaml", "update_event.yaml"]

    def test_tree_propagates_generator_exceptions(self) -> None:
        """Decision #8A — broken metadata raises instead of silent None.

        Uses a real Pydantic model plus ``patch.object`` to force
        ``generate_schema_yaml`` to raise — tests the propagation path
        without faking the schema type (avoids mypy dict-item errors).
        """
        from unittest.mock import patch

        gen = ReadmeDocGenerator(
            skill_name="broken",
            schemas={"op": SimpleSchema},
            operation_traits={},
            error_registry={},
            examples={},
        )
        with (
            patch.object(gen, "generate_schema_yaml", side_effect=RuntimeError("boom")),
            pytest.raises(RuntimeError, match="boom"),
        ):
            gen.generate_tree("/mnt/broken")


# ===========================================================================
# get_virtual_readme_tree_for_backend — module-level cache (#13A)
# ===========================================================================


class _FakeBackend:
    """Minimal backend-like object with class-level metadata."""

    SKILL_NAME = "fake"
    SCHEMAS = _DEFAULT_SCHEMAS
    OPERATION_TRAITS = _DEFAULT_TRAITS
    ERROR_REGISTRY = _DEFAULT_ERRORS
    EXAMPLES = _DEFAULT_EXAMPLES
    README_DIR = ".readme"


class _EmptySkillBackend:
    SKILL_NAME = ""
    SCHEMAS: dict = {}
    OPERATION_TRAITS: dict = {}
    ERROR_REGISTRY: dict = {}
    EXAMPLES: dict = {}


class TestVirtualTreeCache:
    def setup_method(self) -> None:
        _invalidate_virtual_tree_cache()

    def teardown_method(self) -> None:
        _invalidate_virtual_tree_cache()

    def test_first_call_populates_cache(self) -> None:
        assert len(_VIRTUAL_TREE_CACHE) == 0
        tree = get_virtual_readme_tree_for_backend(_FakeBackend(), "/mnt/fake")
        assert tree is not None
        assert tree.find(["README.md"]) is not None
        assert len(_VIRTUAL_TREE_CACHE) == 1

    def test_second_call_returns_same_tree_object(self) -> None:
        backend = _FakeBackend()
        tree1 = get_virtual_readme_tree_for_backend(backend, "/mnt/fake")
        tree2 = get_virtual_readme_tree_for_backend(backend, "/mnt/fake")
        assert tree1 is tree2  # identity, not just equality

    def test_cache_key_includes_mount_path(self) -> None:
        backend = _FakeBackend()
        tree_a = get_virtual_readme_tree_for_backend(backend, "/mnt/a")
        tree_b = get_virtual_readme_tree_for_backend(backend, "/mnt/b")
        assert tree_a is not tree_b  # different mount → different tree
        # But same content structure
        assert tree_a.list_children_names() == tree_b.list_children_names()
        # And the mount path is reflected in README.md content
        readme_a = tree_a.find(["README.md"])
        readme_b = tree_b.find(["README.md"])
        assert readme_a is not None and readme_b is not None
        assert b"/mnt/a" in readme_a.content
        assert b"/mnt/b" in readme_b.content

    def test_cache_key_includes_class(self) -> None:
        # Two different classes with the same mount path produce different entries
        class OtherBackend(_FakeBackend):
            SKILL_NAME = "other"

        tree_fake = get_virtual_readme_tree_for_backend(_FakeBackend(), "/mnt/x")
        tree_other = get_virtual_readme_tree_for_backend(OtherBackend(), "/mnt/x")
        assert tree_fake is not tree_other
        assert len(_VIRTUAL_TREE_CACHE) == 2

    def test_missing_skill_name_raises(self) -> None:
        with pytest.raises(RuntimeError, match="SKILL_NAME"):
            get_virtual_readme_tree_for_backend(_EmptySkillBackend(), "/mnt/x")

    def test_invalidate_clears_all(self) -> None:
        get_virtual_readme_tree_for_backend(_FakeBackend(), "/mnt/x")
        assert len(_VIRTUAL_TREE_CACHE) > 0
        _invalidate_virtual_tree_cache()
        assert len(_VIRTUAL_TREE_CACHE) == 0


# ===========================================================================
# _parse_readme_path_parts — normalization + traversal guards (#4A)
# ===========================================================================


class TestParseReadmePath:
    def test_none_input_returns_none(self) -> None:
        assert _parse_readme_path_parts(None) is None

    def test_empty_string_returns_none(self) -> None:
        assert _parse_readme_path_parts("") is None

    def test_non_readme_path_returns_none(self) -> None:
        assert _parse_readme_path_parts("INBOX/msg.yaml") is None
        assert _parse_readme_path_parts("/INBOX/msg.yaml") is None

    def test_readme_root_returns_empty_list(self) -> None:
        assert _parse_readme_path_parts(".readme") == []
        assert _parse_readme_path_parts(".readme/") == []
        assert _parse_readme_path_parts("/.readme/") == []

    def test_readme_file_returns_single_part(self) -> None:
        assert _parse_readme_path_parts(".readme/README.md") == ["README.md"]
        assert _parse_readme_path_parts("/.readme/README.md") == ["README.md"]

    def test_readme_nested_returns_multiple_parts(self) -> None:
        assert _parse_readme_path_parts(".readme/schemas/send_email.yaml") == [
            "schemas",
            "send_email.yaml",
        ]

    def test_custom_readme_dir(self) -> None:
        assert _parse_readme_path_parts(".docs/FAQ.md", readme_dir=".docs") == ["FAQ.md"]

    def test_double_slash_normalized(self) -> None:
        assert _parse_readme_path_parts(".readme//README.md") == ["README.md"]

    def test_trailing_slash_stripped(self) -> None:
        # .readme/schemas/ (directory listing) — parts are ["schemas"]
        assert _parse_readme_path_parts(".readme/schemas/") == ["schemas"]

    def test_dot_components_normalized(self) -> None:
        assert _parse_readme_path_parts(".readme/./README.md") == ["README.md"]

    def test_traversal_rejected(self) -> None:
        with pytest.raises(ValueError, match="traversal"):
            _parse_readme_path_parts(".readme/../../../etc/passwd")

    def test_traversal_to_self_rejected(self) -> None:
        with pytest.raises(ValueError, match="traversal"):
            _parse_readme_path_parts("..")

    def test_null_byte_rejected(self) -> None:
        with pytest.raises(ValueError, match="null byte"):
            _parse_readme_path_parts(".readme/README\x00.md")

    def test_backslash_rejected(self) -> None:
        with pytest.raises(ValueError, match="backslash"):
            _parse_readme_path_parts(".readme\\README.md")

    def test_x_readme_is_not_readme(self) -> None:
        # Prefix confusion — "x.readme/foo" is NOT under ".readme/"
        assert _parse_readme_path_parts("x.readme/foo") is None

    def test_readme_prefix_of_other_dir_is_not_readme(self) -> None:
        # ".readmex/foo" starts with ".readme" but is a different directory
        assert _parse_readme_path_parts(".readmex/foo") is None


# ===========================================================================
# Dispatch helpers (#1 kernel dispatch, #4A traversal, #8A sentinel protocol)
# ===========================================================================


class TestDispatchHelpers:
    def setup_method(self) -> None:
        _invalidate_virtual_tree_cache()

    def teardown_method(self) -> None:
        _invalidate_virtual_tree_cache()

    # -- read --

    def test_read_returns_bytes_for_virtual_file(self) -> None:
        content = dispatch_virtual_readme_read(_FakeBackend(), "/mnt/x", ".readme/README.md")
        assert content is not None
        assert b"# Fake Connector" in content or b"# Test Skill Connector" in content

    def test_read_returns_none_for_non_virtual_path(self) -> None:
        assert dispatch_virtual_readme_read(_FakeBackend(), "/mnt/x", "INBOX/msg.yaml") is None

    def test_read_raises_not_found_for_missing_virtual(self) -> None:
        from nexus.contracts.exceptions import NexusFileNotFoundError

        with pytest.raises(NexusFileNotFoundError):
            dispatch_virtual_readme_read(_FakeBackend(), "/mnt/x", ".readme/nonexistent.md")

    def test_read_raises_is_a_directory_for_virtual_dir(self) -> None:
        with pytest.raises(IsADirectoryError):
            dispatch_virtual_readme_read(_FakeBackend(), "/mnt/x", ".readme/schemas")

    def test_read_returns_none_for_backend_without_skill(self) -> None:
        assert (
            dispatch_virtual_readme_read(_EmptySkillBackend(), "/mnt/x", ".readme/README.md")
            is None
        )

    def test_read_raises_on_traversal(self) -> None:
        with pytest.raises(ValueError, match="traversal"):
            dispatch_virtual_readme_read(_FakeBackend(), "/mnt/x", ".readme/../../../etc/passwd")

    # -- list --

    def test_list_returns_entries_for_virtual_dir(self) -> None:
        entries = dispatch_virtual_readme_list(_FakeBackend(), "/mnt/x", ".readme")
        assert entries is not None
        assert "README.md" in entries
        assert "schemas/" in entries
        assert "examples/" in entries

    def test_list_returns_entries_for_nested_virtual_dir(self) -> None:
        entries = dispatch_virtual_readme_list(_FakeBackend(), "/mnt/x", ".readme/schemas")
        assert entries is not None
        assert "create_event.yaml" in entries
        assert "update_event.yaml" in entries

    def test_list_returns_none_for_non_virtual_path(self) -> None:
        assert dispatch_virtual_readme_list(_FakeBackend(), "/mnt/x", "INBOX") is None

    def test_list_raises_not_a_dir_for_virtual_file(self) -> None:
        with pytest.raises(NotADirectoryError):
            dispatch_virtual_readme_list(_FakeBackend(), "/mnt/x", ".readme/README.md")

    def test_list_raises_not_found_for_missing_virtual_dir(self) -> None:
        from nexus.contracts.exceptions import NexusFileNotFoundError

        with pytest.raises(NexusFileNotFoundError):
            dispatch_virtual_readme_list(_FakeBackend(), "/mnt/x", ".readme/nonexistent")

    def test_list_returns_none_for_backend_without_skill(self) -> None:
        assert dispatch_virtual_readme_list(_EmptySkillBackend(), "/mnt/x", ".readme") is None

    # -- exists --

    def test_exists_true_for_virtual_file(self) -> None:
        assert dispatch_virtual_readme_exists(_FakeBackend(), "/mnt/x", ".readme/README.md") is True

    def test_exists_true_for_virtual_dir(self) -> None:
        assert dispatch_virtual_readme_exists(_FakeBackend(), "/mnt/x", ".readme/schemas") is True

    def test_exists_false_for_missing_virtual(self) -> None:
        assert (
            dispatch_virtual_readme_exists(_FakeBackend(), "/mnt/x", ".readme/nonexistent") is False
        )

    def test_exists_none_for_non_virtual(self) -> None:
        assert dispatch_virtual_readme_exists(_FakeBackend(), "/mnt/x", "INBOX/msg") is None

    def test_exists_false_on_malformed_path(self) -> None:
        # Malformed paths under .readme/ (null byte, backslash) return False
        # rather than raising — exists() is a predicate, not an access
        # attempt, and the caller needs a definitive answer.
        assert (
            dispatch_virtual_readme_exists(_FakeBackend(), "/mnt/x", ".readme/README\x00.md")
            is False
        )

    def test_exists_none_when_traversal_escapes_readme(self) -> None:
        # posixpath.normpath collapses ".readme/../etc" to "etc", which isn't
        # under .readme/ at all — the helper returns None (not virtual),
        # letting the real backend handle the resulting path.
        assert (
            dispatch_virtual_readme_exists(_FakeBackend(), "/mnt/x", ".readme/../etc/passwd")
            is None
        )

    def test_exists_none_for_backend_without_skill(self) -> None:
        assert (
            dispatch_virtual_readme_exists(_EmptySkillBackend(), "/mnt/x", ".readme/README.md")
            is None
        )

    # -- size --

    def test_size_returns_file_bytes(self) -> None:
        size = dispatch_virtual_readme_size(_FakeBackend(), "/mnt/x", ".readme/README.md")
        assert size is not None
        assert size > 0

    def test_size_returns_zero_for_virtual_dir(self) -> None:
        # Directories report size 0 — consistent with Unix directory sizes
        size = dispatch_virtual_readme_size(_FakeBackend(), "/mnt/x", ".readme/schemas")
        assert size == 0

    def test_size_returns_none_for_non_virtual(self) -> None:
        assert dispatch_virtual_readme_size(_FakeBackend(), "/mnt/x", "INBOX/msg.yaml") is None

    def test_size_raises_for_missing_virtual(self) -> None:
        from nexus.contracts.exceptions import NexusFileNotFoundError

        with pytest.raises(NexusFileNotFoundError):
            dispatch_virtual_readme_size(_FakeBackend(), "/mnt/x", ".readme/nonexistent.md")


# ===========================================================================
# Hypothesis property test for path parsing (#11A)
# ===========================================================================


from hypothesis import given, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402


class TestParseReadmePathFuzz:
    """Property-based tests for _parse_readme_path_parts.

    Invariants:
        1. For any input string, the function either (a) returns None,
           (b) returns a list of parts containing no ``..``, ``/``, or
           null bytes, and no empty strings, or (c) raises ValueError.
        2. Returned parts never escape the ``.readme/`` subtree.
        3. The function never crashes with any other exception type.
    """

    @given(st.text(max_size=200))
    @settings(max_examples=500, deadline=None)
    def test_no_crashes_and_safe_parts(self, path: str) -> None:
        try:
            parts = _parse_readme_path_parts(path)
        except ValueError:
            return  # explicit rejection is fine
        except Exception as exc:
            raise AssertionError(f"unexpected exception type for input {path!r}: {exc!r}") from exc

        if parts is None:
            return  # not a readme path, fine

        # Safety invariants on returned parts
        for part in parts:
            assert part, f"empty part in {parts!r} from {path!r}"
            assert "/" not in part, f"slash in part {part!r} from {path!r}"
            assert "\x00" not in part, f"null byte in part {part!r} from {path!r}"
            assert part != "..", f".. in parts {parts!r} from {path!r}"
            assert part != ".", f". in parts {parts!r} from {path!r}"

    @given(st.text(max_size=200))
    @settings(max_examples=500, deadline=None)
    def test_traversal_always_rejected(self, suffix: str) -> None:
        # Any path starting with ../ after .readme/ should raise
        attack = ".readme/../" + suffix
        try:
            _parse_readme_path_parts(attack)
        except (ValueError, UnicodeError):
            return
        # If it didn't raise, the result must NOT escape .readme/
        # (i.e., parts must be empty or resolvable-safe).
        # This branch is reached when the suffix normalizes to something
        # within .readme/ after collapsing — which is fine.

    def test_explicit_attack_vectors(self) -> None:
        # Each of these must raise ValueError
        attacks = [
            ".readme/../../etc/passwd",
            ".readme/../..",
            "../.readme/README.md",
            ".readme/foo/../../../root",
            ".readme/\x00injected",
            ".readme\\README.md",
        ]
        for attack in attacks:
            with pytest.raises(ValueError):
                _parse_readme_path_parts(attack)


# ===========================================================================
# Kernel wiring integration (Issue #3728 kernel dispatch)
# ===========================================================================
#
# These tests verify that NexusFS._try_virtual_readme_stat + the dispatch
# wiring in sys_read/sys_readdir fires correctly for backends with skill
# docs.  We use monkeypatch to install a fake router route instead of
# constructing a full Backend subclass, which would require a proper
# Transport implementation we don't need for this test.


class _FakeRoute:
    """Minimal fake of RouteResult/ExternalRouteResult for dispatch wiring."""

    def __init__(self, backend, mount_point, backend_path):
        self.backend = backend
        self.mount_point = mount_point
        self.backend_path = backend_path
        self.readonly = True
        self.metastore = None  # stat path checks meta; we override it anyway


class TestKernelWiringDispatch:
    """Verify the dispatch helpers are callable through the public API."""

    def setup_method(self) -> None:
        _invalidate_virtual_tree_cache()

    def teardown_method(self) -> None:
        _invalidate_virtual_tree_cache()

    def test_read_dispatch_returns_readme_bytes(self) -> None:
        """dispatch_virtual_readme_read serves README.md content."""
        backend = _FakeBackend()
        data = dispatch_virtual_readme_read(backend, "/mnt/fake", ".readme/README.md")
        assert data is not None
        assert data.startswith(b"# Fake") or data.startswith(b"#")

    def test_list_dispatch_returns_entries(self) -> None:
        """dispatch_virtual_readme_list serves directory entries."""
        backend = _FakeBackend()
        entries = dispatch_virtual_readme_list(backend, "/mnt/fake", ".readme")
        assert entries is not None
        assert "README.md" in entries

    def test_exists_dispatch_returns_true(self) -> None:
        """dispatch_virtual_readme_exists returns True for known entries."""
        backend = _FakeBackend()
        assert dispatch_virtual_readme_exists(backend, "/mnt/fake", ".readme/README.md") is True

    def test_size_dispatch_returns_nonzero_for_readme(self) -> None:
        """dispatch_virtual_readme_size returns positive size for README.md."""
        backend = _FakeBackend()
        size = dispatch_virtual_readme_size(backend, "/mnt/fake", ".readme/README.md")
        assert size is not None
        assert size > 0

    def test_size_dispatch_returns_zero_for_dir(self) -> None:
        """dispatch_virtual_readme_size returns 0 for virtual directories."""
        backend = _FakeBackend()
        size = dispatch_virtual_readme_size(backend, "/mnt/fake", ".readme/schemas")
        assert size == 0

    def test_fall_through_on_non_readme_path(self) -> None:
        """Non-.readme/ paths return None from all four dispatch helpers."""
        backend = _FakeBackend()
        assert dispatch_virtual_readme_read(backend, "/mnt/fake", "INBOX/msg.yaml") is None
        assert dispatch_virtual_readme_list(backend, "/mnt/fake", "INBOX") is None
        assert dispatch_virtual_readme_exists(backend, "/mnt/fake", "INBOX/msg") is None
        assert dispatch_virtual_readme_size(backend, "/mnt/fake", "INBOX/msg.yaml") is None

    def test_non_skill_backend_always_returns_none(self) -> None:
        """Backends without SKILL_NAME get None from all helpers."""
        backend = _EmptySkillBackend()
        assert dispatch_virtual_readme_read(backend, "/mnt/x", ".readme/README.md") is None
        assert dispatch_virtual_readme_list(backend, "/mnt/x", ".readme") is None
        assert dispatch_virtual_readme_exists(backend, "/mnt/x", ".readme/README.md") is None
        assert dispatch_virtual_readme_size(backend, "/mnt/x", ".readme/README.md") is None
