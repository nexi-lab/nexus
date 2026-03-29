"""Conflicts CLI commands — OCC conflict resolution.

Maps to /api/v2/sync/conflicts/* endpoints via ConflictsClient.
Issue #2812.
"""

from __future__ import annotations

import click

from nexus.cli.clients.conflicts import ConflictsClient
from nexus.cli.output import add_output_options
from nexus.cli.service_command import ServiceResult, service_command
from nexus.cli.utils import REMOTE_API_KEY_OPTION, REMOTE_URL_OPTION


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
@service_command(client_class=ConflictsClient)
def conflicts_list(client: ConflictsClient) -> ServiceResult:
    """List unresolved conflicts.

    \b
    Examples:
        nexus conflicts list
        nexus conflicts list --json
    """
    data = client.list()

    def _render(d: dict) -> None:
        from rich.table import Table

        from nexus.cli.theme import console

        items = d.get("conflicts", [])
        if not items:
            console.print("[nexus.success]No unresolved conflicts[/nexus.success]")
            return

        table = Table(title=f"Conflicts ({len(items)})")
        table.add_column("ID", style="nexus.muted")
        table.add_column("Path")
        table.add_column("Backend")
        table.add_column("Status", style="nexus.muted")

        for c in items:
            table.add_row(
                c.get("conflict_id", "")[:12],
                c.get("path", ""),
                c.get("backend_name", ""),
                c.get("status", ""),
            )
        console.print(table)

    return ServiceResult(data=data, human_formatter=_render)


@conflicts.command("show")
@click.argument("conflict_id")
@add_output_options
@REMOTE_API_KEY_OPTION
@REMOTE_URL_OPTION
@service_command(client_class=ConflictsClient)
def conflicts_show(client: ConflictsClient, conflict_id: str) -> ServiceResult:
    """Show both versions of a conflict.

    \b
    Examples:
        nexus conflicts show abc123
        nexus conflicts show abc123 --json
    """
    data = client.show(conflict_id)

    def _render(d: dict) -> None:
        from nexus.cli.theme import console

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

    return ServiceResult(data=data, human_formatter=_render)


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
@service_command(client_class=ConflictsClient)
def conflicts_resolve(client: ConflictsClient, conflict_id: str, outcome: str) -> ServiceResult:
    """Resolve a conflict.

    \b
    Examples:
        nexus conflicts resolve abc123 --outcome nexus_wins
        nexus conflicts resolve abc123 --outcome backend_wins --json
    """
    data = client.resolve(conflict_id, outcome=outcome)
    return ServiceResult(data=data, message=f"Conflict {conflict_id} resolved ({outcome})")
