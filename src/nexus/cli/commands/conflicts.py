"""Conflicts CLI commands — OCC conflict resolution.

Maps to conflicts_* RPC methods via rpc_call().
Issue #2812.
"""

from __future__ import annotations

import click

from nexus.cli.output import OutputOptions, add_output_options, render_output
from nexus.cli.timing import CommandTiming
from nexus.cli.utils import REMOTE_API_KEY_OPTION, REMOTE_URL_OPTION, console, rpc_call


@click.group()
def conflicts() -> None:
    """OCC conflict resolution.

    \b
    List, inspect, and resolve optimistic concurrency control conflicts.

    \b
    Examples:
        nexus conflicts list --json
        nexus conflicts show <conflict-id>
        nexus conflicts resolve <conflict-id> --outcome nexus_wins
    """


@conflicts.command("list")
@add_output_options
@REMOTE_API_KEY_OPTION
@REMOTE_URL_OPTION
def conflicts_list(
    output_opts: OutputOptions,
    remote_url: str | None,
    remote_api_key: str | None,
) -> None:
    """List unresolved conflicts.

    \b
    Examples:
        nexus conflicts list
        nexus conflicts list --json
    """
    timing = CommandTiming()
    try:
        with timing.phase("server"):
            data = rpc_call(remote_url, remote_api_key, "conflicts_list")

        def _render(d: dict) -> None:
            from rich.table import Table

            items = d.get("conflicts", [])
            if not items:
                console.print("[green]No unresolved conflicts[/green]")
                return

            table = Table(title=f"Conflicts ({len(items)})")
            table.add_column("ID", style="dim")
            table.add_column("Path")
            table.add_column("Backend")
            table.add_column("Status", style="dim")

            for c in items:
                table.add_row(
                    c.get("conflict_id", "")[:12],
                    c.get("path", ""),
                    c.get("backend_name", ""),
                    c.get("status", ""),
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


@conflicts.command("show")
@click.argument("conflict_id")
@add_output_options
@REMOTE_API_KEY_OPTION
@REMOTE_URL_OPTION
def conflicts_show(
    conflict_id: str,
    output_opts: OutputOptions,
    remote_url: str | None,
    remote_api_key: str | None,
) -> None:
    """Show both versions of a conflict.

    \b
    Examples:
        nexus conflicts show abc123
        nexus conflicts show abc123 --json
    """
    timing = CommandTiming()
    try:
        with timing.phase("server"):
            data = rpc_call(remote_url, remote_api_key, "conflicts_get", conflict_id=conflict_id)

        def _render(d: dict) -> None:
            console.print(f"[bold cyan]Conflict: {conflict_id}[/bold cyan]")
            console.print(f"  Path:        {d.get('path', 'N/A')}")
            console.print(f"  Backend:     {d.get('backend_name', 'N/A')}")
            console.print(f"  Strategy:    {d.get('strategy', 'N/A')}")
            console.print(f"  Outcome:     {d.get('outcome', 'N/A')}")
            console.print(f"  Status:      {d.get('status', 'N/A')}")
            console.print(f"  Resolved at: {d.get('resolved_at', 'N/A')}")
            nexus_hash = d.get("nexus_content_hash", "N/A")
            backend_hash = d.get("backend_content_hash", "N/A")
            console.print(f"  Nexus hash:  {nexus_hash}")
            console.print(f"  Backend hash:{backend_hash}")

        render_output(
            data=data,
            output_opts=output_opts,
            timing=timing,
            human_formatter=_render,
        )
    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        raise SystemExit(1) from None


@conflicts.command("resolve")
@click.argument("conflict_id")
@click.option(
    "--outcome",
    required=True,
    type=click.Choice(["nexus_wins", "backend_wins"]),
    help="Resolution outcome",
)
@add_output_options
@REMOTE_API_KEY_OPTION
@REMOTE_URL_OPTION
def conflicts_resolve(
    conflict_id: str,
    outcome: str,
    output_opts: OutputOptions,
    remote_url: str | None,
    remote_api_key: str | None,
) -> None:
    """Resolve a conflict.

    \b
    Examples:
        nexus conflicts resolve abc123 --outcome nexus_wins
        nexus conflicts resolve abc123 --outcome backend_wins --json
    """
    timing = CommandTiming()
    try:
        with timing.phase("server"):
            data = rpc_call(
                remote_url,
                remote_api_key,
                "conflicts_resolve",
                conflict_id=conflict_id,
                outcome=outcome,
            )

        render_output(
            data=data,
            output_opts=output_opts,
            timing=timing,
            message=f"Conflict {conflict_id} resolved ({outcome})",
        )
    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        raise SystemExit(1) from None
