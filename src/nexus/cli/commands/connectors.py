"""Nexus CLI Connector Commands.

Commands for discovering and inspecting available connectors:
- nexus connectors list - List all registered connectors
- nexus connectors info - Show connector details

Uses the HTTP REST API for connector discovery (not gRPC, which
doesn't expose connector registry methods).
"""

import asyncio
import sys
from typing import Any

import click

from nexus.cli.output import OutputOptions, add_output_options, render_output
from nexus.cli.theme import console
from nexus.cli.timing import CommandTiming
from nexus.cli.utils import (
    add_backend_options,
    handle_error,
)


def _resolve_http_url(remote_url: str | None) -> tuple[str, str | None]:
    """Resolve the HTTP base URL and API key from args or environment."""
    import os

    url = remote_url or os.environ.get("NEXUS_URL")
    api_key = os.environ.get("NEXUS_API_KEY")

    if not url:
        console.print("[nexus.error]Error:[/nexus.error] NEXUS_URL or --remote-url is required")
        console.print(
            "[nexus.warning]Hint:[/nexus.warning] export NEXUS_URL=http://your-nexus-server:2026"
            " or use `eval $(nexus env)`"
        )
        sys.exit(1)

    return url.rstrip("/"), api_key


async def _http_get(url: str, api_key: str | None) -> Any:
    """Make an authenticated HTTP GET request."""
    import httpx

    headers: dict[str, str] = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(url, headers=headers)
        resp.raise_for_status()
        return resp.json()


@click.group(name="connectors")
def connectors_group() -> None:
    """Discover and inspect available connectors.

    Connectors are backend types that can be mounted in Nexus.
    Use these commands to see what connectors are available
    and their configuration requirements.

    Examples:
        nexus connectors list
        nexus connectors list --category storage
        nexus connectors info gws_gmail
    """


@connectors_group.command(name="list")
@click.option(
    "--category",
    "-c",
    type=str,
    default=None,
    help="Filter by category (storage, api, oauth, cli)",
)
@add_output_options
@add_backend_options
def list_connectors(
    category: str | None,
    output_opts: OutputOptions,
    remote_url: str | None,
    remote_api_key: str | None,
) -> None:
    """List all registered connectors.

    Shows all available connector types that can be used with 'nexus mounts add'.

    Examples:
        nexus connectors list
        nexus connectors list --category storage
        nexus connectors list --json
    """
    timing = CommandTiming()
    try:
        base_url, api_key = _resolve_http_url(remote_url)
        api_key = remote_api_key or api_key

        with timing.phase("server"):
            data = asyncio.run(_http_get(f"{base_url}/api/v2/connectors", api_key))

        connectors: list[dict[str, Any]] = data.get("connectors", [])

        if category:
            connectors = [c for c in connectors if c.get("category") == category]

        if not connectors:
            if category:
                console.print(
                    f"[nexus.warning]No connectors found in category '{category}'[/nexus.warning]"
                )
            else:
                console.print("[nexus.warning]No connectors registered[/nexus.warning]")
            return

        def _render(items: list[dict[str, Any]]) -> None:
            from rich.table import Table

            table = Table(
                title="Available Connectors", show_header=True, header_style="nexus.table_header"
            )
            table.add_column("Name", style="nexus.success")
            table.add_column("Description")
            table.add_column("Category", style="nexus.warning")
            table.add_column("Capabilities", style="nexus.muted")

            for c in items:
                caps = c.get("capabilities", [])
                caps_str = ", ".join(caps[:3])
                if len(caps) > 3:
                    caps_str += f" (+{len(caps) - 3})"
                table.add_row(
                    c["name"],
                    c.get("description", ""),
                    c.get("category", ""),
                    caps_str,
                )

            console.print(table)
            console.print(f"\n[nexus.muted]Total: {len(items)} connectors[/nexus.muted]")

        render_output(
            data=connectors,
            output_opts=output_opts,
            timing=timing,
            human_formatter=_render,
        )

    except Exception as e:
        handle_error(e)


