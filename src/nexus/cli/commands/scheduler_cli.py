"""Scheduler CLI commands -- fair-share priority queue visibility.

Maps to /api/v2/scheduler/* endpoints via SchedulerClient.
Issue #2812.
"""

from __future__ import annotations

import click

from nexus.cli.clients.scheduler import SchedulerClient
from nexus.cli.output import add_output_options
from nexus.cli.service_command import ServiceResult, service_command
from nexus.cli.utils import REMOTE_API_KEY_OPTION, REMOTE_URL_OPTION


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
@service_command(client_class=SchedulerClient)
def scheduler_status(client: SchedulerClient) -> ServiceResult:
    """Show queue depth, fair-share allocations, and HRRN mode.

    \b
    Examples:
        nexus scheduler status
        nexus scheduler status --json
    """
    data = client.status()

    def _render(d: dict) -> None:
        from rich.table import Table

        from nexus.cli.theme import console

        console.print("[bold nexus.value]Scheduler Status[/bold nexus.value]")
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
            console.print("  [nexus.warning]No queued tasks[/nexus.warning]")

        # Fair-share summary
        fair_share = d.get("fair_share", {})
        if fair_share:
            console.print("\n[bold]Fair-Share Allocations[/bold]")
            for agent_id, info in fair_share.items():
                running = info.get("running_count", 0)
                max_c = info.get("max_concurrent", 0)
                avail = info.get("available_slots", 0)
                console.print(f"  {agent_id}: {running}/{max_c} running, {avail} slots available")

    return ServiceResult(data=data, human_formatter=_render)


@scheduler.command("queue")
@add_output_options
@REMOTE_API_KEY_OPTION
@REMOTE_URL_OPTION
@service_command(client_class=SchedulerClient)
def scheduler_queue(client: SchedulerClient) -> ServiceResult:
    """Show aggregate queue metrics by priority class.

    \b
    Examples:
        nexus scheduler queue
        nexus scheduler queue --json
    """
    data = client.status()

    def _render(d: dict) -> None:
        from rich.table import Table

        from nexus.cli.theme import console

        queue_by_class = d.get("queue_by_class", [])
        if not queue_by_class:
            console.print("[nexus.warning]No queued tasks[/nexus.warning]")
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

    return ServiceResult(data=data, human_formatter=_render)
