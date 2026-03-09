"""Governance CLI commands — alerts, fraud rings, and status.

Maps to /api/v2/governance/* REST endpoints via NexusServiceClient.
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
def governance() -> None:
    """Governance and anti-fraud operations (admin).

    \b
    Prerequisites:
        - Running Nexus server with admin privileges
        - Server URL (set via NEXUS_URL or --remote-url)
        - Admin API key (set via NEXUS_API_KEY or --remote-api-key)

    \b
    Examples:
        nexus governance status
        nexus governance alerts --severity high --json
        nexus governance rings --json
    """


@governance.command("status")
@JSON_OUTPUT_OPTION
@REMOTE_API_KEY_OPTION
@REMOTE_URL_OPTION
def governance_status(
    json_output: bool,
    remote_url: str | None,
    remote_api_key: str | None,
) -> None:
    """Show governance overview.

    Displays recent alerts and detected fraud rings.

    \b
    Examples:
        nexus governance status
        nexus governance status --json
    """
    try:
        with get_service_client(remote_url, remote_api_key) as client:
            data = client.governance_status()

        def _render(d: dict) -> None:
            alerts = d.get("recent_alerts", {})
            rings = d.get("fraud_rings", {})

            alert_list = alerts.get("alerts", []) if isinstance(alerts, dict) else []
            ring_list = rings.get("rings", []) if isinstance(rings, dict) else []

            console.print("[bold cyan]Governance Status[/bold cyan]")
            console.print(f"  Active alerts: [yellow]{len(alert_list)}[/yellow]")
            console.print(f"  Fraud rings:   [yellow]{len(ring_list)}[/yellow]")

            if alert_list:
                console.print("\n[bold]Recent Alerts:[/bold]")
                for alert in alert_list[:5]:
                    sev = alert.get("severity", "unknown")
                    color = "red" if sev == "high" else "yellow"
                    console.print(f"  [{color}]{sev}[/{color}] {alert.get('description', 'N/A')}")

        output_result(data, json_output, _render)
    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        raise SystemExit(1) from None


@governance.command("alerts")
@click.option("--severity", default=None, help="Filter by severity (low/medium/high)")
@click.option("--since", default=None, help="Start time (ISO format)")
@click.option("--limit", default=50, help="Maximum entries", show_default=True)
@JSON_OUTPUT_OPTION
@REMOTE_API_KEY_OPTION
@REMOTE_URL_OPTION
def governance_alerts(
    severity: str | None,
    since: str | None,
    limit: int,
    json_output: bool,
    remote_url: str | None,
    remote_api_key: str | None,
) -> None:
    """List anomaly alerts.

    \b
    Examples:
        nexus governance alerts
        nexus governance alerts --severity high --since 2024-01-01
        nexus governance alerts --json
    """
    try:
        with get_service_client(remote_url, remote_api_key) as client:
            data = client.governance_alerts(severity=severity, since=since, limit=limit)

        def _render(d: dict) -> None:
            from rich.table import Table

            alerts = d.get("alerts", [])
            if not alerts:
                console.print("[green]No anomaly alerts[/green]")
                return

            table = Table(title=f"Anomaly Alerts ({len(alerts)})")
            table.add_column("Time", style="dim")
            table.add_column("Severity")
            table.add_column("Agent")
            table.add_column("Description")

            for alert in alerts:
                sev = alert.get("severity", "")
                sev_style = "red" if sev == "high" else "yellow" if sev == "medium" else "dim"
                table.add_row(
                    alert.get("created_at", "")[:19],
                    f"[{sev_style}]{sev}[/{sev_style}]",
                    alert.get("agent_id", ""),
                    alert.get("description", ""),
                )
            console.print(table)

        output_result(data, json_output, _render)
    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        raise SystemExit(1) from None


@governance.command("rings")
@JSON_OUTPUT_OPTION
@REMOTE_API_KEY_OPTION
@REMOTE_URL_OPTION
def governance_rings(
    json_output: bool,
    remote_url: str | None,
    remote_api_key: str | None,
) -> None:
    """List detected fraud rings.

    \b
    Examples:
        nexus governance rings
        nexus governance rings --json
    """
    try:
        with get_service_client(remote_url, remote_api_key) as client:
            data = client.governance_rings()

        def _render(d: dict) -> None:
            rings = d.get("rings", [])
            if not rings:
                console.print("[green]No fraud rings detected[/green]")
                return

            console.print(f"[bold cyan]Detected Fraud Rings ({len(rings)})[/bold cyan]")
            for i, ring in enumerate(rings, 1):
                members = ring.get("members", [])
                score = ring.get("risk_score", 0)
                console.print(f"\n  [bold]Ring {i}[/bold] (risk: [red]{score:.2f}[/red])")
                console.print(f"  Members: {', '.join(members)}")

        output_result(data, json_output, _render)
    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        raise SystemExit(1) from None
