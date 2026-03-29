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
@service_command(client_class=IPCClient)
def ipc_send(
    client: IPCClient,
    recipient: str,
    message: str,
    sender: str,
    message_type: str,
) -> ServiceResult:
    """Send message to an agent's inbox.

    \b
    Examples:
        nexus ipc send bob "Hello" --from alice
        nexus ipc send bob "cancel task 42" --from alice --type cancel
    """
    data = client.send(sender, recipient, message, message_type=message_type)

    def _render(d: dict) -> None:
        from nexus.cli.theme import console

        console.print("[nexus.success]Message sent[/nexus.success]")
        console.print(f"  Message ID: {d.get('message_id', 'N/A')}")
        console.print(f"  From:       {sender}")
        console.print(f"  To:         {recipient}")

    return ServiceResult(data=data, human_formatter=_render)


@ipc.command("inbox")
@click.argument("agent_id")
@add_output_options
@REMOTE_API_KEY_OPTION
@REMOTE_URL_OPTION
@service_command(client_class=IPCClient)
def ipc_inbox(client: IPCClient, agent_id: str) -> ServiceResult:
    """List messages in agent's inbox.

    \b
    Examples:
        nexus ipc inbox alice
        nexus ipc inbox alice --json
    """
    data = client.inbox(agent_id)

    def _render(d: dict) -> None:
        from rich.table import Table

        from nexus.cli.theme import console

        messages = d.get("messages", [])
        if not messages:
            console.print("[nexus.warning]Inbox empty[/nexus.warning]")
            return

        table = Table(title=f"Inbox for {agent_id} ({len(messages)} messages)")
        table.add_column("File", style="nexus.muted")

        for msg in messages:
            table.add_row(msg.get("filename", str(msg)))
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
        from nexus.cli.theme import console

        count = d.get("count", 0)
        console.print(
            f"[bold nexus.value]{agent_id}[/bold nexus.value]: {count} message(s) in inbox"
        )

    return ServiceResult(data=data, human_formatter=_render)
