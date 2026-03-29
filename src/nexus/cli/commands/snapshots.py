"""Snapshot CLI commands — create, list, and restore transactional snapshots."""

import click

from nexus.cli.output import OutputOptions, add_output_options, render_output
from nexus.cli.timing import CommandTiming
from nexus.cli.utils import (
    REMOTE_API_KEY_OPTION,
    REMOTE_URL_OPTION,
    console,
    rpc_call,
)


@click.group()
def snapshot() -> None:
    """Transactional snapshot management.

    \b
    Prerequisites:
        - Running Nexus server
        - Server URL (set via NEXUS_URL or --remote-url)
        - API key (set via NEXUS_API_KEY or --remote-api-key)

    \b
    Examples:
        nexus snapshot create --description "Before migration"
        nexus snapshot list --json
        nexus snapshot restore <txn_id>
    """


@snapshot.command("create")
@click.option("--description", default=None, help="Snapshot description")
@click.option(
    "--ttl",
    default=3600,
    help="Time-to-live in seconds (60-86400)",
    show_default=True,
)
@add_output_options
@REMOTE_API_KEY_OPTION
@REMOTE_URL_OPTION
def snapshot_create(
    description: str | None,
    ttl: int,
    output_opts: OutputOptions,
    remote_url: str | None,
    remote_api_key: str | None,
) -> None:
    """Create a new transactional snapshot.

    \b
    Examples:
        nexus snapshot create
        nexus snapshot create --description "Pre-deploy backup"
        nexus snapshot create --ttl 7200 --json
    """
    try:
        timing = CommandTiming()
        with timing.phase("server"):
            data = rpc_call(
                remote_url,
                remote_api_key,
                "snapshot_create",
                description=description,
                ttl_seconds=ttl,
            )

        def _render(d: dict) -> None:
            console.print("[nexus.success]Snapshot created[/nexus.success]")
            console.print(f"  Transaction ID: [bold]{d.get('transaction_id', 'N/A')}[/bold]")
            console.print(f"  Status:         {d.get('status', 'N/A')}")
            expires = d.get("expires_at", "N/A")
            console.print(
                f"  Expires:        {expires[:19] if expires and expires != 'N/A' else 'N/A'}"
            )
            if d.get("description"):
                console.print(f"  Description:    {d['description']}")

        render_output(
            data=data,
            output_opts=output_opts,
            timing=timing,
            human_formatter=_render,
        )
    except Exception as e:
        console.print(f"[nexus.error]Error:[/nexus.error] {e}")
        raise SystemExit(1) from None


@snapshot.command("list")
@add_output_options
@REMOTE_API_KEY_OPTION
@REMOTE_URL_OPTION
def snapshot_list(
    output_opts: OutputOptions,
    remote_url: str | None,
    remote_api_key: str | None,
) -> None:
    """List snapshots/transactions.

    \b
    Examples:
        nexus snapshot list
        nexus snapshot list --json
    """
    try:
        timing = CommandTiming()
        with timing.phase("server"):
            data = rpc_call(remote_url, remote_api_key, "snapshot_list")

        def _render(d: dict) -> None:
            from rich.table import Table

            txns = d.get("transactions", [])
            if not txns:
                console.print("[nexus.warning]No snapshots found[/nexus.warning]")
                return

            table = Table(title=f"Snapshots ({len(txns)})")
            table.add_column("Transaction ID", style="bold")
            table.add_column("Status")
            table.add_column("Description")
            table.add_column("Created", style="nexus.muted")
            table.add_column("Entries", justify="right")

            for tx in txns:
                status = tx.get("status", "")
                status_style = (
                    "green"
                    if status == "committed"
                    else "red"
                    if status == "rolled_back"
                    else "yellow"
                )
                table.add_row(
                    tx.get("transaction_id", "")[:12] + "...",
                    f"[{status_style}]{status}[/{status_style}]",
                    tx.get("description", "") or "-",
                    tx.get("created_at", "")[:19],
                    str(tx.get("entry_count", 0)),
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


@snapshot.command("restore")
@click.argument("txn_id")
@add_output_options
@REMOTE_API_KEY_OPTION
@REMOTE_URL_OPTION
def snapshot_restore(
    txn_id: str,
    output_opts: OutputOptions,
    remote_url: str | None,
    remote_api_key: str | None,
) -> None:
    """Restore (rollback) a snapshot transaction.

    \b
    Examples:
        nexus snapshot restore abc123
        nexus snapshot restore abc123 --json
    """
    try:
        timing = CommandTiming()
        with timing.phase("server"):
            data = rpc_call(remote_url, remote_api_key, "snapshot_restore", txn_id=txn_id)

        def _render(d: dict) -> None:
            console.print(f"[nexus.success]Snapshot restored:[/nexus.success] {txn_id}")
            if d:
                console.print(f"  Status: {d.get('status', 'rolled_back')}")

        render_output(
            data=data,
            output_opts=output_opts,
            timing=timing,
            human_formatter=_render,
        )
    except Exception as e:
        console.print(f"[nexus.error]Error:[/nexus.error] {e}")
        raise SystemExit(1) from None
