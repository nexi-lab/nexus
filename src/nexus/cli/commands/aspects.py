"""Aspects CLI commands -- list and get entity aspects (Issue #2930).

Examples:
    nexus aspects list /workspace/demo/restricted/internal.md
    nexus aspects get /workspace/demo/restricted/internal.md governance.classification
"""

from __future__ import annotations

import json
import logging
from urllib.parse import quote

import click

from nexus.cli.utils import add_backend_options, console
from nexus.contracts.constants import ROOT_ZONE_ID

logger = logging.getLogger(__name__)


def register_commands(cli: click.Group) -> None:
    """Register aspects commands."""
    cli.add_command(aspects)


@click.group(name="aspects")
def aspects() -> None:
    """Entity aspect operations -- list and inspect metadata facets."""


@aspects.command(name="list")
@click.argument("path")
@add_backend_options
def aspects_list(path: str, remote_url: str | None, remote_api_key: str | None) -> None:
    """List all aspects attached to a file.

    Example:
        nexus aspects list /workspace/demo/restricted/internal.md
    """
    from nexus.cli.api_client import get_api_client_from_options
    from nexus.contracts.urn import NexusURN

    client = get_api_client_from_options(remote_url, remote_api_key)
    urn = str(NexusURN.for_file(ROOT_ZONE_ID, path))

    try:
        result = client.get(f"/api/v2/aspects/{quote(urn, safe='')}")
    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        raise SystemExit(1) from e

    aspect_names: list[str] = result.get("aspects", [])
    if not aspect_names:
        console.print(f"[yellow]No aspects found for {path}[/yellow]")
        return

    console.print(f"[bold]Aspects for {path}[/bold]")
    console.print(f"  URN: {result.get('entity_urn', urn)}")
    console.print()
    for name in aspect_names:
        console.print(f"  * {name}")


@aspects.command(name="get")
@click.argument("path")
@click.argument("aspect_name")
@add_backend_options
def aspects_get(
    path: str,
    aspect_name: str,
    remote_url: str | None,
    remote_api_key: str | None,
) -> None:
    """Get a specific aspect attached to a file.

    Example:
        nexus aspects get /workspace/demo/restricted/internal.md governance.classification
    """
    from nexus.cli.api_client import get_api_client_from_options
    from nexus.contracts.urn import NexusURN

    client = get_api_client_from_options(remote_url, remote_api_key)
    urn = str(NexusURN.for_file(ROOT_ZONE_ID, path))

    try:
        result = client.get(f"/api/v2/aspects/{quote(urn, safe='')}/{quote(aspect_name, safe='')}")
    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        raise SystemExit(1) from e

    console.print(f"[bold]{aspect_name}[/bold] on {path}")
    console.print(f"  URN:     {result.get('entity_urn', urn)}")
    console.print(f"  Version: {result.get('version', 0)}")
    console.print(f"  Author:  {result.get('created_by', 'system')}")
    console.print()
    console.print(json.dumps(result.get("payload", {}), indent=2))
