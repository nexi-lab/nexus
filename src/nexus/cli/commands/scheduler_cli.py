"""Scheduler CLI commands -- fair-share priority queue visibility.

Maps to scheduler_* RPC methods via rpc_call().
Issue #2812.
"""

from __future__ import annotations

import click

from nexus.cli.output import OutputOptions, add_output_options, render_output
from nexus.cli.timing import CommandTiming
from nexus.cli.utils import REMOTE_API_KEY_OPTION, REMOTE_URL_OPTION, console, rpc_call


@click.group()
def scheduler() -> None:
    """Fair-share priority queue management.

    \b
    View queue status and inspect priority-class metrics.

    \b
    Examples:
        nexus scheduler status --json
        nexus scheduler queue
    """


@scheduler.command("status")
@add_output_options
@REMOTE_API_KEY_OPTION
@REMOTE_URL_OPTION
def scheduler_status(
    output_opts: OutputOptions,
    remote_url: str | None,
    remote_api_key: str | None,
) -> None:
    """Show queue depth, fair-share allocations, and HRRN mode.

    \b
    Examples:
        nexus scheduler status
        nexus scheduler status --json
    """
    timing = CommandTiming()
    try:
        with timing.phase("server"):
            data = rpc_call(remote_url, remote_api_key, "scheduler_status")

        def _render(d: dict) -> None:
            from rich.table import Table

            console.print("[bold cyan]Scheduler Status[/bold cyan]")
            console.print(f"  HRRN Enabled:  {d.get('use_hrrn', 'N/A')}")

            # Queue by class table
            queue_by_class = d.get("queue_by_class", [])
            if queue_by_class:
                table = Table(title="Queue by Priority Class")
                table.add_column("Priority Class")
                table.add_column("Count", justify="right")
                table.add_column("Avg Wait", justify="right")
                table.add_column("Max Wait", justify="right")
                for entry in queue_by_class:
                    table.add_row(
                        str(entry.get("priority_class", "")),
                        str(entry.get("cnt", 0)),
                        str(entry.get("avg_wait", "")),
                        str(entry.get("max_wait", "")),
                    )
                console.print(table)
            else:
                console.print("  [yellow]No queued tasks[/yellow]")

            # Fair-share summary
            fair_share = d.get("fair_share", {})
            if fair_share:
                console.print("\n[bold]Fair-Share Allocations[/bold]")
                for agent_id, info in fair_share.items():
                    running = info.get("running_count", 0)
                    max_c = info.get("max_concurrent", 0)
                    avail = info.get("available_slots", 0)
                    console.print(
                        f"  {agent_id}: {running}/{max_c} running, {avail} slots available"
                    )

        render_output(
            data=data,
            output_opts=output_opts,
            timing=timing,
            human_formatter=_render,
        )
    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        raise SystemExit(1) from None


@scheduler.command("queue")
@add_output_options
@REMOTE_API_KEY_OPTION
@REMOTE_URL_OPTION
def scheduler_queue(
    output_opts: OutputOptions,
    remote_url: str | None,
    remote_api_key: str | None,
) -> None:
    """Show aggregate queue metrics by priority class.

    \b
    Examples:
        nexus scheduler queue
        nexus scheduler queue --json
    """
    timing = CommandTiming()
    try:
        with timing.phase("server"):
            data = rpc_call(remote_url, remote_api_key, "scheduler_status")

        def _render(d: dict) -> None:
            from rich.table import Table

            queue_by_class = d.get("queue_by_class", [])
            if not queue_by_class:
                console.print("[yellow]No queued tasks[/yellow]")
                return

            table = Table(title=f"Queue Metrics ({len(queue_by_class)} classes)")
            table.add_column("Priority Class")
            table.add_column("Count", justify="right")
            table.add_column("Avg Wait", justify="right")
            table.add_column("Max Wait", justify="right")
            for entry in queue_by_class:
                table.add_row(
                    str(entry.get("priority_class", "")),
                    str(entry.get("cnt", 0)),
                    str(entry.get("avg_wait", "")),
                    str(entry.get("max_wait", "")),
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
