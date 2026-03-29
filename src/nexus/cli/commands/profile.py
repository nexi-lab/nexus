"""Profile management commands — kubectl-style connection profiles.

Commands:
    nexus profile list          Show all profiles (* = active)
    nexus profile use <name>    Switch active profile
    nexus profile add <name>    Add a new profile
    nexus profile show          Show current profile details
    nexus profile delete <name> Delete a profile
    nexus profile rename <old> <new>  Rename a profile
"""

import click
from rich.table import Table

from nexus.cli.config import (
    ProfileEntry,
    get_config_path,
    load_cli_config,
    save_cli_config,
)
from nexus.cli.theme import console


@click.group(name="profile")
def profile_group() -> None:
    """Manage connection profiles for different Nexus environments.

    Profiles store connection parameters (URL, API key, zone ID) in
    ~/.nexus/config.yaml. Switch between local, staging, and production
    with a single command.

    Examples:
        nexus profile list
        nexus profile add staging --url https://nexus.staging.example.com
        nexus profile use staging
        nexus --profile staging ls /
    """


@profile_group.command(name="list")
def list_cmd() -> None:
    """Show all saved profiles (* = active).

    Examples:
        nexus profile list
    """
    config = load_cli_config()

    if not config.profiles:
        console.print("[nexus.muted]No profiles configured.[/nexus.muted]")
        console.print(
            "[nexus.muted]Add one with:[/nexus.muted] nexus profile add <name> --url <url>"
        )
        return

    table = Table(title=f"Profiles ({get_config_path()})")
    table.add_column("", width=1)
    table.add_column("Name", style="bold")
    table.add_column("URL")
    table.add_column("Zone ID")
    table.add_column("API Key")

    for name, entry in sorted(config.profiles.items()):
        is_active = name == config.current_profile
        marker = "[nexus.success]*[/nexus.success]" if is_active else ""
        api_key_display = (
            _mask_api_key(entry.api_key) if entry.api_key else "[nexus.muted]-[/nexus.muted]"
        )
        table.add_row(
            marker,
            name,
            entry.url or "[nexus.muted]-[/nexus.muted]",
            entry.zone_id or "[nexus.muted]-[/nexus.muted]",
            api_key_display,
        )

    console.print(table)


@profile_group.command(name="use")
@click.argument("name", type=str)
def use_cmd(name: str) -> None:
    """Switch the active profile.

    Examples:
        nexus profile use staging
        nexus profile use production
    """
    config = load_cli_config()

    if name not in config.profiles:
        console.print(f"[nexus.error]Error:[/nexus.error] Profile '{name}' not found.")
        available = ", ".join(sorted(config.profiles.keys())) if config.profiles else "none"
        console.print(f"[nexus.muted]Available profiles: {available}[/nexus.muted]")
        raise SystemExit(1)

    config.current_profile = name
    save_cli_config(config)
    profile = config.profiles[name]
    console.print(f"Switched to profile [bold]'{name}'[/bold]")
    if profile.url:
        console.print(f"  URL: {profile.url}")


@profile_group.command(name="add")
@click.argument("name", type=str)
@click.option("--url", type=str, default=None, help="Nexus server URL")
@click.option("--api-key", type=str, default=None, help="API key for authentication")
@click.option("--zone-id", type=str, default=None, help="Default zone ID")
@click.option("--use/--no-use", default=False, help="Set as active profile after adding")
def add_cmd(
    name: str,
    url: str | None,
    api_key: str | None,
    zone_id: str | None,
    use: bool,
) -> None:
    """Add a new connection profile.

    Examples:
        nexus profile add staging --url https://nexus.staging.example.com --api-key nx_test_xxx
        nexus profile add local --url http://localhost:2026 --use
    """
    config = load_cli_config()

    if name in config.profiles:
        console.print(f"[nexus.error]Error:[/nexus.error] Profile '{name}' already exists.")
        console.print(
            "[nexus.muted]Use 'nexus profile delete' first, or choose a different name.[/nexus.muted]"
        )
        raise SystemExit(1)

    config.profiles[name] = ProfileEntry(url=url, api_key=api_key, zone_id=zone_id)
    if use:
        config.current_profile = name
    save_cli_config(config)

    console.print(f"Added profile [bold]'{name}'[/bold]")
    if use:
        console.print("  Set as active profile")


