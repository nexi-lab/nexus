"""Memory management CLI commands (v0.4.0)."""

import json

import click

from nexus.cli.utils import get_default_filesystem


@click.group()
def memory() -> None:
    """Agent memory management commands."""
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