@connectors_group.command(name="info")
@click.argument("connector_name", type=str)
@add_backend_options
def connector_info(
    connector_name: str,
    remote_url: str | None,
    remote_api_key: str | None,
) -> None:
    """Show details for a specific connector.

    CONNECTOR_NAME: The connector identifier (e.g., gws_gmail, path_s3)

    Examples:
        nexus connectors info gws_gmail
        nexus connectors info path_s3
    """
    try:
        base_url, api_key = _resolve_http_url(remote_url)
        api_key = remote_api_key or api_key

        data = asyncio.run(_http_get(f"{base_url}/api/v2/connectors", api_key))
        connectors: list[dict[str, Any]] = data.get("connectors", [])

        info = next((c for c in connectors if c["name"] == connector_name), None)
        if not info:
            available = ", ".join(c["name"] for c in connectors)
            console.print(f"[nexus.error]Unknown connector: {connector_name}[/nexus.error]")
            console.print(f"[nexus.muted]Available: {available}[/nexus.muted]")
            sys.exit(1)

        console.print(f"\n[bold nexus.value]{info['name']}[/bold nexus.value]")
        console.print(
            f"  [nexus.muted]Description:[/nexus.muted] {info.get('description') or 'No description'}"
        )
        console.print(f"  [nexus.muted]Category:[/nexus.muted] {info.get('category', 'unknown')}")
        console.print(
            f"  [nexus.muted]User-scoped:[/nexus.muted] {'Yes' if info.get('user_scoped') else 'No'}"
        )

        caps = info.get("capabilities", [])
        if caps:
            console.print(f"  [nexus.muted]Capabilities:[/nexus.muted] {', '.join(caps)}")

        requires = info.get("requires", [])
        if requires:
            console.print(f"  [nexus.muted]Dependencies:[/nexus.muted] {', '.join(requires)}")
        else:
            console.print("  [nexus.muted]Dependencies:[/nexus.muted] None (core)")

        if "class" in info:
            console.print(f"  [nexus.muted]Class:[/nexus.muted] {info['class']}")

        console.print()

    except Exception as e:
        handle_error(e)


@connectors_group.command(name="capabilities")
@click.argument("name", required=False, default=None)
@add_output_options
@add_backend_options
def connectors_capabilities(
    name: str | None,
    output_opts: OutputOptions,
    remote_url: str | None,
    remote_api_key: str | None,
) -> None:
    """Show connector capabilities.

    Without arguments, shows capabilities for all connectors.
    With a connector name, shows detailed capabilities for that connector.

    Examples:
        nexus connectors capabilities
        nexus connectors capabilities gws_gmail
    """
    timing = CommandTiming()
    try:
        base_url, api_key = _resolve_http_url(remote_url)
        api_key = remote_api_key or api_key

        with timing.phase("server"):
            data = asyncio.run(_http_get(f"{base_url}/api/v2/connectors", api_key))

        connectors: list[dict[str, Any]] = data.get("connectors", [])

        if name:
            connectors = [
                c for c in connectors if c.get("name") == name or c.get("connector_id") == name
            ]
            if not connectors:
                available = ", ".join(c["name"] for c in data.get("connectors", []))
                console.print(f"[nexus.error]Error:[/nexus.error] Connector '{name}' not found")
                console.print(f"[nexus.muted]Available: {available}[/nexus.muted]")
                sys.exit(1)

        def _render(items: list[dict[str, Any]]) -> None:
            from rich.table import Table

            table = Table(
                title="Connector Capabilities",
                show_header=True,
                header_style="nexus.table_header",
            )
            table.add_column("Name", style="nexus.value")
            table.add_column("Category", style="nexus.success")
            table.add_column("Capabilities", style="nexus.warning")

            for c in items:
                caps = c.get("capabilities", [])
                caps_str = ", ".join(str(cap) for cap in caps) if caps else "none"
                table.add_row(
                    c.get("name", "?"),
                    c.get("category", "?"),
                    caps_str,
                )

            console.print(table)
            console.print(f"\n[nexus.muted]Total: {len(items)} connectors[/nexus.muted]")

        render_output(
            data=connectors,
            output_opts=output_opts,
            timing=timing,
            human_formatter=_render,
        )

    except SystemExit:
        raise
    except Exception as e:
        handle_error(e)


def register_commands(cli: click.Group) -> None:
    """Register connector commands to the main CLI group."""
    cli.add_command(connectors_group)
