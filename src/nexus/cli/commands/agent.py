"""Agent management CLI commands (v0.5.0).

Manage AI agents for delegation and multi-agent workflows.
"""

import asyncio
from typing import Any

import click
from rich.table import Table

from nexus.cli.clients.agent_ext import AgentExtClient
from nexus.cli.output import add_output_options
from nexus.cli.service_command import ServiceResult, service_command
from nexus.cli.theme import console
from nexus.cli.utils import (
    REMOTE_API_KEY_OPTION,
    REMOTE_URL_OPTION,
    add_backend_options,
    handle_error,
    open_filesystem,
)


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

    async def _impl() -> None:
        async with open_filesystem(remote_url, remote_api_key) as _nx:
            nx: Any = _nx
            try:
                result = nx.service("agent_rpc").register_agent(
                    agent_id=agent_id,
                    name=name,
                    description=description,
                    generate_api_key=with_api_key,
                )
            except Exception as reg_err:
                if if_not_exists and "already exists" in str(reg_err).lower():
                    try:
                        existing = nx.service("agent_rpc").get_agent(agent_id)
                        console.print(
                            f"[nexus.success]✓[/nexus.success] Agent already exists: {agent_id}"
                        )
                        console.print(f"  Name: {existing.get('name', name)}")
                        console.print(f"  Owner: {existing.get('user_id', 'unknown')}")
                    except Exception:
                        console.print(
                            f"[nexus.success]✓[/nexus.success] Agent already exists: {agent_id}"
                        )
                    return
                raise

            console.print(
                f"[nexus.success]✓[/nexus.success] Registered agent: {result['agent_id']}"
            )
            console.print(f"  Name: {result.get('name', name)}")
            if description:
                console.print(f"  Description: {description}")
            console.print(f"  Owner: {result.get('user_id', 'unknown')}")

            if with_api_key and result.get("api_key"):
                console.print("\n[nexus.warning]⚠[/nexus.warning] API Key (save securely):")
                console.print(f"  {result['api_key']}")
                console.print("\n[nexus.muted]Note: API key will not be shown again[/nexus.muted]")
            else:
                console.print("\n[nexus.value]ℹ[/nexus.value] No API key generated (recommended)")
                console.print("  Agent uses owner's auth + X-Agent-ID header")

    try:
        asyncio.run(_impl())
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

    async def _impl() -> None:
        async with open_filesystem(remote_url, remote_api_key) as _nx:
            nx: Any = _nx
            agents = nx.service("agent_rpc").list_agents()

            if not agents:
                console.print("[nexus.warning]No agents registered[/nexus.warning]")
                return

            table = Table(title="Registered Agents")
            table.add_column("Agent ID", style="nexus.value")
            table.add_column("Name", style="nexus.success")
            table.add_column("Description", style="nexus.muted", no_wrap=False)
            table.add_column("Owner", style="nexus.muted")
            table.add_column("Created", style="nexus.muted")

            for ag in agents:
                created = ag.get("created_at", "")
                if created and isinstance(created, str):
                    created = created.split("T")[0] if "T" in created else created

                desc = ag.get("description", "")
                if desc and len(desc) > 50:
                    desc = desc[:47] + "..."

                table.add_row(
                    ag["agent_id"],
                    ag.get("name", ag["agent_id"]),
                    desc,
                    ag.get("user_id", ""),
                    created,
                )

            console.print(table)

    try:
        asyncio.run(_impl())
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

    async def _impl() -> None:
        async with open_filesystem(remote_url, remote_api_key) as _nx:
            nx: Any = _nx
            ag = nx.service("agent_rpc").get_agent(agent_id)

            if not ag:
                console.print(f"[nexus.error]✗[/nexus.error] Agent not found: {agent_id}")
                return

            console.print(f"[bold]Agent: {ag['agent_id']}[/bold]\n")
            console.print(f"  Name: {ag.get('name', ag['agent_id'])}")

            if "description" in ag and ag["description"]:
                console.print(f"  Description: {ag['description']}")

            console.print(f"  Owner: {ag.get('user_id', 'unknown')}")
            console.print(f"  Created: {ag.get('created_at', 'unknown')}")

    try:
        asyncio.run(_impl())
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
    if not yes:
        try:
            confirm = input(f"Delete agent '{agent_id}'? [y/N]: ")
            if confirm.lower() not in ("y", "yes"):
                console.print("[nexus.warning]Cancelled[/nexus.warning]")
                return
        except (EOFError, KeyboardInterrupt):
            console.print("\n[nexus.warning]Cancelled[/nexus.warning]")
            return

    async def _impl() -> None:
        async with open_filesystem(remote_url, remote_api_key) as _nx:
            nx: Any = _nx
            result = nx.service("agent_rpc").delete_agent(agent_id)

            if result:
                console.print(f"[nexus.success]✓[/nexus.success] Deleted agent: {agent_id}")
            else:
                console.print(f"[nexus.error]✗[/nexus.error] Agent not found: {agent_id}")

    try:
        asyncio.run(_impl())
    except Exception as e:
        handle_error(e)


