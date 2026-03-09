"""Pay CLI commands — agent balance, transfer, and history.

Maps to /api/v2/pay/* REST endpoints via NexusServiceClient.
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
def pay() -> None:
    """Agent payment operations.

    \b
    Prerequisites:
        - Running Nexus server
        - Server URL (set via NEXUS_URL or --remote-url)
        - API key (set via NEXUS_API_KEY or --remote-api-key)

    \b
    Examples:
        nexus pay balance
        nexus pay transfer bob 10.00 --memo "For data access"
        nexus pay history --limit 20 --json
    """


@pay.command("balance")
@click.argument("agent_id", required=False, default=None)
@JSON_OUTPUT_OPTION
@REMOTE_API_KEY_OPTION
@REMOTE_URL_OPTION
def pay_balance(
    agent_id: str | None,
    json_output: bool,
    remote_url: str | None,
    remote_api_key: str | None,
) -> None:
    """Show agent credit balance.

    \b
    Examples:
        nexus pay balance              # Own balance
        nexus pay balance agent_abc    # Specific agent
        nexus pay balance --json       # JSON output
    """
    try:
        with get_service_client(remote_url, remote_api_key) as client:
            data = client.pay_balance(agent_id)

        def _render(d: dict) -> None:
            console.print("[bold cyan]Balance[/bold cyan]")
            console.print(f"  Available: [green]{d.get('available', '0')}[/green]")
            console.print(f"  Reserved:  [yellow]{d.get('reserved', '0')}[/yellow]")
            console.print(f"  Total:     [bold]{d.get('total', '0')}[/bold]")

        output_result(data, json_output, _render)
    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        raise SystemExit(1) from None


@pay.command("transfer")
@click.argument("to")
@click.argument("amount")
@click.option("--memo", default="", help="Transfer memo/description")
@click.option(
    "--method",
    type=click.Choice(["auto", "credits", "x402"]),
    default="auto",
    help="Payment method",
    show_default=True,
)
@JSON_OUTPUT_OPTION
@REMOTE_API_KEY_OPTION
@REMOTE_URL_OPTION
def pay_transfer(
    to: str,
    amount: str,
    memo: str,
    method: str,
    json_output: bool,
    remote_url: str | None,
    remote_api_key: str | None,
) -> None:
    """Transfer credits to another agent.

    \b
    Examples:
        nexus pay transfer bob 10.00
        nexus pay transfer bob 50.00 --memo "Data license fee"
        nexus pay transfer 0x1234...abcd 5.00 --method x402
    """
    try:
        with get_service_client(remote_url, remote_api_key) as client:
            data = client.pay_transfer(to, amount, memo=memo, method=method)

        def _render(d: dict) -> None:
            console.print("[green]Transfer successful[/green]")
            console.print(f"  ID:     {d.get('id', 'N/A')}")
            console.print(f"  To:     {d.get('to_agent', to)}")
            console.print(f"  Amount: {d.get('amount', amount)}")
            console.print(f"  Method: {d.get('method', method)}")
            if d.get("memo"):
                console.print(f"  Memo:   {d['memo']}")

        output_result(data, json_output, _render)
    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        raise SystemExit(1) from None


@pay.command("history")
@click.option("--since", default=None, help="Start time (ISO format or relative, e.g., '1h')")
@click.option("--limit", default=20, help="Maximum entries to show", show_default=True)
@JSON_OUTPUT_OPTION
@REMOTE_API_KEY_OPTION
@REMOTE_URL_OPTION
def pay_history(
    since: str | None,
    limit: int,
    json_output: bool,
    remote_url: str | None,
    remote_api_key: str | None,
) -> None:
    """Show payment history.

    \b
    Examples:
        nexus pay history
        nexus pay history --since 2024-01-01 --limit 50
        nexus pay history --json
    """
    try:
        with get_service_client(remote_url, remote_api_key) as client:
            data = client.pay_history(since=since, limit=limit)

        def _render(d: dict) -> None:
            from rich.table import Table

            txns = d.get("transactions", [])
            if not txns:
                console.print("[yellow]No transactions found[/yellow]")
                return

            table = Table(title="Payment History")
            table.add_column("Time", style="dim")
            table.add_column("From")
            table.add_column("To")
            table.add_column("Amount", justify="right", style="green")
            table.add_column("Status")

            for tx in txns:
                table.add_row(
                    tx.get("created_at", "")[:19],
                    tx.get("buyer_agent_id", ""),
                    tx.get("seller_agent_id", ""),
                    tx.get("amount", ""),
                    tx.get("status", ""),
                )
            console.print(table)

        output_result(data, json_output, _render)
    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        raise SystemExit(1) from None
