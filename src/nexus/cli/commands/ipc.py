"""IPC CLI commands — agent-to-agent messaging.

Maps to ipc_* RPC methods via rpc_call().
Issue #2812.
"""

from __future__ import annotations

import click

from nexus.cli.output import OutputOptions, add_output_options, render_output
from nexus.cli.timing import CommandTiming
from nexus.cli.utils import REMOTE_API_KEY_OPTION, REMOTE_URL_OPTION, console, rpc_call


@click.group()
def ipc() -> None:
    """Agent-to-agent messaging.

    \b
    Send and receive messages between agents via their inboxes.

    \b
    Examples:
        nexus ipc send --from alice bob "Hello from Alice"
        nexus ipc inbox alice --json
        nexus ipc count alice
    """


@ipc.command("send")
@click.argument("recipient")
@click.argument("message")
@click.option("--from", "sender", required=True, help="Sender agent ID")
@click.option(
    "--type",
    "message_type",
    default="task",
    show_default=True,
    help="Message type (task, response, event, cancel)",
)
@add_output_options
@REMOTE_API_KEY_OPTION
@REMOTE_URL_OPTION
def ipc_send(
    recipient: str,
    message: str,
    sender: str,
    message_type: str,
    output_opts: OutputOptions,
    remote_url: str | None,
    remote_api_key: str | None,
) -> None:
    """Send message to an agent's inbox.

    \b
    Examples:
        nexus ipc send bob "Hello" --from alice
        nexus ipc send bob "cancel task 42" --from alice --type cancel
    """
    timing = CommandTiming()
    try:
        with timing.phase("server"):
            data = rpc_call(
                remote_url,
                remote_api_key,
                "ipc_send",
                sender=sender,
                recipient=recipient,
                message=message,
                message_type=message_type,
            )

        def _render(d: dict) -> None:
            console.print("[green]Message sent[/green]")
            console.print(f"  Message ID: {d.get('message_id', 'N/A')}")
            console.print(f"  From:       {sender}")
            console.print(f"  To:         {recipient}")

        render_output(
            data=data,
            output_opts=output_opts,
            timing=timing,
            human_formatter=_render,
        )
    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        raise SystemExit(1) from None


@ipc.command("inbox")
@click.argument("agent_id")
@add_output_options
@REMOTE_API_KEY_OPTION
@REMOTE_URL_OPTION
def ipc_inbox(
    agent_id: str,
    output_opts: OutputOptions,
    remote_url: str | None,
    remote_api_key: str | None,
) -> None:
    """List messages in agent's inbox.

    \b
    Examples:
        nexus ipc inbox alice
        nexus ipc inbox alice --json
    """
    timing = CommandTiming()
    try:
        with timing.phase("server"):
            data = rpc_call(remote_url, remote_api_key, "ipc_inbox", agent_id=agent_id)

        def _render(d: dict) -> None:
            from rich.table import Table

            messages = d.get("messages", [])
            if not messages:
                console.print("[yellow]Inbox empty[/yellow]")
                return

            table = Table(title=f"Inbox for {agent_id} ({len(messages)} messages)")
            table.add_column("File", style="dim")

            for msg in messages:
                table.add_row(msg.get("filename", str(msg)))
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


@ipc.command("count")
@click.argument("agent_id")
@add_output_options
@REMOTE_API_KEY_OPTION
@REMOTE_URL_OPTION
def ipc_count(
    agent_id: str,
    output_opts: OutputOptions,
    remote_url: str | None,
    remote_api_key: str | None,
) -> None:
    """Count messages in agent's inbox.

    \b
    Examples:
        nexus ipc count alice
        nexus ipc count alice --json
    """
    timing = CommandTiming()
    try:
        with timing.phase("server"):
            data = rpc_call(remote_url, remote_api_key, "ipc_inbox_count", agent_id=agent_id)

        def _render(d: dict) -> None:
            count = d.get("count", 0)
            console.print(f"[bold cyan]{agent_id}[/bold cyan]: {count} message(s) in inbox")

        render_output(
            data=data,
            output_opts=output_opts,
            timing=timing,
            human_formatter=_render,
        )
    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        raise SystemExit(1) from None
