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
@click.option(
    "--namespace", default=None, help="Hierarchical namespace (e.g., 'knowledge/geography/facts')"
)
@click.option("--path-key", default=None, help="Optional unique key for upsert mode")
@click.option(
    "--state", default="active", help="Memory state (inactive/active). Defaults to 'active'. #368"
)
def store(
    content: str,
    scope: str,
    memory_type: str | None,
    importance: float | None,
    namespace: str | None,
    path_key: str | None,
    state: str,
) -> None:
    """Store a new memory.

    \b
    Examples:
        nexus memory store "User prefers Python" --scope user --type preference
        nexus memory store "Paris is capital of France" --namespace "knowledge/geography/facts"
        nexus memory store "theme:dark" --namespace "user/preferences/ui" --path-key settings
        nexus memory store "Unverified info" --state inactive
    """
    nx = get_default_filesystem()

    try:
        memory_id = nx.memory.store(  # type: ignore[attr-defined]
            content=content,
            scope=scope,
            memory_type=memory_type,
            importance=importance,
            namespace=namespace,
            path_key=path_key,
            state=state,
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
@click.option(
    "--state",
    default="active",
    help="Filter by state (inactive/active/all). Defaults to 'active'. #368",
)
@click.option("--limit", type=int, default=100, help="Maximum number of results")
@click.option("--json", "output_json", is_flag=True, help="Output as JSON")
def query(
    user_id: str | None,
    agent_id: str | None,
    scope: str | None,
    memory_type: str | None,
    state: str,
    limit: int,
    output_json: bool,
) -> None:
    """Query memories by filters.

    \b
    Examples:
        nexus memory query --scope user --type preference
        nexus memory query --agent-id agent1 --limit 10
        nexus memory query --state inactive
        nexus memory query --json
    """
    nx = get_default_filesystem()

    try:
        # Note: user_id and agent_id filtering not supported in remote mode yet
        results = nx.memory.query(  # type: ignore[attr-defined]
            scope=scope,
            memory_type=memory_type,
            state=state,
            limit=limit,
        )

        # Client-side filtering if user_id or agent_id specified
        if user_id or agent_id:
            results = [
                r
                for r in results
                if (not user_id or r.get("user_id") == user_id)
                and (not agent_id or r.get("agent_id") == agent_id)
            ]

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
@click.option(
    "--mode",
    "search_mode",
    type=click.Choice(["semantic", "keyword", "hybrid"], case_sensitive=False),
    default="hybrid",
    help="Search mode: semantic (vector), keyword (text), or hybrid (default: hybrid)",
)
@click.option(
    "--provider",
    "embedding_provider",
    type=click.Choice(["openai", "voyage", "openrouter"], case_sensitive=False),
    default=None,
    help="Embedding provider for semantic search (default: auto-detect from env)",
)
@click.option("--json", "output_json", is_flag=True, help="Output as JSON")
def search(
    query_text: str,
    scope: str | None,
    memory_type: str | None,
    limit: int,
    search_mode: str,
    embedding_provider: str | None,
    output_json: bool,
) -> None:
    """Semantic search over memories.

    \b
    Examples:
        # Hybrid search (default - combines semantic + keyword)
        nexus memory search "Python programming"

        # Semantic-only search (requires API key)
        nexus memory search "user preferences" --mode semantic

        # Keyword-only search (no API key needed)
        nexus memory search "OAuth" --mode keyword

        # With filters
        nexus memory search "API keys" --scope user --type preference --limit 5

        # Specify embedding provider
        nexus memory search "authentication" --provider openrouter

        # JSON output
        nexus memory search "database" --json
    """
    nx = get_default_filesystem()

    try:
        # Create embedding provider if specified
        embedding_provider_obj = None
        if embedding_provider and search_mode in ("semantic", "hybrid"):
            try:
                from nexus.search.embeddings import create_embedding_provider

                embedding_provider_obj = create_embedding_provider(provider=embedding_provider)
            except Exception as e:
                click.echo(
                    f"Warning: Failed to create embedding provider '{embedding_provider}': {e}",
                    err=True,
                )
                click.echo("Falling back to keyword search.", err=True)
                search_mode = "keyword"

        results = nx.memory.search(  # type: ignore[attr-defined]
            query=query_text,
            scope=scope,
            memory_type=memory_type,
            limit=limit,
            search_mode=search_mode,
            embedding_provider=embedding_provider_obj,
        )

        if output_json:
            click.echo(json.dumps(results, indent=2))
        else:
            if not results:
                click.echo("No memories found.")
                return

            click.echo(f"Found {len(results)} memories (mode: {search_mode}):\n")
            for mem in results:
                score = mem.get("score", 0)
                semantic_score = mem.get("semantic_score")
                keyword_score = mem.get("keyword_score")

                # Build score display
                if semantic_score is not None and keyword_score is not None:
                    score_str = f"score: {score:.3f} (semantic: {semantic_score:.3f}, keyword: {keyword_score:.3f})"
                else:
                    score_str = f"score: {score:.3f}"

                click.echo(f"ID: {mem['memory_id']} ({score_str})")
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
@click.option("--namespace", default=None, help="Filter by exact namespace")
@click.option("--namespace-prefix", default=None, help="Filter by namespace prefix (hierarchical)")
@click.option("--state", default="active", help="Filter by state (inactive/active/all)")
@click.option("--limit", type=int, default=100, help="Maximum number of results")
@click.option("--json", "output_json", is_flag=True, help="Output as JSON")
def list(
    scope: str | None,
    memory_type: str | None,
    namespace: str | None,
    namespace_prefix: str | None,
    state: str,
    limit: int,
    output_json: bool,
) -> None:
    """List memories for current user/agent.

    \b
    Examples:
        nexus memory list
        nexus memory list --namespace "knowledge/geography/facts"
        nexus memory list --namespace-prefix "knowledge/"
        nexus memory list --state inactive  # List pending memories
        nexus memory list --state all  # List all memories
        nexus memory list --json
    """
    nx = get_default_filesystem()

    try:
        results = nx.memory.list(  # type: ignore[attr-defined]
            scope=scope,
            memory_type=memory_type,
            namespace=namespace,
            namespace_prefix=namespace_prefix,
            state=state,
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
                if mem.get("state"):
                    click.echo(f"  State: {mem['state']}")
                if mem.get("namespace"):
                    click.echo(f"  Namespace: {mem['namespace']}")
                    if mem.get("path_key"):
                        click.echo(f"  Path Key: {mem['path_key']}")
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
@click.argument("path")
@click.option("--json", "output_json", is_flag=True, help="Output as JSON")
def retrieve(path: str, output_json: bool) -> None:
    """Retrieve a memory by namespace path (namespace/path_key).

    \b
    Examples:
        nexus memory retrieve "user/preferences/ui/settings"
        nexus memory retrieve "knowledge/geography/facts/paris" --json
    """
    nx = get_default_filesystem()

    try:
        result = nx.memory.retrieve(path=path)  # type: ignore[attr-defined]

        if not result:
            click.echo(f"Memory not found at path: {path}", err=True)
            raise click.Abort()

        if output_json:
            click.echo(json.dumps(result, indent=2))
        else:
            click.echo(f"Memory ID: {result['memory_id']}")
            click.echo(f"Namespace: {result.get('namespace', 'N/A')}")
            click.echo(f"Path Key: {result.get('path_key', 'N/A')}")
            click.echo(f"Content: {result['content']}")
            click.echo(f"User: {result['user_id']}, Agent: {result['agent_id']}")
            click.echo(f"Scope: {result['scope']}, Visibility: {result['visibility']}")
            if result.get("memory_type"):
                click.echo(f"Type: {result['memory_type']}")
            if result.get("importance"):
                click.echo(f"Importance: {result['importance']}")
            click.echo(f"Created: {result['created_at']}")
            click.echo(f"Updated: {result['updated_at']}")

    except Exception as e:
        click.echo(f"Error retrieving memory: {e}", err=True)
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


@memory.command()
@click.argument("memory_id")
def approve(memory_id: str) -> None:
    """Approve a memory (activate it).

    \b
    Examples:
        nexus memory approve mem_123
    """
    nx = get_default_filesystem()

    try:
        if nx.memory.approve(memory_id):  # type: ignore[attr-defined]
            click.echo(f"Memory approved: {memory_id}")
        else:
            click.echo(f"Memory not found or no permission: {memory_id}", err=True)
            raise click.Abort()

    except Exception as e:
        click.echo(f"Error approving memory: {e}", err=True)
        raise click.Abort() from e


@memory.command()
@click.argument("memory_id")
def deactivate(memory_id: str) -> None:
    """Deactivate a memory (make it inactive).

    \b
    Examples:
        nexus memory deactivate mem_123
    """
    nx = get_default_filesystem()

    try:
        if nx.memory.deactivate(memory_id):  # type: ignore[attr-defined]
            click.echo(f"Memory deactivated: {memory_id}")
        else:
            click.echo(f"Memory not found or no permission: {memory_id}", err=True)
            raise click.Abort()

    except Exception as e:
        click.echo(f"Error deactivating memory: {e}", err=True)
        raise click.Abort() from e


@memory.command(name="approve-batch")
@click.argument("memory_ids", nargs=-1, required=True)
@click.option("--json", "output_json", is_flag=True, help="Output as JSON")
def approve_batch(memory_ids: tuple[str, ...], output_json: bool) -> None:
    """Approve multiple memories at once.

    \b
    Examples:
        nexus memory approve-batch mem_1 mem_2 mem_3
        nexus memory approve-batch mem_1 mem_2 --json
    """
    nx = get_default_filesystem()

    try:
        result = nx.memory.approve_batch(list(memory_ids))  # type: ignore[attr-defined]

        if output_json:
            click.echo(json.dumps(result, indent=2))
        else:
            click.echo(f"Approved: {result['approved']}")
            click.echo(f"Failed: {result['failed']}")
            if result["failed"] > 0:
                click.echo(f"Failed IDs: {', '.join(result['failed_ids'])}")

    except Exception as e:
        click.echo(f"Error approving memories: {e}", err=True)
        raise click.Abort() from e


@memory.command(name="deactivate-batch")
@click.argument("memory_ids", nargs=-1, required=True)
@click.option("--json", "output_json", is_flag=True, help="Output as JSON")
def deactivate_batch(memory_ids: tuple[str, ...], output_json: bool) -> None:
    """Deactivate multiple memories at once.

    \b
    Examples:
        nexus memory deactivate-batch mem_1 mem_2 mem_3
        nexus memory deactivate-batch mem_1 mem_2 --json
    """
    nx = get_default_filesystem()

    try:
        result = nx.memory.deactivate_batch(list(memory_ids))  # type: ignore[attr-defined]

        if output_json:
            click.echo(json.dumps(result, indent=2))
        else:
            click.echo(f"Deactivated: {result['deactivated']}")
            click.echo(f"Failed: {result['failed']}")
            if result["failed"] > 0:
                click.echo(f"Failed IDs: {', '.join(result['failed_ids'])}")

    except Exception as e:
        click.echo(f"Error deactivating memories: {e}", err=True)
        raise click.Abort() from e


@memory.command(name="delete-batch")
@click.argument("memory_ids", nargs=-1, required=True)
@click.option("--json", "output_json", is_flag=True, help="Output as JSON")
def delete_batch(memory_ids: tuple[str, ...], output_json: bool) -> None:
    """Delete multiple memories at once.

    \b
    Examples:
        nexus memory delete-batch mem_1 mem_2 mem_3
        nexus memory delete-batch mem_1 mem_2 --json
    """
    nx = get_default_filesystem()

    try:
        result = nx.memory.delete_batch(list(memory_ids))  # type: ignore[attr-defined]

        if output_json:
            click.echo(json.dumps(result, indent=2))
        else:
            click.echo(f"Deleted: {result['deleted']}")
            click.echo(f"Failed: {result['failed']}")
            if result["failed"] > 0:
                click.echo(f"Failed IDs: {', '.join(result['failed_ids'])}")

    except Exception as e:
        click.echo(f"Error deleting memories: {e}", err=True)
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


# ========== ACE (Agentic Context Engineering) Commands (v0.5.0) ==========


@memory.group(name="trajectory")
def trajectory_group() -> None:
    """Manage execution trajectories for learning."""
    pass


@trajectory_group.command("start")
@click.argument("task_description")
@click.option("--type", "task_type", help="Task type (e.g., 'api_call', 'data_processing')")
@click.option("--json", "output_json", is_flag=True, help="Output as JSON")
def trajectory_start(task_description: str, task_type: str | None, output_json: bool) -> None:
    """Start tracking a new execution trajectory.

    Example:
        nexus memory trajectory start "Process customer data" --type data_processing
    """
    nx = get_default_filesystem()
    try:
        traj_id = nx.memory.start_trajectory(task_description, task_type)  # type: ignore[attr-defined]
        if output_json:
            click.echo(json.dumps({"trajectory_id": traj_id}))
        else:
            console.print(f"[green]✓[/green] Started trajectory: {traj_id}")
        nx.close()
    except Exception as e:
        nx.close()
        handle_error(e)


@trajectory_group.command("log")
@click.argument("trajectory_id")
@click.argument("description")
@click.option(
    "--type", "step_type", default="action", help="Step type (action/decision/observation)"
)
def trajectory_log(trajectory_id: str, description: str, step_type: str) -> None:
    """Log a step in the trajectory.

    Example:
        nexus memory trajectory log traj_123 "Parsed 100 records" --type action
    """
    nx = get_default_filesystem()
    try:
        nx.memory.log_trajectory_step(trajectory_id, step_type, description)  # type: ignore[attr-defined]
        console.print(f"[green]✓[/green] Logged step to {trajectory_id}")
        nx.close()
    except Exception as e:
        nx.close()
        handle_error(e)


@trajectory_group.command("complete")
@click.argument("trajectory_id")
@click.option("--status", type=click.Choice(["success", "failure", "partial"]), required=True)
@click.option("--score", type=float, help="Success score (0.0-1.0)")
def trajectory_complete(trajectory_id: str, status: str, score: float | None) -> None:
    """Complete a trajectory with outcome.

    Example:
        nexus memory trajectory complete traj_123 --status success --score 0.95
    """
    nx = get_default_filesystem()
    try:
        nx.memory.complete_trajectory(trajectory_id, status, score)  # type: ignore[attr-defined]
        console.print(f"[green]✓[/green] Completed trajectory {trajectory_id} [{status}]")
        nx.close()
    except Exception as e:
        nx.close()
        handle_error(e)


@trajectory_group.command("list")
@click.option("--agent-id", help="Filter by agent ID")
@click.option("--status", help="Filter by status")
@click.option("--limit", type=int, default=50, help="Maximum results")
@click.option("--json", "output_json", is_flag=True, help="Output as JSON")
def trajectory_list(
    agent_id: str | None,
    status: str | None,
    limit: int,
    output_json: bool,
) -> None:
    """List execution trajectories.

    Example:
        nexus memory trajectory list --agent-id agent_a --status success
    """
    nx = get_default_filesystem()
    try:
        # Use Memory API instead of direct metadata access (supports both local and remote)
        trajectories = nx.memory.query_trajectories(  # type: ignore[attr-defined]
            agent_id=agent_id,
            status=status,
            limit=limit,
        )

        if output_json:
            click.echo(json.dumps(trajectories, indent=2))
        else:
            if not trajectories:
                console.print("[yellow]No trajectories found[/yellow]")
            else:
                table = Table(title="Execution Trajectories")
                table.add_column("ID", style="cyan")
                table.add_column("Task", style="white")
                table.add_column("Status", style="green")
                table.add_column("Score", style="yellow")

                for traj in trajectories:
                    score_str = (
                        f"{traj.get('success_score', 0.0):.2f}"
                        if traj.get("success_score")
                        else "N/A"
                    )
                    table.add_row(
                        traj["trajectory_id"][:12],
                        traj["task_description"][:50],
                        traj["status"],
                        score_str,
                    )

                console.print(table)

        nx.close()
    except Exception as e:
        nx.close()
        handle_error(e)


@memory.command("reflect")
@click.argument("trajectory_id", required=False)
@click.option("--batch", is_flag=True, help="Batch reflection mode")
@click.option("--since", help="ISO timestamp for batch reflection (e.g., 2025-10-01T00:00:00Z)")
@click.option("--min-count", type=int, default=10, help="Minimum trajectories for batch")
@click.option("--json", "output_json", is_flag=True, help="Output as JSON")
def reflect_cmd(
    trajectory_id: str | None,
    batch: bool,
    since: str | None,
    min_count: int,
    output_json: bool,
) -> None:
    """Reflect on trajectories to extract learnings.

    \b
    Single reflection:
        nexus memory reflect traj_123

    \b
    Batch reflection:
        nexus memory reflect --batch --since 2025-10-01 --min-count 10
    """
    nx = get_default_filesystem()
    try:
        if batch:
            # Batch reflection
            result = nx.memory.batch_reflect(since=since, min_trajectories=min_count)  # type: ignore[attr-defined]

            if output_json:
                click.echo(json.dumps(result, indent=2))
            else:
                console.print("\n[bold]Batch Reflection Results[/bold]")
                console.print(f"Trajectories analyzed: {result['trajectories_analyzed']}")

                if result.get("common_patterns"):
                    console.print("\n[green]✓ Common Successful Patterns:[/green]")
                    for pattern in result["common_patterns"][:5]:
                        console.print(
                            f"  • {pattern['description']} (freq: {pattern['frequency']})"
                        )

                if result.get("common_failures"):
                    console.print("\n[red]✗ Common Failure Patterns:[/red]")
                    for pattern in result["common_failures"][:5]:
                        console.print(
                            f"  • {pattern['description']} (freq: {pattern['frequency']})"
                        )
        else:
            # Single trajectory reflection
            if not trajectory_id:
                console.print("[red]Error:[/red] trajectory_id required for single reflection")
                nx.close()
                return

            reflection = nx.memory.reflect(trajectory_id)  # type: ignore[attr-defined]

            if output_json:
                click.echo(json.dumps(reflection, indent=2))
            else:
                console.print(f"\n[bold]Reflection for {trajectory_id}[/bold]")

                if reflection.get("helpful_strategies"):
                    console.print("\n[green]✓ Helpful Strategies:[/green]")
                    for s in reflection["helpful_strategies"]:
                        console.print(f"  • {s['description']}")

                if reflection.get("harmful_patterns"):
                    console.print("\n[red]✗ Harmful Patterns:[/red]")
                    for s in reflection["harmful_patterns"]:
                        console.print(f"  • {s['description']}")

                console.print(f"\nReflection memory ID: {reflection.get('memory_id')}")

        nx.close()
    except Exception as e:
        nx.close()
        handle_error(e)


@memory.group(name="playbook")
def playbook_group() -> None:
    """Manage agent playbooks with learned strategies."""
    pass


@playbook_group.command("get")
@click.argument("name", default="default")
@click.option("--json", "output_json", is_flag=True, help="Output as JSON")
def playbook_get(name: str, output_json: bool) -> None:
    """View playbook strategies.

    Example:
        nexus memory playbook get default
    """
    nx = get_default_filesystem()
    try:
        playbook = nx.memory.get_playbook(name)  # type: ignore[attr-defined]

        if not playbook:
            console.print(f"[yellow]Playbook '{name}' not found[/yellow]")
            nx.close()
            return

        if output_json:
            click.echo(json.dumps(playbook, indent=2))
        else:
            console.print(f"\n[bold]{playbook['name']}[/bold] (v{playbook['version']})")
            console.print(f"Scope: {playbook['scope']}")
            console.print(f"Usage: {playbook['usage_count']} times")
            console.print(f"Success Rate: {playbook['success_rate']:.1%}")

            strategies = playbook.get("content", {}).get("strategies", [])
            if strategies:
                console.print("\n[bold]Strategies:[/bold]")
                for s in strategies:
                    marker = {
                        "helpful": "[green]✓[/green]",
                        "harmful": "[red]✗[/red]",
                        "neutral": "[yellow]○[/yellow]",
                    }.get(s.get("type", "neutral"), "○")
                    console.print(f"  {marker} {s.get('description', 'N/A')}")

        nx.close()
    except Exception as e:
        nx.close()
        handle_error(e)


@playbook_group.command("update")
@click.argument("name", default="default")
@click.option("--strategies", required=True, help="Path to strategies JSON file")
def playbook_update(name: str, strategies: str) -> None:
    """Update playbook with new strategies.

    \b
    Strategies JSON format:
    [
      {
        "category": "helpful",
        "pattern": "Always validate input before processing",
        "context": "Data processing tasks",
        "confidence": 0.9
      }
    ]

    Example:
        nexus memory playbook update default --strategies strategies.json
    """
    nx = get_default_filesystem()
    try:
        with open(strategies) as f:
            strategies_data = json.load(f)

        result = nx.memory.update_playbook(strategies_data, name)  # type: ignore[attr-defined]

        console.print(f"[green]✓[/green] Updated playbook '{name}'")
        console.print(f"  Added {result['strategies_added']} strategies")

        nx.close()
    except Exception as e:
        nx.close()
        handle_error(e)


@playbook_group.command("curate")
@click.option("--reflections", required=True, help="Comma-separated reflection memory IDs")
@click.option("--name", default="default", help="Playbook name")
@click.option("--json", "output_json", is_flag=True, help="Output as JSON")
def playbook_curate(reflections: str, name: str, output_json: bool) -> None:
    """Auto-curate playbook from reflections.

    Example:
        nexus memory playbook curate --reflections mem_1,mem_2,mem_3
    """
    nx = get_default_filesystem()
    try:
        reflection_ids = [rid.strip() for rid in reflections.split(",")]
        result = nx.memory.curate_playbook(reflection_ids, name)  # type: ignore[attr-defined]

        if output_json:
            click.echo(json.dumps(result, indent=2))
        else:
            console.print(f"[green]✓[/green] Curated playbook '{name}'")
            console.print(f"  Strategies added: {result.get('strategies_added', 0)}")
            console.print(f"  Strategies merged: {result.get('strategies_merged', 0)}")

        nx.close()
    except Exception as e:
        nx.close()
        handle_error(e)


@playbook_group.command("list")
@click.option("--scope", help="Filter by scope")
@click.option("--json", "output_json", is_flag=True, help="Output as JSON")
def playbook_list(scope: str | None, output_json: bool) -> None:
    """List playbooks.

    Example:
        nexus memory playbook list --scope agent
    """
    nx = get_default_filesystem()
    try:
        # Use Memory API instead of direct metadata access (supports both local and remote)
        playbooks = nx.memory.query_playbooks(agent_id=None, scope=scope, limit=50)  # type: ignore[attr-defined]

        if output_json:
            click.echo(json.dumps(playbooks, indent=2))
        else:
            if not playbooks:
                console.print("[yellow]No playbooks found[/yellow]")
            else:
                table = Table(title="Playbooks")
                table.add_column("Name", style="cyan")
                table.add_column("Version", style="white")
                table.add_column("Usage", style="yellow")
                table.add_column("Success Rate", style="green")

                for pb in playbooks:
                    table.add_row(
                        pb["name"],
                        str(pb["version"]),
                        str(pb["usage_count"]),
                        f"{pb['success_rate']:.1%}",
                    )

                console.print(table)

        nx.close()
    except Exception as e:
        nx.close()
        handle_error(e)


@memory.command("consolidate")
@click.option("--type", "memory_type", help="Filter by memory type")
@click.option("--threshold", type=float, default=0.8, help="Importance threshold")
@click.option("--dry-run", is_flag=True, help="Show what would be consolidated")
@click.option("--json", "output_json", is_flag=True, help="Output as JSON")
def consolidate_cmd(
    memory_type: str | None,
    threshold: float,
    dry_run: bool,
    output_json: bool,
) -> None:
    """Consolidate memories to prevent context collapse.

    Example:
        nexus memory consolidate --type experience --threshold 0.8
        nexus memory consolidate --dry-run --type all
    """
    nx = get_default_filesystem()
    try:
        if dry_run:
            console.print("[yellow]Dry run mode - no changes will be made[/yellow]")
            # TODO: Implement dry-run mode
            nx.close()
            return

        report = nx.memory.consolidate(  # type: ignore[attr-defined]
            memory_type=memory_type,
            importance_threshold=threshold,
        )

        if output_json:
            click.echo(json.dumps(report, indent=2))
        else:
            console.print("\n[bold]Consolidation Report[/bold]")
            console.print(f"Memories consolidated: {report['memories_consolidated']}")
            console.print(f"Consolidations created: {report['consolidations_created']}")
            console.print(f"Space saved: ~{report['space_saved']} memories")

        nx.close()
    except Exception as e:
        nx.close()
        handle_error(e)


@memory.command("process-relearning")
@click.option("--limit", type=int, default=10, help="Maximum trajectories to process")
@click.option("--min-priority", type=int, default=0, help="Minimum priority (1-10)")
@click.option("--json", "output_json", is_flag=True, help="Output as JSON")
def process_relearning_cmd(
    limit: int,
    min_priority: int,
    output_json: bool,
) -> None:
    """Process trajectories flagged for re-learning.

    This command processes trajectories that have received feedback after completion,
    re-reflecting on them with updated scores to improve agent learning.

    Useful for:
    - Processing production feedback
    - Incorporating human ratings
    - Updating learning from A/B test results
    - Running as a scheduled job (cron/systemd)

    \b
    Examples:
        # Process up to 10 trajectories
        nexus memory process-relearning

        # Process high-priority items only
        nexus memory process-relearning --min-priority 7 --limit 5

        # Run as cron job (every 5 minutes)
        */5 * * * * nexus memory process-relearning --limit 10

        # JSON output for automation
        nexus memory process-relearning --json
    """
    nx = get_default_filesystem()
    try:
        # Note: min_priority filtering is not yet supported in the Memory API
        if min_priority > 0 and not output_json:
            console.print(
                "[yellow]Note: Priority filtering is not yet supported. Processing all pending trajectories.[/yellow]\n"
            )

        if not output_json:
            console.print(f"\n[bold]Processing re-learning queue (limit: {limit})...[/bold]\n")

        # Use Memory API (works in both local and remote modes)
        results = nx.memory.process_relearning(limit=limit)  # type: ignore[attr-defined]

        if not results:
            if output_json:
                click.echo(json.dumps({"processed": 0, "results": []}))
            else:
                console.print("[yellow]No trajectories need re-learning[/yellow]")
            nx.close()
            return

        # Output results
        if output_json:
            click.echo(json.dumps({"processed": len(results), "results": results}, indent=2))
        else:
            successful = sum(1 for r in results if r.get("success"))
            failed = len(results) - successful

            console.print("\n[bold]Re-learning Complete[/bold]")
            console.print(f"  [green]✓[/green] Successful: {successful}")
            if failed > 0:
                console.print(f"  [red]✗[/red] Failed: {failed}")

            if failed > 0:
                console.print("\n[bold]Failed Trajectories:[/bold]")
                for r in results:
                    if not r.get("success"):
                        console.print(
                            f"  [red]✗[/red] {r['trajectory_id']}: {r.get('error', 'Unknown error')}"
                        )

        nx.close()
    except Exception as e:
        nx.close()
        handle_error(e)
