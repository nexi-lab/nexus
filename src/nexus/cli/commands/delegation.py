"""Delegation CLI commands — agent identity delegation lifecycle.

Maps to /api/v2/agents/delegate/* endpoints via DelegationClient.
Issue #2812.
"""

from __future__ import annotations

import click

from nexus.cli.clients.delegation import DelegationClient
from nexus.cli.output import add_output_options
from nexus.cli.service_command import ServiceResult, service_command
from nexus.cli.utils import REMOTE_API_KEY_OPTION, REMOTE_URL_OPTION


@click.group()
def delegation() -> None:
    """Agent identity delegation.

    \b
    Delegate capabilities from a coordinator agent to a worker agent.
    Supports COPY, CLEAN, and SHARED delegation modes.

    \b
    Examples:
        nexus delegation create coord worker --scope "/project/*"
        nexus delegation list --json
        nexus delegation revoke <delegation-id>
    """


@delegation.command("create")
@click.argument("coordinator")
@click.argument("worker")
@click.option(
    "--mode",
    type=click.Choice(["COPY", "CLEAN", "SHARED"]),
    default="COPY",
    show_default=True,
    help="Delegation mode",
)
@click.option("--scope", "scope_prefix", default=None, help="Scope prefix (e.g., /project/*)")
@click.option("--ttl", "ttl_seconds", type=int, default=None, help="Time-to-live in seconds")
@click.option("--zone-id", default=None, help="Zone ID for delegation")
@add_output_options
@REMOTE_API_KEY_OPTION
@REMOTE_URL_OPTION
@service_command(client_class=DelegationClient)
def delegation_create(
    client: DelegationClient,
    coordinator: str,
    worker: str,
    mode: str,
    scope_prefix: str | None,
    ttl_seconds: int | None,
    zone_id: str | None,
) -> ServiceResult:
    """Delegate identity from coordinator to worker.

    \b
    Examples:
        nexus delegation create coord worker
        nexus delegation create coord worker --mode CLEAN --scope "/project/*"
        nexus delegation create coord worker --ttl 3600 --json
    """
    data = client.create(
        coordinator,
        worker,
        mode=mode,
        scope_prefix=scope_prefix,
        ttl_seconds=ttl_seconds,
        zone_id=zone_id,
    )

    def _render(d: dict) -> None:
        from nexus.cli.theme import console

        console.print("[nexus.success]Delegation created[/nexus.success]")
        console.print(f"  ID:          {d.get('delegation_id', 'N/A')}")
        console.print(f"  Coordinator: {d.get('coordinator_agent_id', coordinator)}")
        console.print(f"  Worker:      {d.get('worker_id', worker)}")
        console.print(f"  Mode:        {d.get('delegation_mode', mode)}")
        if d.get("scope_prefix"):
            console.print(f"  Scope:       {d['scope_prefix']}")

    return ServiceResult(data=data, human_formatter=_render)


@delegation.command("list")
@click.option(
    "--coordinator", "coordinator_agent_id", default=None, help="Filter by coordinator agent ID"
)
@click.option("--limit", default=50, show_default=True, help="Maximum entries")
@add_output_options
@REMOTE_API_KEY_OPTION
@REMOTE_URL_OPTION
@service_command(client_class=DelegationClient)
def delegation_list(
    client: DelegationClient,
    coordinator_agent_id: str | None,
    limit: int,
) -> ServiceResult:
    """List active delegations.

    \b
    Examples:
        nexus delegation list
        nexus delegation list --coordinator coord_agent --json
    """
    data = client.list(coordinator_agent_id, limit=limit)

    def _render(d: dict) -> None:
        from rich.table import Table

        from nexus.cli.theme import console

        delegations = d.get("delegations", [])
        if not delegations:
            console.print("[nexus.warning]No active delegations[/nexus.warning]")
            return

        table = Table(title=f"Delegations ({len(delegations)})")
        table.add_column("ID", style="nexus.muted")
        table.add_column("Coordinator")
        table.add_column("Worker")
        table.add_column("Mode")
        table.add_column("Status")
        table.add_column("Created", style="nexus.muted")

        for dlg in delegations:
            table.add_row(
                dlg.get("delegation_id", "")[:12],
                dlg.get("coordinator_agent_id", ""),
                dlg.get("worker_id", ""),
                dlg.get("delegation_mode", ""),
                dlg.get("status", ""),
                dlg.get("created_at", "")[:19],
            )
        console.print(table)

    return ServiceResult(data=data, human_formatter=_render)


@delegation.command("revoke")
@click.argument("delegation_id")
@add_output_options
@REMOTE_API_KEY_OPTION
@REMOTE_URL_OPTION
@service_command(client_class=DelegationClient)
def delegation_revoke(client: DelegationClient, delegation_id: str) -> ServiceResult:
    """Revoke a delegation.

    \b
    Examples:
        nexus delegation revoke abc123
        nexus delegation revoke abc123 --json
    """
    data = client.revoke(delegation_id)
    return ServiceResult(data=data, message=f"Delegation {delegation_id} revoked")


@delegation.command("show")
@click.argument("delegation_id")
@add_output_options
@REMOTE_API_KEY_OPTION
@REMOTE_URL_OPTION
@service_command(client_class=DelegationClient)
def delegation_show(client: DelegationClient, delegation_id: str) -> ServiceResult:
    """Show delegation details and chain.

    \b
    Examples:
        nexus delegation show abc123
        nexus delegation show abc123 --json
    """
    data = client.show(delegation_id)

    def _render(d: dict) -> None:
        from nexus.cli.theme import console

        console.print(f"[bold cyan]Delegation Chain: {delegation_id}[/bold cyan]")
        chain = d.get("chain", [d])
        for i, link in enumerate(chain):
            prefix = "  └─" if i == len(chain) - 1 else "  ├─"
            console.print(
                f"{prefix} {link.get('coordinator_agent_id', '?')} → "
                f"{link.get('worker_id', '?')} [{link.get('delegation_mode', '?')}]"
            )

    return ServiceResult(data=data, human_formatter=_render)
