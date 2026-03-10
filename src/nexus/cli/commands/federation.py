"""Federation CLI commands -- zone federation status, sharing, and mounts.

Maps to /api/v2/federation/* REST endpoints via NexusServiceClient.
Issue #A4: User-friendly federation CLI.
"""

from typing import Any

import click
from rich.table import Table

from nexus.cli.output import OutputOptions, add_output_options, render_output
from nexus.cli.timing import CommandTiming
from nexus.cli.utils import (
    REMOTE_API_KEY_OPTION,
    REMOTE_URL_OPTION,
    console,
    get_service_client,
)


@click.group()
def federation() -> None:
    """Federation management -- zones, sharing, and mounts.

    \b
    Prerequisites:
        - Running Nexus server with federation enabled
        - Server URL (set via NEXUS_URL or --remote-url)
        - API key (set via NEXUS_API_KEY or --remote-api-key)

    \b
    Examples:
        nexus federation status
        nexus federation zones --json
        nexus federation info my-zone
        nexus federation share /data/shared
        nexus federation join peer1:2126 /shared /local/shared
        nexus federation mount --parent-zone root --path /mnt --target-zone team
        nexus federation unmount --parent-zone root --path /mnt
    """


@federation.command("status")
@add_output_options
@REMOTE_API_KEY_OPTION
@REMOTE_URL_OPTION
def federation_status(
    output_opts: OutputOptions,
    remote_url: str | None,
    remote_api_key: str | None,
) -> None:
    """Show federation overview.

    Displays zones, link counts, and node info.

    \b
    Examples:
        nexus federation status
        nexus federation status --json
    """
    try:
        timing = CommandTiming()
        with timing.phase("server"), get_service_client(remote_url, remote_api_key) as client:
            data = client.federation_zones()

        def _render(d: dict[str, Any]) -> None:
            zones = d.get("zones", [])
            console.print("[bold cyan]Federation Status[/bold cyan]")
            console.print(f"  Total zones: [yellow]{len(zones)}[/yellow]")

            total_links = sum(z.get("links_count", 0) for z in zones)
            console.print(f"  Total links: [yellow]{total_links}[/yellow]")

            if zones:
                console.print("\n[bold]Zones:[/bold]")
                for z in zones:
                    zone_id = z.get("zone_id", "unknown")
                    links = z.get("links_count", 0)
                    console.print(f"  [cyan]{zone_id}[/cyan]  links={links}")

        render_output(
            data=data,
            output_opts=output_opts,
            timing=timing,
            human_formatter=_render,
        )
    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        raise SystemExit(1) from None


@federation.command("zones")
@add_output_options
@REMOTE_API_KEY_OPTION
@REMOTE_URL_OPTION
def federation_zones(
    output_opts: OutputOptions,
    remote_url: str | None,
    remote_api_key: str | None,
) -> None:
    """List all Raft zones with details.

    \b
    Examples:
        nexus federation zones
        nexus federation zones --json
    """
    try:
        timing = CommandTiming()
        with timing.phase("server"), get_service_client(remote_url, remote_api_key) as client:
            data = client.federation_zones()

        def _render(d: dict[str, Any]) -> None:
            zones = d.get("zones", [])
            if not zones:
                console.print("[dim]No federation zones found[/dim]")
                return

            table = Table(title=f"Federation Zones ({len(zones)})")
            table.add_column("Zone ID", style="cyan")
            table.add_column("Links", justify="right")

            for z in zones:
                table.add_row(
                    z.get("zone_id", "unknown"),
                    str(z.get("links_count", 0)),
                )
            console.print(table)

        render_output(
            data=data,
            output_opts=output_opts,
            timing=timing,
            human_formatter=_render,
        )
    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        raise SystemExit(1) from None


@federation.command("info")
@click.argument("zone_id", type=str)
@add_output_options
@REMOTE_API_KEY_OPTION
@REMOTE_URL_OPTION
def federation_info(
    zone_id: str,
    output_opts: OutputOptions,
    remote_url: str | None,
    remote_api_key: str | None,
) -> None:
    """Show zone cluster info.

    Displays zone_id, node_id, links_count, and has_store for a zone.

    \b
    Examples:
        nexus federation info my-zone
        nexus federation info my-zone --json
    """
    try:
        timing = CommandTiming()
        with timing.phase("server"), get_service_client(remote_url, remote_api_key) as client:
            data = client.federation_cluster_info(zone_id)

        def _render(d: dict[str, Any]) -> None:
            console.print(f"[bold cyan]Zone: {d.get('zone_id', zone_id)}[/bold cyan]")
            console.print(f"  Node ID:     {d.get('node_id', 'N/A')}")
            console.print(f"  Links count: {d.get('links_count', 0)}")
            console.print(f"  Has store:   {d.get('has_store', False)}")

        render_output(
            data=data,
            output_opts=output_opts,
            timing=timing,
            human_formatter=_render,
        )
    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        raise SystemExit(1) from None


