"""Agent management CLI commands (v0.5.0).

Manage AI agents for delegation and multi-agent workflows.
"""

from typing import Any

import click
from rich.console import Console
from rich.table import Table

from nexus.cli.utils import add_backend_options, get_filesystem, handle_error

console = Console()


@click.group(name="agent")
def agent() -> None:
    """Manage AI agents (v0.5.0 ACE).

    Register and manage AI agents for delegation, multi-agent workflows,
    and permission inheritance.

    Examples:
        # Register agent (no API key - uses user's auth)
        nexus agent register alice "Data Analyst Agent"

        # Register agent with API key
        nexus agent register alice "Data Analyst Agent" --with-api-key

        # List all agents
        nexus agent list

        # Show agent info
        nexus agent info alice

        # Delete agent
        nexus agent delete alice
    """
    pass


@agent.command(name="register")
@click.argument("agent_id", type=str)
@click.argument("name", type=str)
@click.option("--with-api-key", is_flag=True, help="Generate API key for agent (not recommended)")
@click.option("--description", "-d", default="", help="Agent description")
@click.option(
    "--if-not-exists",
    is_flag=True,
    default=False,
    help="Succeed silently if agent exists, returning existing agent info",
)
@add_backend_options
def register_cmd(
    agent_id: str,
    name: str,
    with_api_key: bool,
    description: str,
    if_not_exists: bool,
    remote_url: str | None,
    remote_api_key: str | None,
) -> None:
    """Register a new AI agent.

    By default, agents do NOT get API keys. Instead, they use the owner's
    authentication with X-Agent-ID header (recommended).

    With --with-api-key flag, a unique API key is generated for the agent
    (for backward compatibility, but not recommended).

    Examples:
        # Recommended: Register without API key
        nexus agent register alice "Data Analyst Agent"

        # Idempotent: succeed if agent already exists
        nexus agent register alice "Data Analyst Agent" --if-not-exists

        # Legacy: Register with API key
        nexus agent register alice "Data Analyst Agent" --with-api-key
    """
    try:
        nx: Any = get_filesystem(remote_url, remote_api_key)

        try:
            result = nx._agent_rpc_service.register_agent(
                agent_id=agent_id,
                name=name,
                description=description,
                generate_api_key=with_api_key,
            )
        except Exception as reg_err:
            if if_not_exists and "already exists" in str(reg_err).lower():
                try:
                    existing = nx._agent_rpc_service.get_agent(agent_id)
                    console.print(f"[green]✓[/green] Agent already exists: {agent_id}")
                    console.print(f"  Name: {existing.get('name', name)}")
                    console.print(f"  Owner: {existing.get('user_id', 'unknown')}")
                except Exception:
                    console.print(f"[green]✓[/green] Agent already exists: {agent_id}")
                nx.close()
                return
            nx.close()
            raise

        console.print(f"[green]✓[/green] Registered agent: {result['agent_id']}")
        console.print(f"  Name: {result.get('name', name)}")
        if description:
            console.print(f"  Description: {description}")
        console.print(f"  Owner: {result.get('user_id', 'unknown')}")

        if with_api_key and result.get("api_key"):
            console.print("\n[yellow]⚠[/yellow] API Key (save securely):")
            console.print(f"  {result['api_key']}")
            console.print("\n[dim]Note: API key will not be shown again[/dim]")
        else:
            console.print("\n[cyan]ℹ[/cyan] No API key generated (recommended)")
            console.print("  Agent uses owner's auth + X-Agent-ID header")

        nx.close()

    except Exception as e:
        handle_error(e)


@agent.command(name="list")
@add_backend_options
def list_cmd(
    remote_url: str | None,
    remote_api_key: str | None,
) -> None:
    """List all registered agents.

    Examples:
        nexus agent list
    """
    try:
        nx: Any = get_filesystem(remote_url, remote_api_key)

        agents = nx._agent_rpc_service.list_agents()

        if not agents:
            console.print("[yellow]No agents registered[/yellow]")
            nx.close()
            return

        # Create table
        table = Table(title="Registered Agents")
        table.add_column("Agent ID", style="cyan")
        table.add_column("Name", style="green")
        table.add_column("Description", style="dim", no_wrap=False)
        table.add_column("Owner", style="dim")
        table.add_column("Created", style="dim")

        for agent in agents:
            created = agent.get("created_at", "")
            if created and isinstance(created, str):
                # Shorten ISO timestamp
                created = created.split("T")[0] if "T" in created else created

            # Truncate description if too long
            description = agent.get("description", "")
            if description and len(description) > 50:
                description = description[:47] + "..."

            table.add_row(
                agent["agent_id"],
                agent.get("name", agent["agent_id"]),
                description,
                agent.get("user_id", ""),
                created,
            )

        console.print(table)
        nx.close()

    except Exception as e:
        handle_error(e)


@agent.command(name="info")
@click.argument("agent_id", type=str)
@add_backend_options
def info_cmd(
    agent_id: str,
    remote_url: str | None,
    remote_api_key: str | None,
) -> None:
    """Show detailed information about an agent.

    Examples:
        nexus agent info alice
    """
    try:
        nx: Any = get_filesystem(remote_url, remote_api_key)

        agent = nx._agent_rpc_service.get_agent(agent_id)

        if not agent:
            console.print(f"[red]✗[/red] Agent not found: {agent_id}")
            nx.close()
            return

        console.print(f"[bold]Agent: {agent['agent_id']}[/bold]\n")
        console.print(f"  Name: {agent.get('name', agent['agent_id'])}")

        # Show description if available
        if "description" in agent and agent["description"]:
            console.print(f"  Description: {agent['description']}")

        console.print(f"  Owner: {agent.get('user_id', 'unknown')}")
        console.print(f"  Created: {agent.get('created_at', 'unknown')}")

        nx.close()

    except Exception as e:
        handle_error(e)


@agent.command(name="delete")
@click.argument("agent_id", type=str)
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompt")
@add_backend_options
def delete_cmd(
    agent_id: str,
    yes: bool,
    remote_url: str | None,
    remote_api_key: str | None,
) -> None:
    """Delete an agent.

    This removes the agent registration and any associated API keys.

    Examples:
        nexus agent delete alice
        nexus agent delete alice --yes
    """
    try:
        nx: Any = get_filesystem(remote_url, remote_api_key)

        # Confirm deletion
        if not yes:
            try:
                confirm = input(f"Delete agent '{agent_id}'? [y/N]: ")
                if confirm.lower() not in ("y", "yes"):
                    console.print("[yellow]Cancelled[/yellow]")
                    nx.close()
                    return
            except (EOFError, KeyboardInterrupt):
                console.print("\n[yellow]Cancelled[/yellow]")
                nx.close()
                return

        result = nx._agent_rpc_service.delete_agent(agent_id)

        if result:
            console.print(f"[green]✓[/green] Deleted agent: {agent_id}")
        else:
            console.print(f"[red]✗[/red] Agent not found: {agent_id}")

        nx.close()

    except Exception as e:
        handle_error(e)


def register_commands(cli: click.Group) -> None:
    """Register agent commands with the CLI."""
    cli.add_command(agent)
