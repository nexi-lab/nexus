"""Memory management CLI commands (v0.4.0+)."""

import json
import re
from datetime import timedelta

import click
from rich.console import Console
from rich.table import Table

from nexus.cli.utils import (
    BackendConfig,
    add_backend_options,
    get_default_filesystem,
    get_filesystem,
    handle_error,
)

console = Console()


@click.group()
def memory() -> None:
    """Agent memory management and registry commands."""
    pass


@memory.command()
@click.argument("content")
@click.option("--scope", default="user", help="Memory scope (agent/user/tenant/global)")
@click.option(
    "--type", "memory_type", default=None, help="Memory type (fact/preference/experience)"
)
@click.option("--importance", type=float, default=None, help="Importance score (0.0-1.0)")
def store(content: str, scope: str, memory_type: str | None, importance: float | None) -> None:
    """Store a new memory.

    \b
    Examples:
        nexus memory store "User prefers Python" --scope user --type preference
        nexus memory store "API key is abc123" --scope agent --importance 0.9
    """
    nx = get_default_filesystem()

    try:
        memory_id = nx.memory.store(  # type: ignore[attr-defined]
            content=content,
            scope=scope,
            memory_type=memory_type,
            importance=importance,
        )
        click.echo(f"Memory stored: {memory_id}")
    except Exception as e:
        click.echo(f"Error storing memory: {e}", err=True)
        raise click.Abort() from e


@memory.command()
@click.option("--user-id", default=None, help="Filter by user ID")
@click.option("--agent-id", default=None, help="Filter by agent ID")
@click.option("--scope", default=None, help="Filter by scope")
@click.option("--type", "memory_type", default=None, help="Filter by memory type")
@click.option("--limit", type=int, default=100, help="Maximum number of results")
@click.option("--json", "output_json", is_flag=True, help="Output as JSON")
def query(
    user_id: str | None,
    agent_id: str | None,
    scope: str | None,
    memory_type: str | None,
    limit: int,
    output_json: bool,
) -> None:
    """Query memories by filters.

    \b
    Examples:
        nexus memory query --scope user --type preference
        nexus memory query --agent-id agent1 --limit 10
        nexus memory query --json
    """
    nx = get_default_filesystem()

    try:
        results = nx.memory.query(  # type: ignore[attr-defined]
            user_id=user_id,
            agent_id=agent_id,
            scope=scope,
            memory_type=memory_type,
            limit=limit,
        )

        if output_json:
            click.echo(json.dumps(results, indent=2))
        else:
            if not results:
                click.echo("No memories found.")
                return

            click.echo(f"Found {len(results)} memories:\n")
            for mem in results:
                click.echo(f"ID: {mem['memory_id']}")
                click.echo(
                    f"  Content: {mem['content'][:100]}..."
                    if len(mem["content"]) > 100
                    else f"  Content: {mem['content']}"
                )
                click.echo(f"  Scope: {mem['scope']}")
                if mem["memory_type"]:
                    click.echo(f"  Type: {mem['memory_type']}")
                if mem["importance"]:
                    click.echo(f"  Importance: {mem['importance']}")
                click.echo(f"  Created: {mem['created_at']}")
                click.echo()

    except Exception as e:
        click.echo(f"Error querying memories: {e}", err=True)
        raise click.Abort() from e


@memory.command()
@click.argument("query_text")
@click.option("--scope", default=None, help="Filter by scope")
@click.option("--type", "memory_type", default=None, help="Filter by memory type")
@click.option("--limit", type=int, default=10, help="Maximum number of results")
@click.option("--json", "output_json", is_flag=True, help="Output as JSON")
def search(
    query_text: str, scope: str | None, memory_type: str | None, limit: int, output_json: bool
) -> None:
    """Semantic search over memories.

    \b
    Examples:
        nexus memory search "Python programming"
        nexus memory search "user preferences" --scope user --limit 5
        nexus memory search "API keys" --json
    """
    nx = get_default_filesystem()

    try:
        results = nx.memory.search(  # type: ignore[attr-defined]
            query=query_text,
            scope=scope,
            memory_type=memory_type,
            limit=limit,
        )

        if output_json:
            click.echo(json.dumps(results, indent=2))
        else:
            if not results:
                click.echo("No memories found.")
                return

            click.echo(f"Found {len(results)} memories:\n")
            for mem in results:
                click.echo(f"ID: {mem['memory_id']} (score: {mem.get('score', 0):.2f})")
                click.echo(
                    f"  Content: {mem['content'][:100]}..."
                    if len(mem["content"]) > 100
                    else f"  Content: {mem['content']}"
                )
                click.echo(f"  Scope: {mem['scope']}")
                if mem["memory_type"]:
                    click.echo(f"  Type: {mem['memory_type']}")
                click.echo()

    except Exception as e:
        click.echo(f"Error searching memories: {e}", err=True)
        raise click.Abort() from e


