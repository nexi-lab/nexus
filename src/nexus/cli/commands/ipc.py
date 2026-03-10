"""IPC CLI commands — agent-to-agent messaging.

Maps to /api/v2/ipc/* endpoints via IPCClient.
Issue #2812.
"""

from __future__ import annotations

import click

from nexus.cli.clients.ipc import IPCClient
from nexus.cli.output import add_output_options
from nexus.cli.service_command import ServiceResult, service_command
from nexus.cli.utils import REMOTE_API_KEY_OPTION, REMOTE_URL_OPTION


@click.group()
def ipc() -> None:
    """Agent-to-agent messaging.

    \b
    Send and receive messages between agents via their inboxes.

    \b
    Examples:
        nexus ipc send bob "Hello from Alice"
        nexus ipc inbox alice --json
        nexus ipc count alice
    """


@ipc.command("send")
@click.argument("to_agent")
@click.argument("message")
@click.option(
    "--type",
    "message_type",
    default="task",
    show_default=True,
    help="Message type (task, response, event, cancel)",
)
@click.option("--zone-id", default=None, help="Zone ID for cross-zone messaging")
@add_output_options
@REMOTE_API_KEY_OPTION
@REMOTE_URL_OPTION
@service_command(client_class=IPCClient)
def ipc_send(
    client: IPCClient,
    to_agent: str,
    message: str,
    message_type: str,
    zone_id: str | None,
) -> ServiceResult:
    """Send message to an agent's inbox.

    \b
    Examples:
        nexus ipc send bob "Hello from Alice"
        nexus ipc send bob "cancel task 42" --type cancel
        nexus ipc send bob "data ready" --zone-id org_acme --json
    """
    data = client.send(to_agent, message, message_type=message_type, zone_id=zone_id)

    def _render(d: dict) -> None:
        from nexus.cli.utils import console

        console.print("[green]Message sent[/green]")
        console.print(f"  Message ID: {d.get('message_id', 'N/A')}")
        console.print(f"  To:         {to_agent}")

    return ServiceResult(data=data, human_formatter=_render)


@ipc.command("inbox")
@click.argument("agent_id")
@click.option("--limit", default=50, show_default=True, help="Maximum messages to show")
@add_output_options
@REMOTE_API_KEY_OPTION
@REMOTE_URL_OPTION
@service_command(client_class=IPCClient)
def ipc_inbox(client: IPCClient, agent_id: str, limit: int) -> ServiceResult:
    """List messages in agent's inbox.

    \b
    Examples:
        nexus ipc inbox alice
        nexus ipc inbox alice --limit 10 --json
    """
    data = client.inbox(agent_id, limit=limit)

    def _render(d: dict) -> None:
        from rich.table import Table

        from nexus.cli.utils import console

        messages = d.get("messages", [])
        if not messages:
            console.print("[yellow]Inbox empty[/yellow]")
            return

        table = Table(title=f"Inbox for {agent_id} ({len(messages)} messages)")
        table.add_column("ID", style="dim")
        table.add_column("From")
        table.add_column("Type")
        table.add_column("Preview")
        table.add_column("Time", style="dim")

        for msg in messages:
            body = msg.get("body", "")
            preview = body[:60] + "..." if len(body) > 60 else body
            table.add_row(
                msg.get("message_id", "")[:12],
                msg.get("from_agent", ""),
                msg.get("message_type", ""),
                preview,
                msg.get("created_at", "")[:19],
            )
        console.print(table)

    return ServiceResult(data=data, human_formatter=_render)


@ipc.command("count")
@click.argument("agent_id")
@add_output_options
@REMOTE_API_KEY_OPTION
@REMOTE_URL_OPTION
@service_command(client_class=IPCClient)
def ipc_count(client: IPCClient, agent_id: str) -> ServiceResult:
    """Count messages in agent's inbox.

    \b
    Examples:
        nexus ipc count alice
        nexus ipc count alice --json
    """
    data = client.inbox_count(agent_id)

    def _render(d: dict) -> None:
        from nexus.cli.utils import console

        count = d.get("count", 0)
        console.print(f"[bold cyan]{agent_id}[/bold cyan]: {count} message(s) in inbox")

    return ServiceResult(data=data, human_formatter=_render)
