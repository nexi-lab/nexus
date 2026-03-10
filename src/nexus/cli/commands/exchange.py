"""Exchange CLI commands — agent marketplace offers (Phase 2).

Issue #2811. Exchange offer endpoints are not yet implemented on the
server side; these commands will work once the backend is available.
"""

import click

from nexus.cli.output import OutputOptions, add_output_options, render_output
from nexus.cli.timing import CommandTiming
from nexus.cli.utils import (
    REMOTE_API_KEY_OPTION,
    REMOTE_URL_OPTION,
    console,
)

_STUB_STATUS = "not_implemented"
_STUB_ISSUE = "#2811"


def _stub_data(command: str) -> dict[str, str]:
    """Return structured stub data for a Phase 2 command."""
    return {"status": _STUB_STATUS, "command": command, "issue": _STUB_ISSUE}


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
@add_output_options
@REMOTE_API_KEY_OPTION
@REMOTE_URL_OPTION
def exchange_list(
    output_opts: OutputOptions,
    **_kwargs: object,  # noqa: ARG001 — Phase 2 stub
) -> None:
    """List exchange offers.

    \b
    Examples:
        nexus exchange list
        nexus exchange list --status active --json
    """
    timing = CommandTiming()
    render_output(
        data=_stub_data("exchange list"),
        output_opts=output_opts,
        timing=timing,
        human_formatter=lambda _d: console.print(
            "[yellow]Exchange offer listing is not yet available.[/yellow]\n"
            "This feature is planned for Phase 2. See issue #2811."
        ),
    )


@exchange.command("create")
@click.argument("resource")
@click.option("--price", required=True, help="Asking price in credits")
@click.option("--description", default="", help="Offer description")
@add_output_options
@REMOTE_API_KEY_OPTION
@REMOTE_URL_OPTION
def exchange_create(
    output_opts: OutputOptions,
    **_kwargs: object,  # noqa: ARG001 — Phase 2 stub
) -> None:
    """Create an exchange offer.

    \b
    Examples:
        nexus exchange create /data/dataset.csv --price 100
        nexus exchange create /models/v2.bin --price 500 --description "Trained model"
    """
    timing = CommandTiming()
    render_output(
        data=_stub_data("exchange create"),
        output_opts=output_opts,
        timing=timing,
        human_formatter=lambda _d: console.print(
            "[yellow]Exchange offer creation is not yet available.[/yellow]\n"
            "This feature is planned for Phase 2. See issue #2811."
        ),
    )


@exchange.command("show")
@click.argument("offer_id")
@add_output_options
@REMOTE_API_KEY_OPTION
@REMOTE_URL_OPTION
def exchange_show(
    output_opts: OutputOptions,
    **_kwargs: object,  # noqa: ARG001 — Phase 2 stub
) -> None:
    """Show exchange offer details.

    \b
    Examples:
        nexus exchange show abc123 --json
    """
    timing = CommandTiming()
    render_output(
        data=_stub_data("exchange show"),
        output_opts=output_opts,
        timing=timing,
        human_formatter=lambda _d: console.print(
            "[yellow]Exchange offer details are not yet available.[/yellow]\n"
            "This feature is planned for Phase 2. See issue #2811."
        ),
    )


@exchange.command("cancel")
@click.argument("offer_id")
@add_output_options
@REMOTE_API_KEY_OPTION
@REMOTE_URL_OPTION
def exchange_cancel(
    output_opts: OutputOptions,
    **_kwargs: object,  # noqa: ARG001 — Phase 2 stub
) -> None:
    """Cancel an exchange offer.

    \b
    Examples:
        nexus exchange cancel abc123
    """
    timing = CommandTiming()
    render_output(
        data=_stub_data("exchange cancel"),
        output_opts=output_opts,
        timing=timing,
        human_formatter=lambda _d: console.print(
            "[yellow]Exchange offer cancellation is not yet available.[/yellow]\n"
            "This feature is planned for Phase 2. See issue #2811."
        ),
    )
