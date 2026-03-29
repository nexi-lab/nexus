"""Federation CLI commands — zone status, mounts.

``share`` and ``join`` are node-local operations and live in ``nexusd``.
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
    rpc_call,
)


@click.group()
def federation() -> None:
    """Federation management — zones and mounts.

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

    \b
    Examples:
        nexus federation status
        nexus federation status --json
    """
    try:
        timing = CommandTiming()
        with timing.phase("server"):
            data = rpc_call(remote_url, remote_api_key, "federation_list_zones")

        def _render(d: dict[str, Any]) -> None:
            zones = d.get("zones", [])
            console.print("[bold cyan]Federation Status[/bold cyan]")
            console.print(f"  Total zones: [nexus.warning]{len(zones)}[/nexus.warning]")

            total_links = sum(z.get("links_count", 0) for z in zones)
            console.print(f"  Total links: [nexus.warning]{total_links}[/nexus.warning]")

            if zones:
                console.print("\n[bold]Zones:[/bold]")
                for z in zones:
                    zone_id = z.get("zone_id", "unknown")
                    links = z.get("links_count", 0)
                    console.print(f"  [nexus.value]{zone_id}[/nexus.value]  links={links}")

        render_output(
            data=data,
            output_opts=output_opts,
            timing=timing,
            human_formatter=_render,
        )
    except Exception as e:
        console.print(f"[nexus.error]Error:[/nexus.error] {e}")
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
        with timing.phase("server"):
            data = rpc_call(remote_url, remote_api_key, "federation_list_zones")

        def _render(d: dict[str, Any]) -> None:
            zones = d.get("zones", [])
            if not zones:
                console.print("[nexus.muted]No federation zones found[/nexus.muted]")
                return

            table = Table(title=f"Federation Zones ({len(zones)})")
            table.add_column("Zone ID", style="nexus.value")
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
        console.print(f"[nexus.error]Error:[/nexus.error] {e}")
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

    \b
    Examples:
        nexus federation info my-zone
        nexus federation info my-zone --json
    """
    try:
        timing = CommandTiming()
        with timing.phase("server"):
            data = rpc_call(remote_url, remote_api_key, "federation_cluster_info", zone_id=zone_id)

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
        console.print(f"[nexus.error]Error:[/nexus.error] {e}")
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

    \b
    Examples:
        nexus federation mount --parent-zone root --path /shared --target-zone team
    """
    timing = CommandTiming()
    try:
        with timing.phase("server"):
            data = rpc_call(
                remote_url,
                remote_api_key,
                "federation_mount",
                parent_zone=parent_zone,
                path=path,
                target_zone=target_zone,
            )

        def _render(_d: dict[str, Any]) -> None:  # noqa: ARG001
            console.print(
                f"[nexus.success]Mounted zone '{target_zone}' at '{path}' in zone '{parent_zone}'[/nexus.success]"
            )

        render_output(data=data, output_opts=output_opts, timing=timing, human_formatter=_render)
    except Exception as e:
        console.print(f"[nexus.error]Error:[/nexus.error] {e}")
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
    """
    timing = CommandTiming()
    try:
        with timing.phase("server"):
            data = rpc_call(
                remote_url,
                remote_api_key,
                "federation_unmount",
                parent_zone=parent_zone,
                path=path,
            )

        def _render(_d: dict[str, Any]) -> None:  # noqa: ARG001
            console.print(
                f"[nexus.success]Unmounted '{path}' from zone '{parent_zone}'[/nexus.success]"
            )

        render_output(data=data, output_opts=output_opts, timing=timing, human_formatter=_render)
    except Exception as e:
        console.print(f"[nexus.error]Error:[/nexus.error] {e}")
        raise SystemExit(1) from None


def register_commands(cli: click.Group) -> None:
    """Register the federation command group with the main CLI."""
    cli.add_command(federation)
