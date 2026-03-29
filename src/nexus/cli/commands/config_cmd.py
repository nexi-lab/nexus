"""Configuration commands -- `nexus config show/get/set/reset`.

Manages runtime settings in ~/.nexus/config.yaml under the `settings:` section.

Supported keys:
    default-zone-id, output.format, output.color,
    timing.enabled, timing.verbosity,
    connection.timeout, connection.pool-size
"""

from typing import Any

import click

from nexus.cli.config import (
    SUPPORTED_SETTINGS,
    get_config_path,
    get_merged_settings,
    get_setting,
    load_cli_config,
    reset_setting,
    save_cli_config,
    set_setting,
)
from nexus.cli.output import OutputOptions, add_output_options, render_output
from nexus.cli.theme import console
from nexus.cli.timing import CommandTiming


@click.group(name="config")
def config_group() -> None:
    """View and modify Nexus CLI configuration.

    Settings are stored in ~/.nexus/config.yaml under the `settings:` section.
    Precedence: CLI flag > env var > config file > default.

    Examples:
        nexus config show
        nexus config set output.format json
        nexus config get timing.enabled
        nexus config reset output.format
    """


@config_group.command(name="show")
@add_output_options
def show_cmd(output_opts: OutputOptions) -> None:
    """Show merged configuration with source annotations.

    Examples:
        nexus config show
        nexus config show --json
    """
    timing = CommandTiming()
    with timing.phase("load"):
        config = load_cli_config()
        merged = get_merged_settings(config)

    # Build data dict for JSON output
    data: dict[str, Any] = {key: value for key, (value, _source) in merged.items()}

    def _render(_d: dict[str, Any]) -> None:  # noqa: ARG001
        from rich.table import Table

        table = Table(title=f"Configuration ({get_config_path()})")
        table.add_column("Key", style="bold")
        table.add_column("Value")
        table.add_column("Source", style="nexus.muted")

        for key in sorted(merged):
            value, source = merged[key]
            value_str = str(value) if value is not None else "[nexus.muted]null[/nexus.muted]"
            table.add_row(key, value_str, source)

        console.print(table)

        # Also show active profile info
        if config.current_profile:
            console.print(f"\nActive profile: [bold]{config.current_profile}[/bold]")

    render_output(
        data=data,
        output_opts=output_opts,
        timing=timing,
        human_formatter=_render,
    )


@config_group.command(name="get")
@click.argument("key", type=str)
def get_cmd(key: str) -> None:
    """Get a specific configuration value.

    Examples:
        nexus config get output.format
        nexus config get timing.enabled
    """
    if key not in SUPPORTED_SETTINGS:
        console.print(f"[nexus.error]Error:[/nexus.error] Unknown setting: {key}")
        console.print(
            f"[nexus.muted]Supported keys: {', '.join(sorted(SUPPORTED_SETTINGS))}[/nexus.muted]"
        )
        raise SystemExit(1)

    config = load_cli_config()
    value = get_setting(config.settings, key)
    click.echo(value)


@config_group.command(name="set")
@click.argument("key", type=str)
@click.argument("value", type=str)
def set_cmd(key: str, value: str) -> None:
    """Set a configuration value.

    Examples:
        nexus config set output.format json
        nexus config set timing.enabled true
        nexus config set connection.timeout 60
    """
    if key not in SUPPORTED_SETTINGS:
        console.print(f"[nexus.error]Error:[/nexus.error] Unknown setting: {key}")
        console.print(
            f"[nexus.muted]Supported keys: {', '.join(sorted(SUPPORTED_SETTINGS))}[/nexus.muted]"
        )
        raise SystemExit(1)

    config = load_cli_config()
    config.settings = set_setting(config.settings, key, value)
    save_cli_config(config)
    console.print(f"Set [bold]{key}[/bold] = {value}")


@config_group.command(name="reset")
@click.argument("key", type=str)
def reset_cmd(key: str) -> None:
    """Reset a configuration value to its default.

    Examples:
        nexus config reset output.format
        nexus config reset timing.enabled
    """
    if key not in SUPPORTED_SETTINGS:
        console.print(f"[nexus.error]Error:[/nexus.error] Unknown setting: {key}")
        console.print(
            f"[nexus.muted]Supported keys: {', '.join(sorted(SUPPORTED_SETTINGS))}[/nexus.muted]"
        )
        raise SystemExit(1)

    config = load_cli_config()
    config.settings = reset_setting(config.settings, key)
    save_cli_config(config)
    default = SUPPORTED_SETTINGS[key]
    console.print(f"Reset [bold]{key}[/bold] to default ({default})")


def register_commands(cli: click.Group) -> None:
    """Register config commands."""
    cli.add_command(config_group)
