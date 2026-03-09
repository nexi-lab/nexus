"""Tests for CLI → MCP tool export (nexus mcp export-tools)."""

from __future__ import annotations

import json
from typing import Any

import click

from nexus.cli.export_tools import (
    click_type_to_json_schema,
    command_to_tool,
    walk_click_tree,
)

# ---------------------------------------------------------------------------
# Unit tests — click_type_to_json_schema
# ---------------------------------------------------------------------------


class TestClickTypeToJsonSchema:
    def test_string_type(self) -> None:
        assert click_type_to_json_schema(click.STRING) == {"type": "string"}

    def test_int_type(self) -> None:
        assert click_type_to_json_schema(click.INT) == {"type": "integer"}

    def test_float_type(self) -> None:
        assert click_type_to_json_schema(click.FLOAT) == {"type": "number"}

    def test_bool_type(self) -> None:
        assert click_type_to_json_schema(click.BOOL) == {"type": "boolean"}

    def test_choice_type(self) -> None:
        result = click_type_to_json_schema(click.Choice(["a", "b", "c"]))
        assert result == {"type": "string", "enum": ["a", "b", "c"]}

    def test_path_type(self) -> None:
        assert click_type_to_json_schema(click.Path()) == {"type": "string"}

    def test_unknown_type_defaults_to_string(self) -> None:
        assert click_type_to_json_schema(click.UNPROCESSED) == {"type": "string"}


# ---------------------------------------------------------------------------
# Unit tests — command_to_tool
# ---------------------------------------------------------------------------


