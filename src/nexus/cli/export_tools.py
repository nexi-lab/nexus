"""Export CLI commands as MCP tool definitions.

Walks the Click command tree and generates MCP-compatible tool schemas
for each leaf command, enabling AI agents to discover and invoke CLI
operations programmatically.

Usage::

    from nexus.cli.export_tools import walk_click_tree
    from nexus.cli.main import main

    tools = walk_click_tree(main, prefix="nexus")
    # [{"name": "nexus_write", "description": "...", "inputSchema": {...}}, ...]
"""

from __future__ import annotations

from typing import Any

import click

# Infrastructure params injected by decorators — not relevant to tool callers.
_EXCLUDED_PARAMS: frozenset[str] = frozenset(
    {
        # add_backend_options
        "remote_url",
        "remote_api_key",
        # add_context_options
        "subject",
        "zone_id",
        "is_admin",
        "is_system",
        "admin_capabilities",
        "operation_context",
        # add_output_options
        "json_output",
        "quiet",
        "verbosity",
        "fields",
        "output_opts",
    }
)


def click_type_to_json_schema(param_type: click.ParamType) -> dict[str, Any]:
    """Map a Click parameter type to a JSON Schema type definition."""
    if isinstance(param_type, click.Choice):
        return {"type": "string", "enum": list(param_type.choices)}

    type_map: dict[type[click.ParamType], str] = {
        click.types.StringParamType: "string",
        click.types.IntParamType: "integer",
        click.types.FloatParamType: "number",
        click.types.BoolParamType: "boolean",
        click.Path: "string",
        click.types.UUIDParameterType: "string",
    }

    for click_cls, json_type in type_map.items():
        if isinstance(param_type, click_cls):
            return {"type": json_type}

    return {"type": "string"}


def command_to_tool(cmd: click.Command, *, prefix: str = "nexus") -> dict[str, Any]:
    """Convert a Click command into an MCP tool definition.

    Returns a dict with ``name``, ``description``, and ``inputSchema``
    (JSON Schema object).
    """
    # Build tool name: replace hyphens with underscores for valid identifiers
    raw_name = cmd.name or "unknown"
    tool_name = f"{prefix}_{raw_name}".replace("-", "_")

    # First line of docstring only
    description = ""
    if cmd.help:
        first_line = cmd.help.strip().split("\n")[0].strip()
        description = first_line

    properties: dict[str, Any] = {}
    required: list[str] = []

    for param in cmd.params:
        if param.name is None or param.name in _EXCLUDED_PARAMS:
            continue

        # Skip hidden params
        if getattr(param, "hidden", False):
            continue

        # Skip File params (not applicable for MCP tools)
        if isinstance(param.type, click.File):
            continue

        help_text = getattr(param, "help", None)

        # Handle multiple options → array type
        if isinstance(param, click.Option) and param.multiple:
            inner = click_type_to_json_schema(param.type)
            prop: dict[str, Any] = {"type": "array", "items": inner}
            if help_text:
                prop["description"] = help_text
            properties[param.name] = prop
            continue

        # Handle flag options → always boolean
        if isinstance(param, click.Option) and param.is_flag:
            prop = {"type": "boolean"}
            if help_text:
                prop["description"] = help_text
            properties[param.name] = prop
            continue

        # Standard type mapping
        prop = click_type_to_json_schema(param.type)

        if help_text:
            prop["description"] = help_text

        # Record non-None, non-tuple defaults for options (only JSON-safe values)
        if isinstance(param, click.Option) and param.default is not None and param.default != ():
            default = param.default
            if isinstance(default, str | int | float | bool):
                prop["default"] = default

        properties[param.name] = prop

        # Required: arguments without defaults, or required options
        if (
            isinstance(param, click.Argument)
            and param.required
            or isinstance(param, click.Option)
            and param.required
        ):
            required.append(param.name)

    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
    }
    if required:
        schema["required"] = required

    return {
        "name": tool_name,
        "description": description,
        "inputSchema": schema,
    }


def walk_click_tree(
    group: click.Group,
    *,
    prefix: str = "nexus",
) -> list[dict[str, Any]]:
    """Walk a Click group recursively, collecting MCP tool definitions for leaf commands."""
    tools: list[dict[str, Any]] = []
    ctx = click.Context(group)

    for name in group.list_commands(ctx):
        cmd = group.get_command(ctx, name)
        if cmd is None:
            continue
        # Normalize group name for prefix
        normalized = name.replace("-", "_")

        if isinstance(cmd, click.Group):
            tools.extend(walk_click_tree(cmd, prefix=f"{prefix}_{normalized}"))
        else:
            tools.append(command_to_tool(cmd, prefix=prefix))

    return tools
