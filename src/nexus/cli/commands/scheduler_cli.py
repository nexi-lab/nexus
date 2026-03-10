"""Scheduler CLI commands — fair-share priority queue visibility.

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
    View queue status, list pending tasks, and control dispatch.

    \b
    Examples:
        nexus scheduler status --json
        nexus scheduler queue
        nexus scheduler pause
    """


@scheduler.command("status")
@add_output_options
@REMOTE_API_KEY_OPTION
@REMOTE_URL_OPTION
@service_command(client_class=SchedulerClient)
def scheduler_status(client: SchedulerClient) -> ServiceResult:
    """Show queue depth, active workers, and throughput.

    \b
    Examples:
        nexus scheduler status
        nexus scheduler status --json
    """
    data = client.status()

    def _render(d: dict) -> None:
        from nexus.cli.utils import console

        console.print("[bold cyan]Scheduler Status[/bold cyan]")
        console.print(f"  Queue Depth:     {d.get('queue_depth', 'N/A')}")
        console.print(f"  Active Workers:  {d.get('active_workers', 'N/A')}")
        console.print(f"  Throughput:      {d.get('throughput', 'N/A')} tasks/min")
        console.print(f"  Avg Wait Time:   {d.get('avg_wait_ms', 'N/A')}ms")

    return ServiceResult(data=data, human_formatter=_render)


@scheduler.command("queue")
@click.option("--limit", default=20, show_default=True, help="Maximum tasks to show")
@add_output_options
@REMOTE_API_KEY_OPTION
@REMOTE_URL_OPTION
@service_command(client_class=SchedulerClient)
def scheduler_queue(client: SchedulerClient, limit: int) -> ServiceResult:
    """List pending tasks with priority.

    \b
    Examples:
        nexus scheduler queue
        nexus scheduler queue --limit 50 --json
    """
    # Get metrics which includes queue information
    data = client.status()

    def _render(d: dict) -> None:
        from rich.table import Table

        from nexus.cli.utils import console

        tasks = d.get("pending_tasks", d.get("tasks", []))
        if not tasks:
            console.print("[yellow]No pending tasks[/yellow]")
            return

        table = Table(title=f"Pending Tasks ({len(tasks)})")
        table.add_column("Task ID", style="dim")
        table.add_column("Priority")
        table.add_column("Agent")
        table.add_column("Submitted", style="dim")

        for task in tasks[:limit]:
            table.add_row(
                task.get("task_id", "")[:12],
                task.get("priority_class", ""),
                task.get("agent_id", ""),
                task.get("submitted_at", "")[:19],
            )
        console.print(table)

    return ServiceResult(data=data, human_formatter=_render)


@scheduler.command("pause")
@add_output_options
@REMOTE_API_KEY_OPTION
@REMOTE_URL_OPTION
@service_command(client_class=SchedulerClient)
def scheduler_pause(client: SchedulerClient) -> ServiceResult:
    """Pause task dispatch.

    \b
    Examples:
        nexus scheduler pause
    """
    data = client.pause()
    return ServiceResult(data=data, message="Scheduler paused")


@scheduler.command("resume")
@add_output_options
@REMOTE_API_KEY_OPTION
@REMOTE_URL_OPTION
@service_command(client_class=SchedulerClient)
def scheduler_resume(client: SchedulerClient) -> ServiceResult:
    """Resume task dispatch.

    \b
    Examples:
        nexus scheduler resume
    """
    data = client.resume()
    return ServiceResult(data=data, message="Scheduler resumed")
