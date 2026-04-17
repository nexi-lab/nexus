"""Path-context CLI commands (Issue #3773).

Thin wrapper over ``/api/v2/path-contexts`` for administering the zone-scoped
path-prefix -> description mappings that surface as the ``context`` field on
search results.

Examples:
    nexus path-context set src/nexus/bricks/search "Hybrid search brick"
    nexus path-context list
    nexus path-context list --zone-id other
    nexus path-context delete src/nexus/bricks/search
"""

from __future__ import annotations

import json
import logging

import click

from nexus.cli.theme import console
from nexus.cli.utils import add_backend_options

logger = logging.getLogger(__name__)


def register_commands(cli: click.Group) -> None:
    """Register path-context commands."""
    cli.add_command(path_context)


@click.group(name="path-context")
def path_context() -> None:
    """Path context descriptions — admin CRUD for search result annotations.

    Descriptions are attached to search results whose path starts with the
    configured prefix (longest prefix wins). Requires admin credentials.
    """


@path_context.command(name="set")
@click.argument("path_prefix")
@click.argument("description")
@click.option("--zone-id", default="root", show_default=True, help="Zone scope")
@add_backend_options
@click.pass_context
def path_context_set(
    ctx: click.Context,
    path_prefix: str,
    description: str,
    zone_id: str,
    remote_url: str | None,
    remote_api_key: str | None,
) -> None:
    """Upsert a context description for a path prefix (admin only).

    Example:
        nexus path-context set src/nexus/bricks/search "Hybrid search brick"
    """
    from nexus.cli.api_client import get_api_client_from_options

    profile_name = (ctx.obj or {}).get("profile")
    client = get_api_client_from_options(remote_url, remote_api_key, profile_name=profile_name)
    try:
        result = client.put(
            "/api/v2/path-contexts/",
            {"zone_id": zone_id, "path_prefix": path_prefix, "description": description},
        )
    except Exception as e:
        console.print(f"[nexus.error]Error:[/nexus.error] {e}")
        raise SystemExit(1) from e
    console.print(
        f"[nexus.success]set[/nexus.success] "
        f"{result.get('zone_id')}:{result.get('path_prefix')} = {result.get('description')!r}"
    )


@path_context.command(name="list")
@click.option("--zone-id", default=None, help="Filter by zone (default: all zones)")
@click.option("--json", "as_json", is_flag=True, help="Emit raw JSON")
@add_backend_options
@click.pass_context
def path_context_list(
    ctx: click.Context,
    zone_id: str | None,
    as_json: bool,
    remote_url: str | None,
    remote_api_key: str | None,
) -> None:
    """List path contexts.

    Example:
        nexus path-context list
        nexus path-context list --zone-id root
    """
    from nexus.cli.api_client import get_api_client_from_options

    profile_name = (ctx.obj or {}).get("profile")
    client = get_api_client_from_options(remote_url, remote_api_key, profile_name=profile_name)
    params = {"zone_id": zone_id} if zone_id else None
    try:
        result = client.get("/api/v2/path-contexts/", params=params)
    except Exception as e:
        console.print(f"[nexus.error]Error:[/nexus.error] {e}")
        raise SystemExit(1) from e

    contexts = result.get("contexts", [])
    if as_json:
        console.print(json.dumps(contexts, indent=2))
        return
    if not contexts:
        console.print("[nexus.warning]No path contexts configured[/nexus.warning]")
        return
    for entry in contexts:
        console.print(
            f"  {entry.get('zone_id')}:{entry.get('path_prefix')} -> {entry.get('description')}"
        )


@path_context.command(name="delete")
@click.argument("path_prefix")
@click.option("--zone-id", default="root", show_default=True, help="Zone scope")
@add_backend_options
@click.pass_context
def path_context_delete(
    ctx: click.Context,
    path_prefix: str,
    zone_id: str,
    remote_url: str | None,
    remote_api_key: str | None,
) -> None:
    """Delete a path context (admin only).

    Example:
        nexus path-context delete src/nexus/bricks/search
    """
    import httpx

    from nexus.cli.api_client import get_api_client_from_options

    profile_name = (ctx.obj or {}).get("profile")
    client = get_api_client_from_options(remote_url, remote_api_key, profile_name=profile_name)
    try:
        client.delete(
            "/api/v2/path-contexts/",
            params={"zone_id": zone_id, "path_prefix": path_prefix},
        )
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            console.print(
                f"[nexus.warning]No path context found: {zone_id}:{path_prefix}[/nexus.warning]"
            )
            raise SystemExit(1) from e
        console.print(f"[nexus.error]Error:[/nexus.error] {e}")
        raise SystemExit(1) from e
    except Exception as e:
        console.print(f"[nexus.error]Error:[/nexus.error] {e}")
        raise SystemExit(1) from e
    console.print(f"[nexus.success]deleted[/nexus.success] {zone_id}:{path_prefix}")
