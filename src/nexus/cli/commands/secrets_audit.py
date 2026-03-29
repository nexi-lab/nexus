"""Secrets audit CLI commands — secret access event auditing.

Maps to /api/v2/secrets-audit/* endpoints via SecretsAuditClient.
Issue #2812. Distinct from `nexus audit` (transaction audit in #2811).
"""

from __future__ import annotations

import click

from nexus.cli.clients.secrets_audit import SecretsAuditClient
from nexus.cli.output import add_output_options
from nexus.cli.service_command import ServiceResult, service_command
from nexus.cli.utils import REMOTE_API_KEY_OPTION, REMOTE_URL_OPTION


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
@service_command(client_class=SecretsAuditClient)
def secrets_audit_list(
    client: SecretsAuditClient,
    since: str | None,
    action: str | None,
    limit: int,
) -> ServiceResult:
    """List secret access events.

    \b
    Examples:
        nexus secrets-audit list
        nexus secrets-audit list --since 1h --action read --json
    """
    data = client.list(since=since, action=action, limit=limit)

    def _render(d: dict) -> None:
        from rich.table import Table

        from nexus.cli.theme import console

        events = d.get("events", [])
        if not events:
            console.print("[nexus.warning]No secret access events[/nexus.warning]")
            return

        table = Table(title=f"Secret Access Events ({len(events)})")
        table.add_column("ID", style="nexus.muted")
        table.add_column("Action")
        table.add_column("Secret")
        table.add_column("Agent")
        table.add_column("Time", style="nexus.muted")

        for ev in events:
            table.add_row(
                ev.get("record_id", "")[:12],
                ev.get("action", ""),
                ev.get("secret_name", ev.get("secret_id", "")),
                ev.get("agent_id", ""),
                ev.get("timestamp", "")[:19],
            )
        console.print(table)

    return ServiceResult(data=data, human_formatter=_render)


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
    from nexus.cli.service_command import _validate_url
    from nexus.cli.theme import console

    url = _validate_url(remote_url)
    try:
        client = SecretsAuditClient(url=url, api_key=remote_api_key)
        with client:
            content = client.export(fmt=fmt, since=since)

        if output_file:
            with open(output_file, "w") as f:
                f.write(content)
            console.print(f"[nexus.success]Exported to {output_file}[/nexus.success]")
        else:
            click.echo(content)
    except Exception as e:
        console.print(f"[nexus.error]Error:[/nexus.error] {e}")
        raise SystemExit(1) from None


@secrets_audit.command("verify")
@click.argument("record_id")
@add_output_options
@REMOTE_API_KEY_OPTION
@REMOTE_URL_OPTION
@service_command(client_class=SecretsAuditClient)
def secrets_audit_verify(client: SecretsAuditClient, record_id: str) -> ServiceResult:
    """Verify integrity of an audit record.

    \b
    Examples:
        nexus secrets-audit verify rec_123
        nexus secrets-audit verify rec_123 --json
    """
    data = client.verify(record_id)

    def _render(d: dict) -> None:
        from nexus.cli.theme import console

        valid = d.get("valid", d.get("integrity_valid", False))
        status = (
            "[nexus.success]Valid[/nexus.success]"
            if valid
            else "[nexus.error]Tampered[/nexus.error]"
        )
        console.print(f"[bold nexus.value]Integrity Check: {record_id}[/bold nexus.value]")
        console.print(f"  Status: {status}")
        if d.get("hash"):
            console.print(f"  Hash:   {d['hash']}")

    return ServiceResult(data=data, human_formatter=_render)
