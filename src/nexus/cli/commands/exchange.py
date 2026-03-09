"""Exchange CLI commands — agent marketplace offers (Phase 2).

Issue #2811. Exchange offer endpoints are not yet implemented on the
server side; these commands will work once the backend is available.
"""

import click

from nexus.cli.utils import (
    JSON_OUTPUT_OPTION,
    REMOTE_API_KEY_OPTION,
    REMOTE_URL_OPTION,
    console,
)


@click.group()
def exchange() -> None:
    """Agent exchange marketplace (Phase 2).

    \b
    Note: Exchange offer endpoints are under development.
    Commands will become functional when the backend is available.

    \b
    Examples:
        nexus exchange list
        nexus exchange create /data/dataset.csv --price 100
        nexus exchange show <offer-id>
        nexus exchange cancel <offer-id>
    """


@exchange.command("list")
@click.option("--status", type=click.Choice(["active", "completed"]), default=None)
@JSON_OUTPUT_OPTION
@REMOTE_API_KEY_OPTION
@REMOTE_URL_OPTION
def exchange_list(
    **_kwargs: object,  # noqa: ARG001 — Phase 2 stub
) -> None:
    """List exchange offers.

    \b
    Examples:
        nexus exchange list
        nexus exchange list --status active --json
    """
    console.print(
        "[yellow]Exchange offer listing is not yet available.[/yellow]\n"
        "This feature is planned for Phase 2. See issue #2811."
    )


@exchange.command("create")
@click.argument("resource")
@click.option("--price", required=True, help="Asking price in credits")
@click.option("--description", default="", help="Offer description")
@JSON_OUTPUT_OPTION
@REMOTE_API_KEY_OPTION
@REMOTE_URL_OPTION
def exchange_create(
    **_kwargs: object,  # noqa: ARG001 — Phase 2 stub
) -> None:
    """Create an exchange offer.

    \b
    Examples:
        nexus exchange create /data/dataset.csv --price 100
        nexus exchange create /models/v2.bin --price 500 --description "Trained model"
    """
    console.print(
        "[yellow]Exchange offer creation is not yet available.[/yellow]\n"
        "This feature is planned for Phase 2. See issue #2811."
    )


@exchange.command("show")
@click.argument("offer_id")
@JSON_OUTPUT_OPTION
@REMOTE_API_KEY_OPTION
@REMOTE_URL_OPTION
def exchange_show(
    **_kwargs: object,  # noqa: ARG001 — Phase 2 stub
) -> None:
    """Show exchange offer details.

    \b
    Examples:
        nexus exchange show abc123 --json
    """
    console.print(
        "[yellow]Exchange offer details are not yet available.[/yellow]\n"
        "This feature is planned for Phase 2. See issue #2811."
    )


@exchange.command("cancel")
@click.argument("offer_id")
@REMOTE_API_KEY_OPTION
@REMOTE_URL_OPTION
def exchange_cancel(
    **_kwargs: object,  # noqa: ARG001 — Phase 2 stub
) -> None:
    """Cancel an exchange offer.

    \b
    Examples:
        nexus exchange cancel abc123
    """
    console.print(
        "[yellow]Exchange offer cancellation is not yet available.[/yellow]\n"
        "This feature is planned for Phase 2. See issue #2811."
    )
