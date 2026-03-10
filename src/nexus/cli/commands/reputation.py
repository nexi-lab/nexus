"""Reputation CLI commands — agent reputation scores and disputes.

Maps to /api/v2/agents/*/reputation and /api/v2/disputes/* endpoints.
Issue #2812.
"""

from __future__ import annotations

import click

from nexus.cli.clients.reputation import ReputationClient
from nexus.cli.output import add_output_options
from nexus.cli.service_command import ServiceResult, service_command
from nexus.cli.utils import REMOTE_API_KEY_OPTION, REMOTE_URL_OPTION


@click.group()
def reputation() -> None:
    """Agent reputation and dispute management.

    \b
    View reputation scores, submit feedback, and manage disputes.

    \b
    Examples:
        nexus reputation show agent_alice --json
        nexus reputation leaderboard --limit 10
        nexus reputation feedback <exchange-id> --outcome positive
    """


@reputation.command("show")
@click.argument("agent_id")
@click.option("--context", default="general", show_default=True, help="Reputation context")
@click.option(
    "--window", default="all_time", show_default=True, help="Time window (all_time, 30d, 7d)"
)
@add_output_options
@REMOTE_API_KEY_OPTION
@REMOTE_URL_OPTION
@service_command(client_class=ReputationClient)
def reputation_show(
    client: ReputationClient,
    agent_id: str,
    context: str,
    window: str,
) -> ServiceResult:
    """Show agent's composite reputation score.

    \b
    Examples:
        nexus reputation show agent_alice
        nexus reputation show agent_alice --window 30d --json
    """
    data = client.show(agent_id, context=context, window=window)

    def _render(d: dict) -> None:
        from nexus.cli.utils import console

        score = d.get("composite_score", d.get("score", "N/A"))
        console.print(f"[bold cyan]Reputation: {agent_id}[/bold cyan]")
        console.print(f"  Composite Score: [bold]{score}[/bold]")
        console.print(f"  Reliability:     {d.get('reliability_score', 'N/A')}")
        console.print(f"  Quality:         {d.get('quality_score', 'N/A')}")
        console.print(f"  Timeliness:      {d.get('timeliness_score', 'N/A')}")
        console.print(f"  Fairness:        {d.get('fairness_score', 'N/A')}")
        console.print(f"  Total Ratings:   {d.get('total_ratings', 0)}")

    return ServiceResult(data=data, human_formatter=_render)


@reputation.command("leaderboard")
@click.option("--zone-id", default=None, help="Filter by zone ID")
@click.option("--limit", default=20, show_default=True, help="Number of entries")
@add_output_options
@REMOTE_API_KEY_OPTION
@REMOTE_URL_OPTION
@service_command(client_class=ReputationClient)
def reputation_leaderboard(
    client: ReputationClient,
    zone_id: str | None,
    limit: int,
) -> ServiceResult:
    """Show top agents by reputation score.

    \b
    Examples:
        nexus reputation leaderboard
        nexus reputation leaderboard --zone-id org_acme --limit 10 --json
    """
    data = client.leaderboard(zone_id=zone_id, limit=limit)

    def _render(d: dict) -> None:
        from rich.table import Table

        from nexus.cli.utils import console

        entries = d.get("leaderboard", d.get("entries", []))
        if not entries:
            console.print("[yellow]No reputation data[/yellow]")
            return

        table = Table(title=f"Reputation Leaderboard (top {len(entries)})")
        table.add_column("#", justify="right", style="dim")
        table.add_column("Agent")
        table.add_column("Score", justify="right", style="green")
        table.add_column("Ratings", justify="right")

        for i, entry in enumerate(entries, 1):
            table.add_row(
                str(i),
                entry.get("agent_id", ""),
                str(entry.get("composite_score", entry.get("score", ""))),
                str(entry.get("total_ratings", "")),
            )
        console.print(table)

    return ServiceResult(data=data, human_formatter=_render)


