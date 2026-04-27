"""Lineage CLI commands -- query agent lineage (Issue #3417).

Examples:
    nexus lineage upstream /output/result.json
    nexus lineage downstream /data/input.csv
    nexus lineage stale /data/input.csv
"""

from __future__ import annotations

import logging
from urllib.parse import quote

import click

from nexus.cli.utils import add_backend_options, console
from nexus.contracts.constants import ROOT_ZONE_ID

logger = logging.getLogger(__name__)


def register_commands(cli: click.Group) -> None:
    """Register lineage commands."""
    cli.add_command(lineage)


@click.group(name="lineage")
def lineage() -> None:
    """Agent lineage tracking -- query input/output dependencies."""


@lineage.command(name="upstream")
@click.argument("path")
@add_backend_options
@click.pass_context
def lineage_upstream(
    ctx: click.Context, path: str, remote_url: str | None, remote_api_key: str | None
) -> None:
    """Show upstream inputs for an output file.

    Example:
        nexus lineage upstream /output/result.json
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
        result = client.get(f"/api/v2/lineage/{quote(urn, safe='')}")
    except Exception as e:
        console.print(f"[yellow]No lineage found for {path}[/yellow]")
        logger.debug("lineage upstream error: %s", e)
        return

    upstream = result.get("upstream", [])
    agent_id = result.get("agent_id", "")
    operation = result.get("operation", "")

    console.print(f"[bold]Lineage for {path}[/bold]")
    console.print(f"  Agent:     {agent_id}")
    console.print(f"  Operation: {operation}")
    if result.get("truncated"):
        console.print("  [yellow]Truncated: upstream list was capped[/yellow]")
    console.print()

    if not upstream:
        console.print("  No upstream inputs recorded.")
        return

    console.print(f"  Upstream inputs ({len(upstream)}):")
    for entry in upstream:
        version = entry.get("version", 0)
        etag = entry.get("content_id", "")[:12]
        access = entry.get("access_type", "content")
        console.print(f"    {entry['path']}  (v{version}, {access}, content_id={etag}...)")


@lineage.command(name="downstream")
@click.argument("path")
@add_backend_options
@click.pass_context
def lineage_downstream(
    ctx: click.Context, path: str, remote_url: str | None, remote_api_key: str | None
) -> None:
    """Show downstream outputs that depend on an input file (impact analysis).

    Example:
        nexus lineage downstream /data/input.csv
    """
    from nexus.cli.api_client import get_api_client_from_options
    from nexus.cli.config import resolve_connection

    profile_name = (ctx.obj or {}).get("profile")
    resolve_connection(
        remote_url=remote_url, remote_api_key=remote_api_key, profile_name=profile_name
    )
    client = get_api_client_from_options(remote_url, remote_api_key, profile_name=profile_name)

    try:
        result = client.get(f"/api/v2/lineage/downstream/query?path={quote(path, safe='')}")
    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        raise SystemExit(1) from e

    downstream = result.get("downstream", [])
    console.print(f"[bold]Downstream dependents of {path}[/bold]")
    console.print()

    if not downstream:
        console.print("  No downstream outputs found.")
        return

    console.print(f"  Downstream outputs ({len(downstream)}):")
    for entry in downstream:
        ds_path = entry.get("downstream_path", entry.get("downstream_urn", "?"))
        agent = entry.get("agent_id", "")
        version = entry.get("upstream_version", 0)
        console.print(f"    {ds_path}  (read at v{version}, by {agent})")


@lineage.command(name="stale")
@click.argument("path")
@click.option(
    "--version", "file_version", type=int, help="Current version (auto-detected if omitted)"
)
@click.option("--etag", help="Current etag/content hash (auto-detected if omitted)")
@add_backend_options
@click.pass_context
def lineage_stale(
    ctx: click.Context,
    path: str,
    file_version: int | None,
    etag: str | None,
    remote_url: str | None,
    remote_api_key: str | None,
) -> None:
    """Show downstream outputs that are stale because this input changed.

    Example:
        nexus lineage stale /data/input.csv
        nexus lineage stale /data/input.csv --version 5 --etag abc123
    """
    from nexus.cli.api_client import get_api_client_from_options
    from nexus.cli.config import resolve_connection

    profile_name = (ctx.obj or {}).get("profile")
    resolve_connection(
        remote_url=remote_url, remote_api_key=remote_api_key, profile_name=profile_name
    )
    client = get_api_client_from_options(remote_url, remote_api_key, profile_name=profile_name)

    # Auto-detect version/etag if not provided
    if file_version is None or etag is None:
        try:
            stat = client.get(f"/api/v2/files/stat?path={quote(path, safe='')}")
            if file_version is None:
                file_version = stat.get("version", 0)
            if etag is None:
                etag = stat.get("content_id", "")
        except Exception as exc:
            console.print(
                "[yellow]Could not auto-detect version/etag. Provide --version and --etag.[/yellow]"
            )
            raise SystemExit(1) from exc

    try:
        params = f"path={quote(path, safe='')}&current_version={file_version}&current_etag={quote(etag or '', safe='')}"
        result = client.get(f"/api/v2/lineage/stale/query?{params}")
    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        raise SystemExit(1) from e

    stale = result.get("stale", [])
    console.print(f"[bold]Staleness check for {path}[/bold]")
    console.print(f"  Current version: {file_version}")
    console.print(f"  Current etag:    {(etag or '')[:12]}...")
    console.print()

    if not stale:
        console.print("  [green]All downstream outputs are up to date.[/green]")
        return

    console.print(f"  [red]Stale outputs ({len(stale)}):[/red]")
    for entry in stale:
        ds_path = entry.get("downstream_path", entry.get("downstream_urn", "?"))
        rec_v = entry.get("recorded_version", 0)
        agent = entry.get("agent_id", "")
        console.print(f"    {ds_path}  (read at v{rec_v}, by {agent})")
