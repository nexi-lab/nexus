"""Delegation CLI commands — agent identity delegation lifecycle.

Maps to delegation_* RPC methods via rpc_call().
Issue #2812.
"""

from __future__ import annotations

import click

from nexus.cli.output import OutputOptions, add_output_options, render_output
from nexus.cli.timing import CommandTiming
from nexus.cli.utils import REMOTE_API_KEY_OPTION, REMOTE_URL_OPTION, console, rpc_call


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
def delegation_create(
    coordinator: str,
    worker: str,
    mode: str,
    scope_prefix: str | None,
    ttl_seconds: int | None,
    zone_id: str | None,
    output_opts: OutputOptions,
    remote_url: str | None,
    remote_api_key: str | None,
) -> None:
    """Delegate identity from coordinator to worker.

    \b
    Examples:
        nexus delegation create coord worker
        nexus delegation create coord worker --mode CLEAN --scope "/project/*"
        nexus delegation create coord worker --ttl 3600 --json
    """
    timing = CommandTiming()
    try:
        with timing.phase("server"):
            data = rpc_call(
                remote_url,
                remote_api_key,
                "delegation_create",
                coordinator=coordinator,
                worker=worker,
                mode=mode,
                scope_prefix=scope_prefix,
                ttl_seconds=ttl_seconds,
                zone_id=zone_id,
            )

        def _render(d: dict) -> None:
            console.print("[green]Delegation created[/green]")
            console.print(f"  ID:          {d.get('delegation_id', 'N/A')}")
            console.print(f"  Coordinator: {d.get('coordinator_agent_id', coordinator)}")
            console.print(f"  Worker:      {d.get('worker_id', worker)}")
            console.print(f"  Mode:        {d.get('delegation_mode', mode)}")
            if d.get("scope_prefix"):
                console.print(f"  Scope:       {d['scope_prefix']}")

        render_output(
            data=data,
            output_opts=output_opts,
            timing=timing,
            human_formatter=_render,
        )
    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        raise SystemExit(1) from None


@delegation.command("list")
@click.option(
    "--coordinator", "coordinator_agent_id", default=None, help="Filter by coordinator agent ID"
)
@click.option("--limit", default=50, show_default=True, help="Maximum entries")
@add_output_options
@REMOTE_API_KEY_OPTION
@REMOTE_URL_OPTION
def delegation_list(
    coordinator_agent_id: str | None,
    limit: int,
    output_opts: OutputOptions,
    remote_url: str | None,
    remote_api_key: str | None,
) -> None:
    """List active delegations.

    \b
    Examples:
        nexus delegation list
        nexus delegation list --coordinator coord_agent --json
    """
    timing = CommandTiming()
    try:
        with timing.phase("server"):
            data = rpc_call(
                remote_url,
                remote_api_key,
                "delegation_list",
                coordinator_agent_id=coordinator_agent_id,
                limit=limit,
            )

        def _render(d: dict) -> None:
            from rich.table import Table

            delegations = d.get("delegations", [])
            if not delegations:
                console.print("[yellow]No active delegations[/yellow]")
                return

            table = Table(title=f"Delegations ({len(delegations)})")
            table.add_column("ID", style="dim")
            table.add_column("Coordinator")
            table.add_column("Worker")
            table.add_column("Mode")
            table.add_column("Status")
            table.add_column("Created", style="dim")

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

        render_output(
            data=data,
            output_opts=output_opts,
            timing=timing,
            human_formatter=_render,
        )
    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        raise SystemExit(1) from None


@delegation.command("revoke")
@click.argument("delegation_id")
@add_output_options
@REMOTE_API_KEY_OPTION
@REMOTE_URL_OPTION
def delegation_revoke(
    delegation_id: str,
    output_opts: OutputOptions,
    remote_url: str | None,
    remote_api_key: str | None,
) -> None:
    """Revoke a delegation.

    \b
    Examples:
        nexus delegation revoke abc123
        nexus delegation revoke abc123 --json
    """
    timing = CommandTiming()
    try:
        with timing.phase("server"):
            data = rpc_call(
                remote_url,
                remote_api_key,
                "delegation_revoke",
                delegation_id=delegation_id,
            )

        render_output(
            data=data,
            output_opts=output_opts,
            timing=timing,
            message=f"Delegation {delegation_id} revoked",
        )
    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        raise SystemExit(1) from None


@delegation.command("show")
@click.argument("delegation_id")
@add_output_options
@REMOTE_API_KEY_OPTION
@REMOTE_URL_OPTION
def delegation_show(
    delegation_id: str,
    output_opts: OutputOptions,
    remote_url: str | None,
    remote_api_key: str | None,
) -> None:
    """Show delegation details and chain.

    \b
    Examples:
        nexus delegation show abc123
        nexus delegation show abc123 --json
    """
    timing = CommandTiming()
    try:
        with timing.phase("server"):
            data = rpc_call(
                remote_url,
                remote_api_key,
                "delegation_chain",
                delegation_id=delegation_id,
            )

        def _render(d: dict) -> None:
            console.print(f"[bold cyan]Delegation Chain: {delegation_id}[/bold cyan]")
            chain = d.get("chain", [d])
            for i, link in enumerate(chain):
                prefix = "  └─" if i == len(chain) - 1 else "  ├─"
                console.print(
                    f"{prefix} {link.get('coordinator_agent_id', '?')} → "
                    f"{link.get('worker_id', '?')} [{link.get('delegation_mode', '?')}]"
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
