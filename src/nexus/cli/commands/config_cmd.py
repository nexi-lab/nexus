"""Configuration commands — `nexus config show/get/set/reset`.

Manages runtime settings in ~/.nexus/config.yaml under the `settings:` section.

Supported keys:
    default-zone-id, output.format, output.color,
    timing.enabled, timing.verbosity,
    connection.timeout, connection.pool-size
"""

import json

import click
from rich.console import Console
from rich.table import Table

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

console = Console()


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
@click.option(
    "--json-output", "--json", "json_out", is_flag=True, default=False, help="Output as JSON"
)
def show_cmd(json_out: bool) -> None:
    """Show merged configuration with source annotations.

    Examples:
        nexus config show
        nexus config show --json
    """
    config = load_cli_config()
    merged = get_merged_settings(config)

    if json_out:
        output = {key: value for key, (value, _source) in merged.items()}
        console.print(json.dumps(output, indent=2))
        return

    table = Table(title=f"Configuration ({get_config_path()})")
    table.add_column("Key", style="bold")
    table.add_column("Value")
    table.add_column("Source", style="dim")

    for key in sorted(merged):
        value, source = merged[key]
        value_str = str(value) if value is not None else "[dim]null[/dim]"
        table.add_row(key, value_str, source)

    console.print(table)

    # Also show active profile info
    if config.current_profile:
        console.print(f"\nActive profile: [bold]{config.current_profile}[/bold]")


@config_group.command(name="get")
@click.argument("key", type=str)
def get_cmd(key: str) -> None:
    """Get a specific configuration value.

    Examples:
        nexus config get output.format
        nexus config get timing.enabled
    """
    if key not in SUPPORTED_SETTINGS:
        console.print(f"[red]Error:[/red] Unknown setting: {key}")
        console.print(f"[dim]Supported keys: {', '.join(sorted(SUPPORTED_SETTINGS))}[/dim]")
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
        console.print(f"[red]Error:[/red] Unknown setting: {key}")
        console.print(f"[dim]Supported keys: {', '.join(sorted(SUPPORTED_SETTINGS))}[/dim]")
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
        console.print(f"[red]Error:[/red] Unknown setting: {key}")
        console.print(f"[dim]Supported keys: {', '.join(sorted(SUPPORTED_SETTINGS))}[/dim]")
        raise SystemExit(1)

    config = load_cli_config()
    config.settings = reset_setting(config.settings, key)
    save_cli_config(config)
    default = SUPPORTED_SETTINGS[key]
    console.print(f"Reset [bold]{key}[/bold] to default ({default})")


def register_commands(cli: click.Group) -> None:
    """Register config commands."""
    cli.add_command(config_group)
