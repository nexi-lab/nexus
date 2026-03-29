"""Interactive connection setup — `nexus connect <url>`.

Guides the user through:
  1. Prompt for API key
  2. Test connection (Ping RPC, 3s timeout)
  3. Save as named profile
"""

import os
from urllib.parse import urlparse

import click

from nexus.cli.config import (
    ProfileEntry,
    load_cli_config,
    save_cli_config,
)
from nexus.cli.theme import console

_CONNECT_TIMEOUT = 3.0


@click.command(name="connect")
@click.argument("url", type=str)
@click.option(
    "--name", "-n", type=str, default=None, help="Profile name (default: derived from URL hostname)"
)
@click.option("--api-key", "-k", type=str, default=None, help="API key (skips interactive prompt)")
@click.option("--zone-id", "-z", type=str, default=None, help="Default zone ID for this profile")
@click.option("--skip-test", is_flag=True, default=False, help="Skip connection test")
def connect_cmd(
    url: str,
    name: str | None,
    api_key: str | None,
    zone_id: str | None,
    skip_test: bool,
) -> None:
    """Interactively set up a connection to a Nexus server.

    Prompts for API key, tests the connection, and saves as a named profile.

    Examples:
        nexus connect https://nexus.prod.example.com
        nexus connect http://localhost:2026 --name local --skip-test
        nexus connect https://nexus.staging.example.com -k nx_test_xxx -n staging
    """
    # Derive profile name from hostname if not provided
    if not name:
        parsed = urlparse(url)
        hostname = parsed.hostname or "unknown"
        # Use first segment of hostname: nexus.prod.example.com -> nexus-prod
        parts = hostname.split(".")
        name = "-".join(parts[:2]) if len(parts) > 1 else parts[0]
        if name in ("localhost", "127-0-0-1"):
            name = "local"

    # Prompt for API key if not provided
    if api_key is None:
        api_key = click.prompt(
            "API key",
            default="",
            hide_input=True,
            show_default=False,
            prompt_suffix=": ",
        )
        if not api_key:
            api_key = None

    # Test connection
    test_passed = True
    if not skip_test:
        test_passed = _test_connection_interactive(url, api_key)

    if not test_passed:
        # Connection failed — offer to save anyway
        save_anyway = click.confirm(
            "Connection test failed. Save profile anyway?",
            default=False,
        )
        if not save_anyway:
            console.print("[nexus.muted]Aborted. Profile not saved.[/nexus.muted]")
            return

    # Save profile
    config = load_cli_config()
    if name in config.profiles:
        overwrite = click.confirm(
            f"Profile '{name}' already exists. Overwrite?",
            default=True,
        )
        if not overwrite:
            console.print("[nexus.muted]Aborted. Profile not saved.[/nexus.muted]")
            return

    config.profiles[name] = ProfileEntry(url=url, api_key=api_key, zone_id=zone_id)
    config.current_profile = name
    save_cli_config(config)

    console.print(f"\nSaved and activated profile [bold]'{name}'[/bold]")
    console.print(f"  URL: {url}")
    if zone_id:
        console.print(f"  Zone: {zone_id}")


def _test_connection_interactive(url: str, api_key: str | None) -> bool:
    """Test connection with 3s timeout. Returns True if successful."""
    console.print(f"[nexus.muted]Testing connection to {url}...[/nexus.muted]")

    try:
        from nexus.remote.rpc_transport import RPCTransport

        grpc_port = int(os.getenv("NEXUS_GRPC_PORT", "2028"))
        parsed = urlparse(url)
        grpc_address = f"{parsed.hostname}:{grpc_port}"

        transport = RPCTransport(
            server_address=grpc_address,
            auth_token=api_key,
            timeout=_CONNECT_TIMEOUT,
            connect_timeout=_CONNECT_TIMEOUT,
        )
        result = transport.ping()
        console.print(
            f"[nexus.success]Connected![/nexus.success] "
            f"Server version {result.get('version', '?')}, "
            f"zone '{result.get('zone_id', 'default')}'"
        )
        return True
    except Exception as e:
        console.print(f"[nexus.error]Connection failed:[/nexus.error] {e}")
        # Offer retry
        retry = click.confirm("Retry?", default=True)
        if retry:
            return _test_connection_interactive(url, api_key)
        return False


def register_commands(cli: click.Group) -> None:
    """Register connect command."""
    cli.add_command(connect_cmd)