class TestCommandToTool:
    def test_simple_command(self) -> None:
        @click.command(name="my-cmd")
        @click.argument("path", type=str)
        def my_cmd(path: str) -> None:
            """Do something with a file."""

        tool = command_to_tool(my_cmd, prefix="nexus")
        assert tool["name"] == "nexus_my_cmd"
        assert tool["description"] == "Do something with a file."
        assert "path" in tool["inputSchema"]["properties"]
        assert "path" in tool["inputSchema"]["required"]

    def test_command_with_options(self) -> None:
        @click.command()
        @click.argument("path", type=str)
        @click.option("--recursive", "-r", is_flag=True, help="Recurse into dirs")
        def ls(path: str, recursive: bool) -> None:
            """List files."""

        tool = command_to_tool(ls, prefix="nexus")
        props = tool["inputSchema"]["properties"]
        assert "path" in props
        assert "recursive" in props
        assert props["recursive"]["type"] == "boolean"
        assert "path" in tool["inputSchema"]["required"]
        assert "recursive" not in tool["inputSchema"]["required"]

    def test_choice_option_has_enum(self) -> None:
        @click.command()
        @click.option("--format", type=click.Choice(["json", "yaml", "toml"]))
        def export(format: str) -> None:  # noqa: A002
            """Export data."""

        tool = command_to_tool(export, prefix="nexus")
        assert tool["inputSchema"]["properties"]["format"]["enum"] == ["json", "yaml", "toml"]

    def test_flag_option_is_boolean(self) -> None:
        @click.command()
        @click.option("--force", "-f", is_flag=True, help="Don't ask")
        def rm(force: bool) -> None:
            """Remove file."""

        tool = command_to_tool(rm, prefix="nexus")
        assert tool["inputSchema"]["properties"]["force"]["type"] == "boolean"

    def test_option_with_default(self) -> None:
        @click.command()
        @click.option("--level", type=int, default=3, help="Depth")
        def tree(level: int) -> None:
            """Show tree."""

        tool = command_to_tool(tree, prefix="nexus")
        assert tool["inputSchema"]["properties"]["level"]["default"] == 3

    def test_multiple_option_is_array(self) -> None:
        @click.command()
        @click.option("--exclude", multiple=True, help="Exclude pattern")
        def cmd(exclude: tuple[str, ...]) -> None:
            """Test."""

        tool = command_to_tool(cmd, prefix="nexus")
        prop = tool["inputSchema"]["properties"]["exclude"]
        assert prop["type"] == "array"
        assert prop["items"]["type"] == "string"

    def test_filters_backend_options(self) -> None:
        from nexus.cli.utils import add_backend_options

        @click.command()
        @click.argument("path", type=str)
        @add_backend_options
        def my_cmd(path: str, remote_url: str | None, remote_api_key: str | None) -> None:
            """Test."""

        tool = command_to_tool(my_cmd, prefix="nexus")
        props = tool["inputSchema"]["properties"]
        assert "path" in props
        for excluded in (
            "remote_url",
            "remote_api_key",
        ):
            assert excluded not in props, f"{excluded} should be filtered"

    def test_filters_context_options(self) -> None:
        from nexus.cli.utils import add_context_options

        @click.command()
        @click.argument("path", type=str)
        @add_context_options
        def my_cmd(path: str, operation_context: dict[str, Any]) -> None:
            """Test."""

        tool = command_to_tool(my_cmd, prefix="nexus")
        props = tool["inputSchema"]["properties"]
        assert "path" in props
        for excluded in ("subject", "zone_id", "is_admin", "is_system", "admin_capabilities"):
            assert excluded not in props, f"{excluded} should be filtered"

    def test_filters_output_options(self) -> None:
        from nexus.cli.output import add_output_options

        @click.command()
        @click.argument("path", type=str)
        @add_output_options
        def my_cmd(path: str, output_opts: Any) -> None:
            """Test."""

        tool = command_to_tool(my_cmd, prefix="nexus")
        props = tool["inputSchema"]["properties"]
        assert "path" in props
        for excluded in ("json_output", "quiet", "verbosity", "fields"):
            assert excluded not in props, f"{excluded} should be filtered"

    def test_preserves_dry_run(self) -> None:
        from nexus.cli.dry_run import add_dry_run_option

        @click.command()
        @click.argument("path", type=str)
        @add_dry_run_option
        def my_cmd(path: str, dry_run: bool) -> None:
            """Test."""

        tool = command_to_tool(my_cmd, prefix="nexus")
        assert "dry_run" in tool["inputSchema"]["properties"]

    def test_optional_argument_not_required(self) -> None:
        @click.command()
        @click.argument("path", default="/", type=str)
        def ls(path: str) -> None:
            """List files."""

        tool = command_to_tool(ls, prefix="nexus")
        assert "path" not in tool["inputSchema"].get("required", [])

    def test_first_line_of_docstring_only(self) -> None:
        @click.command()
        def my_cmd() -> None:
            """First line summary.

            Additional details that should NOT appear.
            """

        tool = command_to_tool(my_cmd, prefix="nexus")
        assert tool["description"] == "First line summary."

    def test_hyphenated_name_becomes_underscored(self) -> None:
        @click.command(name="write-batch")
        def write_batch() -> None:
            """Batch write."""

        tool = command_to_tool(write_batch, prefix="nexus")
        assert tool["name"] == "nexus_write_batch"

    def test_file_param_skipped(self) -> None:
        @click.command()
        @click.option("--input", "input_file", type=click.File("rb"), help="Read from file")
        @click.argument("path", type=str)
        def my_cmd(path: str, input_file: Any) -> None:
            """Test."""

        tool = command_to_tool(my_cmd, prefix="nexus")
        assert "input_file" not in tool["inputSchema"]["properties"]
        assert "path" in tool["inputSchema"]["properties"]


# ---------------------------------------------------------------------------
# Unit tests — walk_click_tree
# ---------------------------------------------------------------------------


