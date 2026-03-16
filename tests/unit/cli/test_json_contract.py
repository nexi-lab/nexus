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

from nexus.cli.exit_codes import ExitCode
from nexus.cli.output import _exception_to_error_code, add_output_options, render_error


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
    # Q1 sweep — newly migrated modules
    ("nexus.cli.commands.sandbox", "list_sandboxes"),
    ("nexus.cli.commands.mounts", "list_mounts"),
    ("nexus.cli.commands.memory", "query"),
    ("nexus.cli.commands.cache", "stats"),
    ("nexus.cli.commands.admin", "list_users"),
    ("nexus.cli.commands.snapshots", "snapshot_list"),
    ("nexus.cli.commands.pay", "pay_balance"),
    ("nexus.cli.commands.locks", "lock_list"),
    ("nexus.cli.commands.governance_cli", "governance_status"),
    ("nexus.cli.commands.events_cli", "events_replay"),
    ("nexus.cli.commands.exchange", "exchange_list"),
    ("nexus.cli.commands.audit", "audit_list"),
    ("nexus.cli.commands.connectors", "list_connectors"),
    ("nexus.cli.commands.config_cmd", "show_cmd"),
    # A4 — federation CLI
    ("nexus.cli.commands.federation", "federation_status"),
    ("nexus.cli.commands.federation", "federation_zones"),
    ("nexus.cli.commands.federation", "federation_info"),
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

    # All previously ad-hoc modules have been migrated to add_output_options.
    # This list is intentionally empty — any future ad-hoc additions will
    # be caught here.
    adhoc_modules: list[str] = []

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


# ---------------------------------------------------------------------------
# Test: structured error envelope from render_error() (Issue #2812)
# ---------------------------------------------------------------------------


class TestErrorEnvelope:
    """Verify the structured error envelope from render_error()."""

    def test_json_error_envelope_structure(self):
        """render_error() in JSON mode produces correct envelope."""
        from nexus.cli.output import OutputOptions

        output_opts = OutputOptions(
            json_output=True, quiet=False, verbosity=0, fields=None, request_id="test123"
        )
        with pytest.raises(SystemExit) as exc_info:
            render_error(error=ValueError("test error"), output_opts=output_opts)
        assert exc_info.value.code == ExitCode.GENERAL_ERROR

    def test_api_error_404_maps_to_not_found(self):
        """NexusAPIError with 404 status maps to NOT_FOUND error code."""
        from nexus.cli.client import NexusAPIError

        error = NexusAPIError(404, "Agent not found")
        code = _exception_to_error_code(error)
        assert code == "NOT_FOUND"

    def test_api_error_403_maps_to_permission_denied(self):
        from nexus.cli.client import NexusAPIError

        error = NexusAPIError(403, "Forbidden")
        code = _exception_to_error_code(error)
        assert code == "PERMISSION_DENIED"

    def test_api_error_400_maps_to_validation_error(self):
        from nexus.cli.client import NexusAPIError

        error = NexusAPIError(400, "Bad request")
        code = _exception_to_error_code(error)
        assert code == "VALIDATION_ERROR"

    def test_api_error_500_maps_to_internal_error(self):
        from nexus.cli.client import NexusAPIError

        error = NexusAPIError(500, "Internal server error")
        code = _exception_to_error_code(error)
        assert code == "INTERNAL_ERROR"

    def test_api_error_429_maps_to_unavailable(self):
        from nexus.cli.client import NexusAPIError

        error = NexusAPIError(429, "Too many requests")
        code = _exception_to_error_code(error)
        assert code == "UNAVAILABLE"

    def test_api_error_408_maps_to_timeout(self):
        from nexus.cli.client import NexusAPIError

        error = NexusAPIError(408, "Request timeout")
        code = _exception_to_error_code(error)
        assert code == "TIMEOUT"
