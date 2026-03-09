"""Snapshot CLI commands — create, list, and restore transactional snapshots.

Maps to /api/v2/snapshots/* REST endpoints via NexusServiceClient.
Issue #2811.
"""

import click

from nexus.cli.utils import (
    JSON_OUTPUT_OPTION,
    REMOTE_API_KEY_OPTION,
    REMOTE_URL_OPTION,
    console,
    get_service_client,
    output_result,
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
@JSON_OUTPUT_OPTION
@REMOTE_API_KEY_OPTION
@REMOTE_URL_OPTION
def snapshot_create(
    description: str | None,
    ttl: int,
    json_output: bool,
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
        with get_service_client(remote_url, remote_api_key) as client:
            data = client.snapshot_create(description=description, ttl_seconds=ttl)

        def _render(d: dict) -> None:
            console.print("[green]Snapshot created[/green]")
            console.print(f"  Transaction ID: [bold]{d.get('transaction_id', 'N/A')}[/bold]")
            console.print(f"  Status:         {d.get('status', 'N/A')}")
            console.print(f"  Expires:        {d.get('expires_at', 'N/A')[:19]}")
            if d.get("description"):
                console.print(f"  Description:    {d['description']}")

        output_result(data, json_output, _render)
    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        raise SystemExit(1) from None


@snapshot.command("list")
@JSON_OUTPUT_OPTION
@REMOTE_API_KEY_OPTION
@REMOTE_URL_OPTION
def snapshot_list(
    json_output: bool,
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
        with get_service_client(remote_url, remote_api_key) as client:
            data = client.snapshot_list()

        def _render(d: dict) -> None:
            from rich.table import Table

            txns = d.get("transactions", [])
            if not txns:
                console.print("[yellow]No snapshots found[/yellow]")
                return

            table = Table(title=f"Snapshots ({len(txns)})")
            table.add_column("Transaction ID", style="bold")
            table.add_column("Status")
            table.add_column("Description")
            table.add_column("Created", style="dim")
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

        output_result(data, json_output, _render)
    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        raise SystemExit(1) from None


@snapshot.command("restore")
@click.argument("txn_id")
@JSON_OUTPUT_OPTION
@REMOTE_API_KEY_OPTION
@REMOTE_URL_OPTION
def snapshot_restore(
    txn_id: str,
    json_output: bool,
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
        with get_service_client(remote_url, remote_api_key) as client:
            data = client.snapshot_restore(txn_id)

        def _render(d: dict) -> None:
            console.print(f"[green]Snapshot restored:[/green] {txn_id}")
            if d:
                console.print(f"  Status: {d.get('status', 'rolled_back')}")

        output_result(data, json_output, _render)
    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        raise SystemExit(1) from None
