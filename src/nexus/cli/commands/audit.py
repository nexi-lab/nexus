"""Audit CLI commands — transaction listing and export."""

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
def audit() -> None:
    """Audit trail for exchange transactions.

    \b
    Prerequisites:
        - Running Nexus server with database authentication
        - Server URL (set via NEXUS_URL or --remote-url)
        - API key (set via NEXUS_API_KEY or --remote-api-key)

    \b
    Examples:
        nexus audit list --since 1h
        nexus audit list --agent-id alice --json
        nexus audit export --format csv --output report.csv
    """


@audit.command("list")
@click.option("--since", default=None, help="Start time (ISO format)")
@click.option("--until", default=None, help="End time (ISO format)")
@click.option("--agent-id", default=None, help="Filter by agent ID")
@click.option("--action", default=None, help="Filter by action/status")
@click.option("--limit", default=50, help="Maximum entries", show_default=True)
@add_output_options
@REMOTE_API_KEY_OPTION
@REMOTE_URL_OPTION
def audit_list(
    since: str | None,
    until: str | None,
    agent_id: str | None,
    action: str | None,
    limit: int,
    output_opts: OutputOptions,
    remote_url: str | None,
    remote_api_key: str | None,
) -> None:
    """List audit trail entries.

    \b
    Examples:
        nexus audit list
        nexus audit list --since 2024-01-01 --limit 100
        nexus audit list --agent-id alice --json
    """
    timing = CommandTiming()
    try:
        with timing.phase("server"):
            data = rpc_call(
                remote_url,
                remote_api_key,
                "audit_list",
                since=since,
                until=until,
                agent_id=agent_id,
                action=action,
                limit=limit,
            )

        def _render(d: dict) -> None:
            from rich.table import Table

            txns = d.get("transactions", [])
            if not txns:
                console.print("[nexus.warning]No audit entries found[/nexus.warning]")
                return

            table = Table(title=f"Audit Trail ({len(txns)} entries)")
            table.add_column("Time", style="nexus.muted")
            table.add_column("Buyer")
            table.add_column("Seller")
            table.add_column("Amount", justify="right", style="nexus.success")
            table.add_column("Protocol")
            table.add_column("Status")

            for tx in txns:
                table.add_row(
                    tx.get("created_at", "")[:19],
                    tx.get("buyer_agent_id", ""),
                    tx.get("seller_agent_id", ""),
                    tx.get("amount", ""),
                    tx.get("protocol", ""),
                    tx.get("status", ""),
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


@audit.command("export")
@click.option(
    "--format",
    "fmt",
    type=click.Choice(["csv", "json"]),
    default="json",
    help="Export format",
    show_default=True,
)
@click.option("--output", "output_file", default=None, help="Output file path (default: stdout)")
@click.option("--since", default=None, help="Start time (ISO format)")
@click.option("--until", default=None, help="End time (ISO format)")
@REMOTE_API_KEY_OPTION
@REMOTE_URL_OPTION
def audit_export(
    fmt: str,
    output_file: str | None,
    since: str | None,
    until: str | None,
    remote_url: str | None,
    remote_api_key: str | None,
) -> None:
    """Export audit data to file.

    \b
    Examples:
        nexus audit export --format csv --output report.csv
        nexus audit export --format json --since 2024-01-01
        nexus audit export --format csv > transactions.csv
    """
    try:
        content = rpc_call(
            remote_url, remote_api_key, "audit_export", fmt=fmt, since=since, until=until
        )

        if output_file:
            with open(output_file, "w") as f:
                f.write(content)
            console.print(f"[nexus.success]Exported to {output_file}[/nexus.success]")
        else:
            click.echo(content)
    except Exception as e:
        console.print(f"[nexus.error]Error:[/nexus.error] {e}")
        raise SystemExit(1) from None