class TestWalkClickTree:
    def test_flat_commands(self) -> None:
        @click.group()
        def cli() -> None:
            pass

        @cli.command()
        @click.argument("path", type=str)
        def cat(path: str) -> None:
            """Read a file."""

        @cli.command()
        @click.argument("path", type=str)
        @click.argument("content", type=str)
        def write(path: str, content: str) -> None:
            """Write a file."""

        tools = walk_click_tree(cli, prefix="nexus")
        names = {t["name"] for t in tools}
        assert "nexus_cat" in names
        assert "nexus_write" in names

    def test_nested_groups(self) -> None:
        @click.group()
        def cli() -> None:
            pass

        @cli.group()
        def agent() -> None:
            pass

        @agent.command()
        @click.argument("agent_id", type=str)
        def info(agent_id: str) -> None:
            """Show agent info."""

        tools = walk_click_tree(cli, prefix="nexus")
        names = {t["name"] for t in tools}
        assert "nexus_agent_info" in names

    def test_skips_groups_themselves(self) -> None:
        @click.group()
        def cli() -> None:
            """Root group."""

        @cli.group()
        def sub() -> None:
            """Sub group."""

        @sub.command()
        def leaf() -> None:
            """Leaf command."""

        tools = walk_click_tree(cli, prefix="nexus")
        names = {t["name"] for t in tools}
        # Groups are not tools
        assert "nexus" not in names
        assert "nexus_sub" not in names
        # Leaf command is a tool
        assert "nexus_sub_leaf" in names

    def test_hyphenated_group_names(self) -> None:
        @click.group()
        def cli() -> None:
            pass

        @cli.group(name="my-group")
        def my_group() -> None:
            pass

        @my_group.command(name="do-thing")
        def do_thing() -> None:
            """Do the thing."""

        tools = walk_click_tree(cli, prefix="nexus")
        names = {t["name"] for t in tools}
        assert "nexus_my_group_do_thing" in names

    def test_all_schemas_valid_json(self) -> None:
        @click.group()
        def cli() -> None:
            pass

        @cli.command()
        @click.argument("path", type=str)
        @click.option("--recursive", "-r", is_flag=True)
        def ls(path: str, recursive: bool) -> None:
            """List files."""

        tools = walk_click_tree(cli, prefix="nexus")
        for tool in tools:
            schema = tool["inputSchema"]
            assert schema["type"] == "object"
            assert "properties" in schema
            # Must be JSON-serializable
            json.dumps(schema)

    def test_empty_group(self) -> None:
        @click.group()
        def cli() -> None:
            pass

        tools = walk_click_tree(cli, prefix="nexus")
        assert tools == []


# ---------------------------------------------------------------------------
# Integration — walk the real CLI tree
# ---------------------------------------------------------------------------


class TestExportToolsIntegration:
    def test_real_cli_produces_tools(self) -> None:
        from nexus.cli.main import main

        tools = walk_click_tree(main, prefix="nexus")
        assert len(tools) > 10

        names = {t["name"] for t in tools}
        assert "nexus_ls" in names
        assert "nexus_cat" in names
        assert "nexus_write" in names
        assert "nexus_mkdir" in names
        assert "nexus_rm" in names

    def test_real_cli_no_infrastructure_params(self) -> None:
        from nexus.cli.main import main

        tools = walk_click_tree(main, prefix="nexus")
        infra = {"data_dir", "backend", "config", "gcs_bucket", "remote_url", "remote_api_key"}
        for tool in tools:
            props = set(tool["inputSchema"]["properties"].keys())
            leaked = props & infra
            assert not leaked, f"Tool {tool['name']} leaks infra params: {leaked}"

    def test_all_tools_json_serializable(self) -> None:
        from nexus.cli.main import main

        tools = walk_click_tree(main, prefix="nexus")
        # Must serialize without default=str — all values should be JSON-native
        output = json.dumps(tools, indent=2)
        parsed = json.loads(output)
        assert len(parsed) == len(tools)

    def test_nested_command_names(self) -> None:
        from nexus.cli.main import main

        tools = walk_click_tree(main, prefix="nexus")
        names = {t["name"] for t in tools}
        # Agent subcommands should be nested
        assert "nexus_agent_register" in names
        assert "nexus_agent_list" in names
        # MCP subcommands
        assert "nexus_mcp_serve" in names

    def test_every_tool_has_description(self) -> None:
        from nexus.cli.main import main

        tools = walk_click_tree(main, prefix="nexus")
        for tool in tools:
            assert tool["description"], f"Tool {tool['name']} has no description"