@reputation.command("feedback")
@click.argument("exchange_id")
@click.option(
    "--outcome",
    required=True,
    type=click.Choice(["positive", "negative", "neutral"]),
    help="Outcome of the exchange",
)
@click.option("--reliability", type=float, default=None, help="Reliability score (0.0-1.0)")
@click.option("--quality", type=float, default=None, help="Quality score (0.0-1.0)")
@click.option("--memo", default=None, help="Feedback memo/description")
@add_output_options
@REMOTE_API_KEY_OPTION
@REMOTE_URL_OPTION
@service_command(client_class=ReputationClient)
def reputation_feedback(
    client: ReputationClient,
    exchange_id: str,
    outcome: str,
    reliability: float | None,
    quality: float | None,
    memo: str | None,
) -> ServiceResult:
    """Submit feedback for an exchange.

    \b
    Examples:
        nexus reputation feedback exch_123 --outcome positive
        nexus reputation feedback exch_123 --outcome negative --reliability 0.3 --memo "Late delivery"
    """
    data = client.submit_feedback(
        exchange_id,
        outcome=outcome,
        reliability_score=reliability,
        quality_score=quality,
        memo=memo,
    )
    return ServiceResult(data=data, message=f"Feedback submitted for exchange {exchange_id}")


@reputation.group("dispute")
def dispute() -> None:
    """Manage reputation disputes."""


@dispute.command("create")
@click.argument("exchange_id")
@click.option("--reason", required=True, help="Reason for the dispute")
@add_output_options
@REMOTE_API_KEY_OPTION
@REMOTE_URL_OPTION
@service_command(client_class=ReputationClient)
def dispute_create(client: ReputationClient, exchange_id: str, reason: str) -> ServiceResult:
    """Open a dispute for an exchange.

    \b
    Examples:
        nexus reputation dispute create exch_123 --reason "Service not delivered"
    """
    data = client.dispute_create(exchange_id, reason=reason)

    def _render(d: dict) -> None:
        from nexus.cli.utils import console

        console.print("[green]Dispute filed[/green]")
        console.print(f"  Dispute ID: {d.get('dispute_id', 'N/A')}")
        console.print(f"  Status:     {d.get('status', 'filed')}")

    return ServiceResult(data=data, human_formatter=_render)


@dispute.command("list")
@click.option("--agent-id", default=None, help="Filter by agent ID")
@click.option("--status", "dispute_status", default=None, help="Filter by status")
@add_output_options
@REMOTE_API_KEY_OPTION
@REMOTE_URL_OPTION
@service_command(client_class=ReputationClient)
def dispute_list_cmd(
    client: ReputationClient,
    agent_id: str | None,
    dispute_status: str | None,
) -> ServiceResult:
    """List disputes.

    \b
    Examples:
        nexus reputation dispute list
        nexus reputation dispute list --status filed --json
    """
    # Use the leaderboard endpoint filtered — or more accurately, list disputes
    # The reputation client doesn't have a direct list-disputes method via the
    # exchange endpoint, so we use a general query approach
    data = client._request(
        "GET",
        "/api/v2/disputes",
        params={"agent_id": agent_id, "status": dispute_status},
    )

    def _render(d: dict) -> None:
        from rich.table import Table

        from nexus.cli.utils import console

        disputes = d.get("disputes", [])
        if not disputes:
            console.print("[yellow]No disputes found[/yellow]")
            return

        table = Table(title=f"Disputes ({len(disputes)})")
        table.add_column("ID", style="dim")
        table.add_column("Exchange")
        table.add_column("Status")
        table.add_column("Reason")
        table.add_column("Filed", style="dim")

        for disp in disputes:
            table.add_row(
                disp.get("dispute_id", "")[:12],
                disp.get("exchange_id", ""),
                disp.get("status", ""),
                disp.get("reason", "")[:40],
                disp.get("created_at", "")[:19],
            )
        console.print(table)

    return ServiceResult(data=data, human_formatter=_render)
