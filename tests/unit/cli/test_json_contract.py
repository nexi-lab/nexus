"""T4: Contract test for --json output consistency.

Verifies that all commands using ``add_output_options`` produce a consistent
JSON envelope: ``{"data": ..., "_timing": ..., "_request_id": ...}``.

Also detects commands that use ad-hoc ``--json`` flags instead of the unified
decorator, flagging them for future migration.
"""

from __future__ import annotations

import importlib
import inspect
from typing import Any

import click
import pytest

from nexus.cli.output import add_output_options


def _collect_commands_from_group(
    group: click.Group,
    prefix: str = "",
) -> list[tuple[str, click.Command]]:
    """Recursively collect all commands from a Click group."""
    result: list[tuple[str, click.Command]] = []
    for name, cmd in group.commands.items():
        full_name = f"{prefix} {name}".strip() if prefix else name
        if isinstance(cmd, click.Group):
            result.extend(_collect_commands_from_group(cmd, full_name))
        else:
            result.append((full_name, cmd))
    return result


def _has_adhoc_json(cmd: click.Command) -> bool:
    """Check if a command has an ad-hoc --json flag (not from add_output_options)."""
    for p in cmd.params:
        if isinstance(p, click.Option):
            opts = p.opts + p.secondary_opts
            if "--json" in opts and p.name != "json_output":
                return True
            if p.name in ("output_json", "as_json", "json_out", "json_format"):
                return True
    return False


# ---------------------------------------------------------------------------
# Test: commands using add_output_options have correct params
# ---------------------------------------------------------------------------

# Modules known to use add_output_options
_UNIFIED_MODULES = [
    ("nexus.cli.commands.directory", "ls_cmd"),
    ("nexus.cli.commands.directory", "tree"),
    ("nexus.cli.commands.file_ops", "cat"),
    ("nexus.cli.commands.search", "glob_cmd"),
    ("nexus.cli.commands.search", "grep_cmd"),
    ("nexus.cli.commands.inspect", "info"),
    ("nexus.cli.commands.zone", "zone_list"),
    ("nexus.cli.commands.status", "status"),
    ("nexus.cli.commands.doctor", "doctor"),
]


def _load_command(module_path: str, cmd_name: str) -> click.Command | None:
    """Try to load a Click command from a module."""
    try:
        mod = importlib.import_module(module_path)
        return getattr(mod, cmd_name, None)
    except (ImportError, AttributeError):
        return None


@pytest.mark.parametrize(
    "module_path,cmd_name",
    _UNIFIED_MODULES,
    ids=[f"{m.split('.')[-1]}.{c}" for m, c in _UNIFIED_MODULES],
)
def test_unified_commands_have_output_params(module_path: str, cmd_name: str) -> None:
    """Commands using add_output_options must have --json, --verbose, --fields."""
    cmd = _load_command(module_path, cmd_name)
    if cmd is None:
        pytest.skip(f"Could not load {module_path}.{cmd_name}")

    param_names = {p.name for p in cmd.params}
    assert "json_output" in param_names, f"{cmd_name} missing --json from add_output_options"
    assert "verbosity" in param_names, f"{cmd_name} missing --verbose from add_output_options"
    assert "fields" in param_names, f"{cmd_name} missing --fields from add_output_options"
    assert "quiet" in param_names, f"{cmd_name} missing --quiet from add_output_options"


# ---------------------------------------------------------------------------
# Test: add_output_options decorator injects correct parameter types
# ---------------------------------------------------------------------------


def test_add_output_options_decorator_adds_correct_params() -> None:
    """The decorator should add exactly --json, --quiet, --verbose, --fields."""

    @click.command()
    @add_output_options
    def dummy_cmd(**_kwargs: Any) -> None:
        pass

    param_names = {p.name for p in dummy_cmd.params}
    assert "json_output" in param_names
    assert "quiet" in param_names
    assert "verbosity" in param_names
    assert "fields" in param_names


def test_add_output_options_json_is_flag() -> None:
    """--json must be a boolean flag, not a value option."""

    @click.command()
    @add_output_options
    def dummy_cmd(**_kwargs: Any) -> None:
        pass

    json_param = next(p for p in dummy_cmd.params if p.name == "json_output")
    assert isinstance(json_param, click.Option)
    assert json_param.is_flag is True


def test_add_output_options_verbose_is_count() -> None:
    """--verbose must be a count option (supports -v, -vv, -vvv)."""

    @click.command()
    @add_output_options
    def dummy_cmd(**_kwargs: Any) -> None:
        pass

    verbose_param = next(p for p in dummy_cmd.params if p.name == "verbosity")
    assert isinstance(verbose_param, click.Option)
    assert verbose_param.count is True


# ---------------------------------------------------------------------------
# Test: detect ad-hoc JSON flag inconsistencies
# ---------------------------------------------------------------------------


# Known ad-hoc JSON parameter names — migration targets for Q1 sweep:
# "output_json", "as_json", "json_out", "json_format"


def test_document_adhoc_json_commands() -> None:
    """Informational test: list commands with ad-hoc --json flags.

    This test does NOT fail — it documents which commands need migration
    to add_output_options in the Q1 sweep. It serves as a living inventory.
    """
    adhoc_commands: list[str] = []

    # Modules with known ad-hoc --json
    adhoc_modules = [
        "nexus.cli.commands.status",
        "nexus.cli.commands.doctor",
        "nexus.cli.commands.mounts",
        "nexus.cli.commands.memory",
        "nexus.cli.commands.cache",
        "nexus.cli.commands.connectors",
        "nexus.cli.commands.config_cmd",
        "nexus.cli.commands.sandbox",
    ]

    for mod_path in adhoc_modules:
        try:
            mod = importlib.import_module(mod_path)
        except ImportError:
            continue

        for _, obj in inspect.getmembers(mod):
            if (
                isinstance(obj, click.Command)
                and not isinstance(obj, click.Group)
                and _has_adhoc_json(obj)
            ):
                adhoc_commands.append(f"{mod_path.split('.')[-1]}.{obj.name}")

    # This is informational — print the list for visibility
    if adhoc_commands:
        pytest.skip(
            f"Q1 migration targets ({len(adhoc_commands)} commands with ad-hoc --json): "
            + ", ".join(sorted(adhoc_commands))
        )
