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
        nexus reputation feedback <exchange-id> --rater alice --rated bob --outcome positive
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

        def _beta_score(prefix: str) -> str:
            """Compute score from Bayesian alpha/beta: alpha / (alpha + beta)."""
            alpha = d.get(f"{prefix}_alpha")
            beta = d.get(f"{prefix}_beta")
            if alpha is not None and beta is not None and (alpha + beta) > 0:
                return f"{alpha / (alpha + beta):.2f}"
            return "N/A"

        score = d.get("composite_score", d.get("score", "N/A"))
        confidence = d.get("composite_confidence", "N/A")
        console.print(f"[bold cyan]Reputation: {agent_id}[/bold cyan]")
        console.print(f"  Composite Score:      [bold]{score}[/bold]")
        console.print(f"  Composite Confidence: {confidence}")
        console.print(f"  Reliability:          {_beta_score('reliability')}")
        console.print(f"  Quality:              {_beta_score('quality')}")
        console.print(f"  Timeliness:           {_beta_score('timeliness')}")
        console.print(f"  Fairness:             {_beta_score('fairness')}")
        console.print(f"  Total Interactions:   {d.get('total_interactions', 0)}")

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
        table.add_column("Interactions", justify="right")

        for i, entry in enumerate(entries, 1):
            table.add_row(
                str(i),
                entry.get("agent_id", ""),
                str(entry.get("composite_score", entry.get("score", ""))),
                str(entry.get("total_interactions", "")),
            )
        console.print(table)

    return ServiceResult(data=data, human_formatter=_render)


@reputation.command("feedback")
@click.argument("exchange_id")
@click.option("--rater", "rater_agent_id", required=True, help="Rater agent ID")
@click.option("--rated", "rated_agent_id", required=True, help="Rated agent ID")
@click.option(
    "--outcome",
    required=True,
    type=click.Choice(["positive", "negative", "neutral", "mixed"]),
    help="Outcome of the exchange",
)
@click.option("--reliability", type=float, default=None, help="Reliability score (0.0-1.0)")
@click.option("--quality", type=float, default=None, help="Quality score (0.0-1.0)")
@add_output_options
@REMOTE_API_KEY_OPTION
@REMOTE_URL_OPTION
@service_command(client_class=ReputationClient)
def reputation_feedback(
    client: ReputationClient,
    exchange_id: str,
    rater_agent_id: str,
    rated_agent_id: str,
    outcome: str,
    reliability: float | None,
    quality: float | None,
) -> ServiceResult:
    """Submit feedback for an exchange.

    \b
    Examples:
        nexus reputation feedback exch_123 --rater alice --rated bob --outcome positive
        nexus reputation feedback exch_123 --rater alice --rated bob --outcome negative --reliability 0.3
    """
    data = client.submit_feedback(
        exchange_id,
        rater_agent_id=rater_agent_id,
        rated_agent_id=rated_agent_id,
        outcome=outcome,
        reliability_score=reliability,
        quality_score=quality,
    )
    return ServiceResult(data=data, message=f"Feedback submitted for exchange {exchange_id}")


@reputation.group("dispute")
def dispute() -> None:
    """Manage reputation disputes."""


@dispute.command("create")
@click.argument("exchange_id")
@click.option("--complainant", required=True, help="Complainant agent ID")
@click.option("--respondent", required=True, help="Respondent agent ID")
@click.option("--reason", required=True, help="Reason for the dispute")
@add_output_options
@REMOTE_API_KEY_OPTION
@REMOTE_URL_OPTION
@service_command(client_class=ReputationClient)
def dispute_create(
    client: ReputationClient,
    exchange_id: str,
    complainant: str,
    respondent: str,
    reason: str,
) -> ServiceResult:
    """Open a dispute for an exchange.

    \b
    Examples:
        nexus reputation dispute create exch_123 --complainant alice --respondent bob --reason "Service not delivered"
    """
    data = client.dispute_create(
        exchange_id,
        complainant_agent_id=complainant,
        respondent_agent_id=respondent,
        reason=reason,
    )

    def _render(d: dict) -> None:
        from nexus.cli.utils import console

        console.print("[green]Dispute filed[/green]")
        console.print(f"  Dispute ID: {d.get('id', d.get('dispute_id', 'N/A'))}")
        console.print(f"  Status:     {d.get('status', 'filed')}")

    return ServiceResult(data=data, human_formatter=_render)


@dispute.command("show")
@click.argument("dispute_id")
@add_output_options
@REMOTE_API_KEY_OPTION
@REMOTE_URL_OPTION
@service_command(client_class=ReputationClient)
def dispute_show(client: ReputationClient, dispute_id: str) -> ServiceResult:
    """Show dispute details.

    \b
    Examples:
        nexus reputation dispute show disp_123 --json
    """
    data = client.dispute_get(dispute_id)

    def _render(d: dict) -> None:
        from nexus.cli.utils import console

        console.print(f"[bold cyan]Dispute: {dispute_id}[/bold cyan]")
        console.print(f"  Status:      {d.get('status', 'N/A')}")
        console.print(f"  Complainant: {d.get('complainant_agent_id', 'N/A')}")
        console.print(f"  Respondent:  {d.get('respondent_agent_id', 'N/A')}")
        console.print(f"  Reason:      {d.get('reason', 'N/A')}")

    return ServiceResult(data=data, human_formatter=_render)