@profile_group.command(name="delete")
@click.argument("name", type=str)
@click.option("--force", is_flag=True, default=False, help="Skip confirmation")
def delete_cmd(name: str, force: bool) -> None:
    """Delete a saved profile.

    Examples:
        nexus profile delete old-staging
        nexus profile delete old-staging --force
    """
    config = load_cli_config()

    if name not in config.profiles:
        console.print(f"[nexus.error]Error:[/nexus.error] Profile '{name}' not found.")
        raise SystemExit(1)

    if not force:
        click.confirm(f"Delete profile '{name}'?", abort=True)

    del config.profiles[name]
    if config.current_profile == name:
        config.current_profile = None
        console.print(
            f"[nexus.warning]Note:[/nexus.warning] '{name}' was the active profile. No profile is now active."
        )

    save_cli_config(config)
    console.print(f"Deleted profile [bold]'{name}'[/bold]")


@profile_group.command(name="show")
@click.option(
    "--test", is_flag=True, default=False, help="Test connection to the active profile's server"
)
def show_cmd(test: bool) -> None:
    """Show the current active profile and its settings.

    Examples:
        nexus profile show
        nexus profile show --test
    """
    from nexus.cli.config import resolve_connection

    config = load_cli_config()
    resolved = resolve_connection(config=config)

    if config.current_profile:
        console.print(f"Active profile: [bold]{config.current_profile}[/bold]")
    else:
        console.print("[nexus.muted]No active profile (using defaults)[/nexus.muted]")

    console.print(f"  URL:    {resolved.url or '[nexus.muted]local[/nexus.muted]'}")
    console.print(f"  Zone:   {resolved.zone_id or '[nexus.muted]not set[/nexus.muted]'}")
    console.print(f"  Source: {resolved.source}")

    if resolved.api_key:
        console.print(f"  Key:    {_mask_api_key(resolved.api_key)}")

    if test and resolved.is_remote:
        _test_connection(resolved.url, resolved.api_key)
    elif test and not resolved.is_remote:
        console.print("[nexus.muted]Skipping connection test (local mode)[/nexus.muted]")


@profile_group.command(name="rename")
@click.argument("old_name", type=str)
@click.argument("new_name", type=str)
def rename_cmd(old_name: str, new_name: str) -> None:
    """Rename an existing profile.

    Examples:
        nexus profile rename staging staging-v2
    """
    config = load_cli_config()

    if old_name not in config.profiles:
        console.print(f"[nexus.error]Error:[/nexus.error] Profile '{old_name}' not found.")
        raise SystemExit(1)

    if new_name in config.profiles:
        console.print(f"[nexus.error]Error:[/nexus.error] Profile '{new_name}' already exists.")
        raise SystemExit(1)

    config.profiles[new_name] = config.profiles.pop(old_name)
    if config.current_profile == old_name:
        config.current_profile = new_name

    save_cli_config(config)
    console.print(f"Renamed profile [bold]'{old_name}'[/bold] to [bold]'{new_name}'[/bold]")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mask_api_key(key: str) -> str:
    """Mask an API key for display (show first 8 chars + last 4)."""
    if len(key) <= 12:
        return key[:4] + "****"
    return key[:8] + "****" + key[-4:]


def _test_connection(url: str | None, api_key: str | None) -> None:
    """Test connection to a remote Nexus server."""
    import os
    from urllib.parse import urlparse

    if not url:
        console.print("[nexus.error]Error:[/nexus.error] No URL to test")
        return

    console.print("[nexus.muted]Testing connection...[/nexus.muted]")

    try:
        from nexus.remote.rpc_transport import RPCTransport

        grpc_port = int(os.getenv("NEXUS_GRPC_PORT", "2028"))
        parsed = urlparse(url)
        grpc_address = f"{parsed.hostname}:{grpc_port}"

        transport = RPCTransport(
            server_address=grpc_address,
            auth_token=api_key,
            timeout=3.0,
            connect_timeout=3.0,
        )
        result = transport.ping()
        console.print(
            f"[nexus.success]Connection OK[/nexus.success] "
            f"(version={result.get('version', '?')}, "
            f"zone={result.get('zone_id', '?')}, "
            f"uptime={result.get('uptime', '?')}s)"
        )
    except Exception as e:
        console.print(f"[nexus.error]Connection failed:[/nexus.error] {e}")


def register_commands(cli: click.Group) -> None:
    """Register profile commands."""
    cli.add_command(profile_group)
