"""Events CLI commands — replay and subscribe.

Maps to /api/v2/events/* REST endpoints via NexusServiceClient.
Issue #2811.
"""

import click

from nexus.cli.output import OutputOptions, add_output_options, render_output
from nexus.cli.timing import CommandTiming
from nexus.cli.utils import (
    REMOTE_API_KEY_OPTION,
    REMOTE_URL_OPTION,
    console,
    get_service_client,
)


@click.group()
def events() -> None:
    """Event replay and subscription.

    \b
    Prerequisites:
        - Running Nexus server
        - Server URL (set via NEXUS_URL or --remote-url)
        - API key (set via NEXUS_API_KEY or --remote-api-key)

    \b
    Examples:
        nexus events replay --since 1h
        nexus events subscribe "file_write"
    """


@events.command("replay")
@click.option("--since", default=None, help="Start time (ISO format or relative)")
@click.option("--type", "event_type", default=None, help="Filter by event type")
@click.option("--path", "event_path", default=None, help="Filter by file path")
@click.option("--limit", default=50, help="Maximum events", show_default=True)
@add_output_options
@REMOTE_API_KEY_OPTION
@REMOTE_URL_OPTION
def events_replay(
    since: str | None,
    event_type: str | None,
    event_path: str | None,
    limit: int,
    output_opts: OutputOptions,
    remote_url: str | None,
    remote_api_key: str | None,
) -> None:
    """Replay historical events.

    \b
    Examples:
        nexus events replay --since 1h
        nexus events replay --type file_write --path /data/
        nexus events replay --since 2024-01-01 --json
    """
    try:
        timing = CommandTiming()
        with timing.phase("server"), get_service_client(remote_url, remote_api_key) as client:
            data = client.events_replay(
                since=since,
                event_type=event_type,
                path=event_path,
                limit=limit,
            )

        def _render(d: dict) -> None:
            from rich.table import Table

            evts = d.get("events", [])
            if not evts:
                console.print("[yellow]No events found[/yellow]")
                return

            table = Table(title=f"Events ({len(evts)})")
            table.add_column("Seq", style="dim", justify="right")
            table.add_column("Time", style="dim")
            table.add_column("Type")
            table.add_column("Path")
            table.add_column("Agent")

            for ev in evts:
                table.add_row(
                    str(ev.get("seq_number", "")),
                    ev.get("timestamp", "")[:19],
                    ev.get("event_type", ""),
                    ev.get("path", ""),
                    ev.get("agent_id", ""),
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


@events.command("subscribe")
@click.argument("pattern")
@add_output_options
@REMOTE_API_KEY_OPTION
@REMOTE_URL_OPTION
def events_subscribe(
    pattern: str,
    output_opts: OutputOptions,
    remote_url: str | None,
    remote_api_key: str | None,
) -> None:
    """Subscribe to real-time events (SSE).

    Streams events matching the given pattern until interrupted (Ctrl+C).

    \b
    Examples:
        nexus events subscribe "file_write"
        nexus events subscribe "*" --json
    """
    try:
        timing = CommandTiming()
        with timing.phase("server"), get_service_client(remote_url, remote_api_key) as client:
            console.print(f"[yellow]Subscribing to events matching:[/yellow] {pattern}")
            console.print("[dim]Press Ctrl+C to stop[/dim]\n")
            content = client.events_subscribe(pattern)

        def _render(d: str) -> None:
            for line in d.splitlines():
                if line.startswith("data:"):
                    console.print(f"  {line[5:].strip()}")
                elif line.strip():
                    console.print(f"  [dim]{line}[/dim]")

        render_output(
            data=content,
            output_opts=output_opts,
            timing=timing,
            human_formatter=_render,
        )
    except KeyboardInterrupt:
        console.print("\n[yellow]Subscription ended[/yellow]")
    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        raise SystemExit(1) from None