@federation.command("share")
@click.argument("path", type=str)
@click.option(
    "--zone-id",
    type=str,
    default=None,
    help="Explicit zone ID for the shared subtree (auto-generated if omitted).",
)
@add_output_options
@REMOTE_API_KEY_OPTION
@REMOTE_URL_OPTION
def federation_share(
    path: str,
    zone_id: str | None,
    output_opts: OutputOptions,
    remote_url: str | None,
    remote_api_key: str | None,
) -> None:
    """Share a subtree for federation.

    Creates a new federation zone from a local path so that peers can join it.

    \b
    Examples:
        nexus federation share /data/shared
        nexus federation share /data/shared --zone-id my-shared-zone
    """
    timing = CommandTiming()
    try:
        with get_service_client(remote_url, remote_api_key) as client, timing.phase("server"):
            data = client.federation_share(path, zone_id=zone_id)

        def _render(_d: dict[str, Any]) -> None:  # noqa: ARG001
            new_zone = data.get("zone_id", "unknown")
            console.print(f"[green]Shared '{path}' as federation zone[/green]")
            console.print(f"  Zone ID: [cyan]{new_zone}[/cyan]")

        render_output(data=data, output_opts=output_opts, timing=timing, human_formatter=_render)
    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        raise SystemExit(1) from None


@federation.command("join")
@click.argument("peer_addr", type=str)
@click.argument("remote_path", type=str)
@click.argument("local_path", type=str)
@add_output_options
@REMOTE_API_KEY_OPTION
@REMOTE_URL_OPTION
def federation_join(
    peer_addr: str,
    remote_path: str,
    local_path: str,
    output_opts: OutputOptions,
    remote_url: str | None,
    remote_api_key: str | None,
) -> None:
    """Join a peer's federation zone.

    Connects to a remote peer and replicates a shared subtree locally.

    \b
    Examples:
        nexus federation join peer1:2126 /shared /local/shared
        nexus federation join 10.0.0.5:2126 /data /mnt/data
    """
    timing = CommandTiming()
    try:
        with get_service_client(remote_url, remote_api_key) as client, timing.phase("server"):
            data = client.federation_join(peer_addr, remote_path, local_path)

        def _render(_d: dict[str, Any]) -> None:  # noqa: ARG001
            joined_zone = data.get("zone_id", "unknown")
            console.print(f"[green]Joined federation zone from {peer_addr}[/green]")
            console.print(f"  Zone ID:     [cyan]{joined_zone}[/cyan]")
            console.print(f"  Remote path: {remote_path}")
            console.print(f"  Local path:  {local_path}")

        render_output(data=data, output_opts=output_opts, timing=timing, human_formatter=_render)
    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        raise SystemExit(1) from None


@federation.command("mount")
@click.option(
    "--parent-zone",
    type=str,
    required=True,
    help="Zone containing the mount point.",
)
@click.option(
    "--path",
    type=str,
    required=True,
    help="Path where the target zone will be mounted.",
)
@click.option(
    "--target-zone",
    type=str,
    required=True,
    help="Zone to mount at the given path.",
)
@add_output_options
@REMOTE_API_KEY_OPTION
@REMOTE_URL_OPTION
def federation_mount(
    parent_zone: str,
    path: str,
    target_zone: str,
    output_opts: OutputOptions,
    remote_url: str | None,
    remote_api_key: str | None,
) -> None:
    """Create a cross-zone mount point.

    Mounts a target zone at a path within the parent zone so that files
    under that path are routed to the target zone's metadata store.

    \b
    Examples:
        nexus federation mount --parent-zone root --path /shared --target-zone team
        nexus federation mount --parent-zone default --path /projects --target-zone proj
    """
    timing = CommandTiming()
    try:
        with get_service_client(remote_url, remote_api_key) as client, timing.phase("server"):
            data = client.federation_mount(parent_zone, path, target_zone)

        def _render(_d: dict[str, Any]) -> None:  # noqa: ARG001
            console.print(
                f"[green]Mounted zone '{target_zone}' at '{path}' in zone '{parent_zone}'[/green]"
            )

        render_output(data=data, output_opts=output_opts, timing=timing, human_formatter=_render)
    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        raise SystemExit(1) from None


@federation.command("unmount")
@click.option(
    "--parent-zone",
    type=str,
    required=True,
    help="Zone containing the mount point.",
)
@click.option(
    "--path",
    type=str,
    required=True,
    help="Path of the mount point to remove.",
)
@add_output_options
@REMOTE_API_KEY_OPTION
@REMOTE_URL_OPTION
def federation_unmount(
    parent_zone: str,
    path: str,
    output_opts: OutputOptions,
    remote_url: str | None,
    remote_api_key: str | None,
) -> None:
    """Remove a cross-zone mount point.

    \b
    Examples:
        nexus federation unmount --parent-zone root --path /shared
        nexus federation unmount --parent-zone default --path /projects
    """
    timing = CommandTiming()
    try:
        with get_service_client(remote_url, remote_api_key) as client, timing.phase("server"):
            data = client.federation_unmount(parent_zone, path)

        def _render(_d: dict[str, Any]) -> None:  # noqa: ARG001
            console.print(f"[green]Unmounted '{path}' from zone '{parent_zone}'[/green]")

        render_output(data=data, output_opts=output_opts, timing=timing, human_formatter=_render)
    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        raise SystemExit(1) from None


def register_commands(cli: click.Group) -> None:
    """Register the federation command group with the main CLI."""
    cli.add_command(federation)