@memory.command()
@click.option("--scope", default=None, help="Filter by scope")
@click.option("--type", "memory_type", default=None, help="Filter by memory type")
@click.option("--limit", type=int, default=100, help="Maximum number of results")
@click.option("--json", "output_json", is_flag=True, help="Output as JSON")
def list(scope: str | None, memory_type: str | None, limit: int, output_json: bool) -> None:
    """List memories for current user/agent.

    \b
    Examples:
        nexus memory list
        nexus memory list --scope user --type preference
        nexus memory list --json
    """
    nx = get_default_filesystem()

    try:
        results = nx.memory.list(  # type: ignore[attr-defined]
            scope=scope,
            memory_type=memory_type,
            limit=limit,
        )

        if output_json:
            click.echo(json.dumps(results, indent=2))
        else:
            if not results:
                click.echo("No memories found.")
                return

            click.echo(f"Found {len(results)} memories:\n")
            for mem in results:
                click.echo(f"ID: {mem['memory_id']}")
                click.echo(f"  User: {mem['user_id']}, Agent: {mem['agent_id']}")
                click.echo(f"  Scope: {mem['scope']}")
                if mem["memory_type"]:
                    click.echo(f"  Type: {mem['memory_type']}")
                if mem["importance"]:
                    click.echo(f"  Importance: {mem['importance']}")
                click.echo(f"  Created: {mem['created_at']}")
                click.echo()

    except Exception as e:
        click.echo(f"Error listing memories: {e}", err=True)
        raise click.Abort() from e


@memory.command()
@click.argument("memory_id")
@click.option("--json", "output_json", is_flag=True, help="Output as JSON")
def get(memory_id: str, output_json: bool) -> None:
    """Get a specific memory by ID.

    \b
    Examples:
        nexus memory get mem_123
        nexus memory get mem_123 --json
    """
    nx = get_default_filesystem()

    try:
        result = nx.memory.get(memory_id)  # type: ignore[attr-defined]

        if not result:
            click.echo(f"Memory not found: {memory_id}", err=True)
            raise click.Abort()

        if output_json:
            click.echo(json.dumps(result, indent=2))
        else:
            click.echo(f"Memory ID: {result['memory_id']}")
            click.echo(f"Content: {result['content']}")
            click.echo(f"User: {result['user_id']}, Agent: {result['agent_id']}")
            click.echo(f"Scope: {result['scope']}, Visibility: {result['visibility']}")
            if result["memory_type"]:
                click.echo(f"Type: {result['memory_type']}")
            if result["importance"]:
                click.echo(f"Importance: {result['importance']}")
            click.echo(f"Created: {result['created_at']}")
            click.echo(f"Updated: {result['updated_at']}")

    except Exception as e:
        click.echo(f"Error getting memory: {e}", err=True)
        raise click.Abort() from e


@memory.command()
@click.argument("memory_id")
def delete(memory_id: str) -> None:
    """Delete a memory by ID.

    \b
    Examples:
        nexus memory delete mem_123
    """
    nx = get_default_filesystem()

    try:
        if nx.memory.delete(memory_id):  # type: ignore[attr-defined]
            click.echo(f"Memory deleted: {memory_id}")
        else:
            click.echo(f"Memory not found or no permission: {memory_id}", err=True)
            raise click.Abort()

    except Exception as e:
        click.echo(f"Error deleting memory: {e}", err=True)
        raise click.Abort() from e


# ===== Memory Registry Commands (v0.7.0) =====


@memory.command(name="register")
@click.argument("path", type=str)
@click.option("--name", "-n", default=None, help="Friendly name for memory")
@click.option("--description", "-d", default="", help="Description of memory")
@click.option("--created-by", default=None, help="User/agent who created it")
@click.option(
    "--session-id", default=None, help="Session ID for temporary session-scoped memory (v0.5.0)"
)
@click.option(
    "--ttl", default=None, help="Time-to-live (e.g., '8h', '2d', '30m') for auto-expiry (v0.5.0)"
)
@add_backend_options
def register_memory_cmd(
    path: str,
    name: str | None,
    description: str,
    created_by: str | None,
    session_id: str | None,
    ttl: str | None,
    backend_config: BackendConfig,
) -> None:
    """Register a directory as a memory.

    Memories support consolidation, semantic search, and versioning.

    Examples:
        # Persistent memory (traditional)
        nexus memory register /my-memory --name kb

        # Temporary agent memory (v0.5.0 - auto-expire after task)
        nexus memory register /tmp/agent-context --session-id abc123 --ttl 2h
    """
    try:
        nx = get_filesystem(backend_config)

        # v0.5.0: Parse TTL string to timedelta
        ttl_delta = None
        if ttl:
            ttl_delta = _parse_ttl(ttl)

        result = nx.register_memory(
            path=path,
            name=name,
            description=description,
            created_by=created_by,
            session_id=session_id,  # v0.5.0
            ttl=ttl_delta,  # v0.5.0
        )

        console.print(f"[green]✓[/green] Registered memory: {result['path']}")
        if result["name"]:
            console.print(f"  Name: {result['name']}")
        if result["description"]:
            console.print(f"  Description: {result['description']}")
        if result["created_by"]:
            console.print(f"  Created by: {result['created_by']}")
        # v0.5.0: Show session-scoped info
        if session_id:
            console.print(f"  Session: {session_id} (temporary)")
            if ttl_delta:
                console.print(f"  TTL: {ttl} (auto-expires)")

        nx.close()

    except Exception as e:
        handle_error(e)


