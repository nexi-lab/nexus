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

from nexus.cli.theme import console
from nexus.cli.utils import add_backend_options
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
@click.pass_context
def aspects_list(
    ctx: click.Context, path: str, remote_url: str | None, remote_api_key: str | None
) -> None:
    """List all aspects attached to a file.

    Example:
        nexus aspects list /workspace/demo/restricted/internal.md
    """
    from nexus.cli.api_client import get_api_client_from_options
    from nexus.cli.config import resolve_connection
    from nexus.contracts.urn import NexusURN

    profile_name = (ctx.obj or {}).get("profile")
    conn = resolve_connection(
        remote_url=remote_url, remote_api_key=remote_api_key, profile_name=profile_name
    )
    zone_id = conn.zone_id or ROOT_ZONE_ID
    client = get_api_client_from_options(remote_url, remote_api_key, profile_name=profile_name)
    urn = str(NexusURN.for_file(zone_id, path))

    try:
        result = client.get(f"/api/v2/aspects/{quote(urn, safe='')}")
    except Exception as e:
        console.print(f"[nexus.error]Error:[/nexus.error] {e}")
        raise SystemExit(1) from e

    aspect_names: list[str] = result.get("aspects", [])
    if not aspect_names:
        console.print(f"[nexus.warning]No aspects found for {path}[/nexus.warning]")
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
@click.pass_context
def aspects_get(
    ctx: click.Context,
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
    from nexus.cli.config import resolve_connection
    from nexus.contracts.urn import NexusURN

    profile_name = (ctx.obj or {}).get("profile")
    conn = resolve_connection(
        remote_url=remote_url, remote_api_key=remote_api_key, profile_name=profile_name
    )
    zone_id = conn.zone_id or ROOT_ZONE_ID
    client = get_api_client_from_options(remote_url, remote_api_key, profile_name=profile_name)
    urn = str(NexusURN.for_file(zone_id, path))

    try:
        result = client.get(f"/api/v2/aspects/{quote(urn, safe='')}/{quote(aspect_name, safe='')}")
    except Exception as e:
        console.print(f"[nexus.error]Error:[/nexus.error] {e}")
        raise SystemExit(1) from e

    console.print(f"[bold]{aspect_name}[/bold] on {path}")
    console.print(f"  URN:     {result.get('entity_urn', urn)}")
    console.print(f"  Version: {result.get('version', 0)}")
    console.print(f"  Author:  {result.get('created_by', 'system')}")
    console.print()
    console.print(json.dumps(result.get("payload", {}), indent=2))