@agent.command(name="status")
@click.argument("agent_id", type=str)
@add_output_options
@REMOTE_API_KEY_OPTION
@REMOTE_URL_OPTION
@service_command(client_class=AgentExtClient)
def agent_status(client: AgentExtClient, agent_id: str) -> ServiceResult:
    """Show agent lifecycle state, generation, and zone.

    \b
    Examples:
        nexus agent status alice
        nexus agent status alice --json
    """
    data = client.status(agent_id)

    def _render(d: dict) -> None:
        console.print(f"[bold nexus.value]Agent Status: {agent_id}[/bold nexus.value]")
        console.print(f"  Phase:       {d.get('phase', 'N/A')}")
        console.print(f"  Generation:  {d.get('observed_generation', 'N/A')}")
        console.print(f"  Inbox:       {d.get('inbox_depth', 0)} message(s)")
        console.print(f"  Context:     {d.get('context_usage_pct', 0):.1f}%")
        if d.get("last_heartbeat"):
            console.print(f"  Heartbeat:   {d['last_heartbeat'][:19]}")
        if d.get("last_activity"):
            console.print(f"  Activity:    {d['last_activity'][:19]}")
        ru = d.get("resource_usage", {})
        if ru:
            console.print(f"  Tokens:      {ru.get('tokens_used', 0)}")
            console.print(f"  Storage:     {ru.get('storage_used_mb', 0):.1f} MB")
        conditions = d.get("conditions", [])
        if conditions:
            console.print("  Conditions:")
            for c in conditions:
                status_icon = (
                    "[nexus.success]OK[/nexus.success]"
                    if c.get("status") == "True"
                    else "[nexus.error]!![/nexus.error]"
                )
                console.print(f"    {status_icon} {c.get('type', '')}: {c.get('message', '')}")

    return ServiceResult(data=data, human_formatter=_render)


@agent.group(name="spec")
def agent_spec() -> None:
    """Agent capabilities specification."""


@agent_spec.command(name="show")
@click.argument("agent_id", type=str)
@add_output_options
@REMOTE_API_KEY_OPTION
@REMOTE_URL_OPTION
@service_command(client_class=AgentExtClient)
def agent_spec_show(client: AgentExtClient, agent_id: str) -> ServiceResult:
    """Show agent capabilities spec.

    \b
    Examples:
        nexus agent spec show alice --json
    """
    data = client.spec_show(agent_id)

    def _render(d: dict) -> None:
        import json

        console.print(f"[bold nexus.value]Agent Spec: {agent_id}[/bold nexus.value]")
        console.print(json.dumps(d, indent=2, default=str))

    return ServiceResult(data=data, human_formatter=_render)


@agent_spec.command(name="set")
@click.argument("agent_id", type=str)
@click.argument("spec_json", type=str)
@add_output_options
@REMOTE_API_KEY_OPTION
@REMOTE_URL_OPTION
@service_command(client_class=AgentExtClient)
def agent_spec_set(client: AgentExtClient, agent_id: str, spec_json: str) -> ServiceResult:
    """Set agent capabilities spec (JSON string).

    \b
    Examples:
        nexus agent spec set alice '{"tools": ["read", "write"]}'
    """
    import json

    spec = json.loads(spec_json)
    data = client.spec_set(agent_id, spec)
    return ServiceResult(data=data, message=f"Spec updated for {agent_id}")


@agent.command(name="warmup")
@click.argument("agent_id", type=str)
@add_output_options
@REMOTE_API_KEY_OPTION
@REMOTE_URL_OPTION
@service_command(client_class=AgentExtClient)
def agent_warmup(client: AgentExtClient, agent_id: str) -> ServiceResult:
    """Pre-warm an agent.

    \b
    Examples:
        nexus agent warmup alice
    """
    data = client.warmup(agent_id)
    return ServiceResult(data=data, message=f"Agent {agent_id} warmup initiated")


def register_commands(cli: click.Group) -> None:
    """Register agent commands with the CLI."""
    cli.add_command(agent)