@memory.command(name="list-registered")
@add_backend_options
def list_registered_cmd(
    backend_config: BackendConfig,
) -> None:
    """List all registered memories.

    Examples:
        nexus memory list-registered
    """
    try:
        nx = get_filesystem(backend_config)

        memories = nx.list_memories()

        if not memories:
            console.print("[yellow]No memories registered[/yellow]")
            nx.close()
            return

        # Create table
        table = Table(title="Registered Memories")
        table.add_column("Path", style="cyan")
        table.add_column("Name", style="green")
        table.add_column("Description")
        table.add_column("Created By", style="dim")

        for mem in memories:
            table.add_row(
                mem["path"],
                mem["name"] or "",
                mem["description"] or "",
                mem["created_by"] or "",
            )

        console.print(table)
        console.print(f"\n[dim]{len(memories)} memory/memories registered[/dim]")

        nx.close()

    except Exception as e:
        handle_error(e)


@memory.command(name="unregister")
@click.argument("path", type=str)
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompt")
@add_backend_options
def unregister_memory_cmd(
    path: str,
    yes: bool,
    backend_config: BackendConfig,
) -> None:
    """Unregister a memory (does NOT delete files).

    This removes the memory from the registry but keeps all files intact.

    Examples:
        nexus memory unregister /my-memory
        nexus memory unregister /my-memory --yes
    """
    try:
        nx = get_filesystem(backend_config)

        # Get memory info first
        info = nx.get_memory_info(path)
        if not info:
            console.print(f"[red]✗[/red] Memory not registered: {path}")
            nx.close()
            return

        # Confirm
        if not yes:
            console.print(f"[yellow]⚠[/yellow]  About to unregister memory: {path}")
            if info["name"]:
                console.print(f"    Name: {info['name']}")
            if info["description"]:
                console.print(f"    Description: {info['description']}")
            console.print(
                "\n[dim]Note: Files will NOT be deleted, only registry entry removed[/dim]"
            )

            if not click.confirm("Continue?"):
                console.print("[yellow]Cancelled[/yellow]")
                nx.close()
                return

        # Unregister
        result = nx.unregister_memory(path)

        if result:
            console.print(f"[green]✓[/green] Unregistered memory: {path}")
        else:
            console.print(f"[red]✗[/red] Failed to unregister memory: {path}")

        nx.close()

    except Exception as e:
        handle_error(e)


@memory.command(name="info")
@click.argument("path", type=str)
@add_backend_options
def memory_info_cmd(
    path: str,
    backend_config: BackendConfig,
) -> None:
    """Show information about a registered memory.

    Examples:
        nexus memory info /my-memory
    """
    try:
        nx = get_filesystem(backend_config)

        info = nx.get_memory_info(path)

        if not info:
            console.print(f"[red]✗[/red] Memory not registered: {path}")
            nx.close()
            return

        console.print(f"[bold]Memory: {info['path']}[/bold]\n")
        if info["name"]:
            console.print(f"Name: {info['name']}")
        if info["description"]:
            console.print(f"Description: {info['description']}")
        if info["created_at"]:
            console.print(f"Created: {info['created_at']}")
        if info["created_by"]:
            console.print(f"Created by: {info['created_by']}")

        nx.close()

    except Exception as e:
        handle_error(e)


def _parse_ttl(ttl_str: str) -> timedelta:
    """Parse TTL string to timedelta.

    Supports formats like: 8h, 2d, 30m, 1w, 90s

    Args:
        ttl_str: TTL string (e.g., "8h", "2d", "30m")

    Returns:
        timedelta object

    Raises:
        ValueError: If format is invalid
    """
    pattern = r"^(\d+)([smhdw])$"
    match = re.match(pattern, ttl_str.lower())
    if not match:
        raise ValueError(
            f"Invalid TTL format: '{ttl_str}'. Expected format like '8h', '2d', '30m', '1w', '90s'"
        )

    value, unit = match.groups()
    value = int(value)

    if unit == "s":
        return timedelta(seconds=value)
    elif unit == "m":
        return timedelta(minutes=value)
    elif unit == "h":
        return timedelta(hours=value)
    elif unit == "d":
        return timedelta(days=value)
    elif unit == "w":
        return timedelta(weeks=value)
    else:
        raise ValueError(f"Invalid time unit: '{unit}'")
