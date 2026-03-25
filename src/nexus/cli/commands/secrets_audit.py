"""Secrets audit CLI commands — secret access event auditing.

Maps to secrets_audit_* RPC methods via rpc_call().
Issue #2812. Distinct from `nexus audit` (transaction audit in #2811).
"""

from __future__ import annotations

import click

from nexus.cli.output import OutputOptions, add_output_options, render_output
from nexus.cli.timing import CommandTiming
from nexus.cli.utils import REMOTE_API_KEY_OPTION, REMOTE_URL_OPTION, console, rpc_call


@click.group("secrets-audit")
def secrets_audit() -> None:
    """Secret access auditing.

    \b
    View and export tamper-proof secret access events.

    \b
    Examples:
        nexus secrets-audit list --since 1h --json
        nexus secrets-audit export --format csv
        nexus secrets-audit verify <record-id>
    """


@secrets_audit.command("list")
@click.option("--since", default=None, help="Start time (ISO format or relative, e.g., '1h')")
@click.option("--action", default=None, help="Filter by action type")
@click.option("--limit", default=50, show_default=True, help="Maximum entries")
@add_output_options
@REMOTE_API_KEY_OPTION
@REMOTE_URL_OPTION
def secrets_audit_list(
    since: str | None,
    action: str | None,
    limit: int,
    output_opts: OutputOptions,
    remote_url: str | None,
    remote_api_key: str | None,
) -> None:
    """List secret access events.

    \b
    Examples:
        nexus secrets-audit list
        nexus secrets-audit list --since 1h --action read --json
    """
    timing = CommandTiming()
    try:
        with timing.phase("server"):
            data = rpc_call(
                remote_url,
                remote_api_key,
                "secrets_audit_list",
                since=since,
                action=action,
                limit=limit,
            )

        def _render(d: dict) -> None:
            from rich.table import Table

            events = d.get("events", [])
            if not events:
                console.print("[yellow]No secret access events[/yellow]")
                return

            table = Table(title=f"Secret Access Events ({len(events)})")
            table.add_column("ID", style="dim")
            table.add_column("Action")
            table.add_column("Secret")
            table.add_column("Agent")
            table.add_column("Time", style="dim")

            for ev in events:
                table.add_row(
                    ev.get("record_id", "")[:12],
                    ev.get("action", ""),
                    ev.get("secret_name", ev.get("secret_id", "")),
                    ev.get("agent_id", ""),
                    ev.get("timestamp", "")[:19],
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


@secrets_audit.command("export")
@click.option(
    "--format",
    "fmt",
    type=click.Choice(["csv", "json"]),
    default="json",
    show_default=True,
    help="Export format",
)
@click.option("--output", "output_file", default=None, help="Output file path (default: stdout)")
@click.option("--since", default=None, help="Start time (ISO format)")
@REMOTE_API_KEY_OPTION
@REMOTE_URL_OPTION
def secrets_audit_export(
    fmt: str,
    output_file: str | None,
    since: str | None,
    remote_url: str | None,
    remote_api_key: str | None,
) -> None:
    """Export secret access audit log.

    \b
    Examples:
        nexus secrets-audit export --format csv > audit.csv
        nexus secrets-audit export --format json --output audit.json
    """
    try:
        data = rpc_call(
            remote_url,
            remote_api_key,
            "secrets_audit_export",
            fmt=fmt,
            since=since,
        )
        # data may be a string (raw export) or dict
        content = data if isinstance(data, str) else str(data)

        if output_file:
            with open(output_file, "w") as f:
                f.write(content)
            console.print(f"[green]Exported to {output_file}[/green]")
        else:
            click.echo(content)
    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        raise SystemExit(1) from None


@secrets_audit.command("verify")
@click.argument("record_id")
@add_output_options
@REMOTE_API_KEY_OPTION
@REMOTE_URL_OPTION
def secrets_audit_verify(
    record_id: str,
    output_opts: OutputOptions,
    remote_url: str | None,
    remote_api_key: str | None,
) -> None:
    """Verify integrity of an audit record.

    \b
    Examples:
        nexus secrets-audit verify rec_123
        nexus secrets-audit verify rec_123 --json
    """
    timing = CommandTiming()
    try:
        with timing.phase("server"):
            data = rpc_call(
                remote_url, remote_api_key, "secrets_audit_integrity", record_id=record_id
            )

        def _render(d: dict) -> None:
            valid = d.get("valid", d.get("integrity_valid", False))
            status = "[green]Valid[/green]" if valid else "[red]Tampered[/red]"
            console.print(f"[bold cyan]Integrity Check: {record_id}[/bold cyan]")
            console.print(f"  Status: {status}")
            if d.get("hash"):
                console.print(f"  Hash:   {d['hash']}")

        render_output(
            data=data,
            output_opts=output_opts,
            timing=timing,
            human_formatter=_render,
        )
    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        raise SystemExit(1) from None
