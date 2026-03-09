"""Memory management CLI commands (v0.4.0+)."""

import json
import re
from datetime import timedelta
from typing import Any

import click
from rich.console import Console
from rich.table import Table

from nexus.bricks.memory.memory_provider import get_memory_api
from nexus.cli.utils import (
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
@click.option("--scope", default="user", help="Memory scope (agent/user/zone/global)")
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
        memory_id = get_memory_api(nx).store(
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
@click.option(
    "--after", default=None, help="Filter memories created after this time (ISO-8601). #1023"
)
@click.option(
    "--before", default=None, help="Filter memories created before this time (ISO-8601). #1023"
)
@click.option(
    "--during",
    default=None,
    help="Filter memories during this period (e.g., '2025', '2025-01'). #1023",
)
@click.option("--limit", type=int, default=100, help="Maximum number of results")
@click.option("--json", "output_json", is_flag=True, help="Output as JSON")
def query(
    user_id: str | None,
    agent_id: str | None,
    scope: str | None,
    memory_type: str | None,
    state: str,
    after: str | None,
    before: str | None,
    during: str | None,
    limit: int,
    output_json: bool,
) -> None:
    """Query memories by filters.

    \b
    Examples:
        nexus memory query --scope user --type preference
        nexus memory query --agent-id agent1 --limit 10
        nexus memory query --state inactive
        nexus memory query --during "2025-01"  # Memories from January 2025
        nexus memory query --after "2025-01-01T00:00:00Z"  # After this date
        nexus memory query --json
    """
    nx = get_default_filesystem()

    try:
        # Note: user_id and agent_id filtering not supported in remote mode yet
        results = get_memory_api(nx).query(
            scope=scope,
            memory_type=memory_type,
            state=state,
            after=after,
            before=before,
            during=during,
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
@click.option(
    "--after", default=None, help="Filter memories created after this time (ISO-8601). #1023"
)
@click.option(
    "--before", default=None, help="Filter memories created before this time (ISO-8601). #1023"
)
@click.option(
    "--during",
    default=None,
    help="Filter memories during this period (e.g., '2025', '2025-01'). #1023",
)
@click.option("--json", "output_json", is_flag=True, help="Output as JSON")
def search(
    query_text: str,
    scope: str | None,
    memory_type: str | None,
    limit: int,
    search_mode: str,
    embedding_provider: str | None,
    after: str | None,
    before: str | None,
    during: str | None,
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

        # Search with temporal filters (#1023)
        nexus memory search "project updates" --during "2025-01"
        nexus memory search "API changes" --after "2025-01-01"

        # JSON output
        nexus memory search "database" --json
    """
    nx = get_default_filesystem()

    try:
        # Create embedding provider if specified
        # Removed: txtai handles this (Issue #2663)
        # embeddings module was deleted; embedding provider is no longer created here.
        embedding_provider_obj = None
        if embedding_provider and search_mode in ("semantic", "hybrid"):
            click.echo(
                "Warning: Embedding provider creation is no longer supported here. "
                "Use txtai-based search instead (Issue #2663).",
                err=True,
            )
            search_mode = "keyword"

        results = get_memory_api(nx).search(
            query=query_text,
            scope=scope,
            memory_type=memory_type,
            limit=limit,
            search_mode=search_mode,
            embedding_provider=embedding_provider_obj,
            after=after,
            before=before,
            during=during,
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
@click.option(
    "--after", default=None, help="Filter memories created after this time (ISO-8601). #1023"
)
@click.option(
    "--before", default=None, help="Filter memories created before this time (ISO-8601). #1023"
)
@click.option(
    "--during",
    default=None,
    help="Filter memories during this period (e.g., '2025', '2025-01'). #1023",
)
@click.option("--limit", type=int, default=100, help="Maximum number of results")
@click.option("--json", "output_json", is_flag=True, help="Output as JSON")
def list(
    scope: str | None,
    memory_type: str | None,
    namespace: str | None,
    namespace_prefix: str | None,
    state: str,
    after: str | None,
    before: str | None,
    during: str | None,
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
        nexus memory list --during "2025-01"  # List January 2025 memories
        nexus memory list --after "2025-01-01"  # List recent memories
        nexus memory list --json
    """
    nx = get_default_filesystem()

    try:
        results = get_memory_api(nx).list(
            scope=scope,
            memory_type=memory_type,
            namespace=namespace,
            namespace_prefix=namespace_prefix,
            state=state,
            after=after,
            before=before,
            during=during,
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
        result = get_memory_api(nx).get(memory_id)
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
        result = get_memory_api(nx).retrieve(path=path)
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
        if get_memory_api(nx).delete(memory_id):
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
        if get_memory_api(nx).approve(memory_id):
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
        if get_memory_api(nx).deactivate(memory_id):
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
        result = get_memory_api(nx).approve_batch(list(memory_ids))
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
        result = get_memory_api(nx).deactivate_batch(list(memory_ids))
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
        result = get_memory_api(nx).delete_batch(list(memory_ids))
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
    remote_url: str | None,
    remote_api_key: str | None,
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
        nx: Any = get_filesystem(remote_url, remote_api_key)

        # v0.5.0: Parse TTL string to timedelta
        ttl_delta = None
        if ttl:
            ttl_delta = _parse_ttl(ttl)

        result = nx._workspace_rpc_service.register_memory(
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
    remote_url: str | None,
    remote_api_key: str | None,
) -> None:
    """List all registered memories.

    Examples:
        nexus memory list-registered
    """
    try:
        nx: Any = get_filesystem(remote_url, remote_api_key)

        memories = nx._workspace_rpc_service.list_registered_memories()

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
    remote_url: str | None,
    remote_api_key: str | None,
) -> None:
    """Unregister a memory (does NOT delete files).

    This removes the memory from the registry but keeps all files intact.

    Examples:
        nexus memory unregister /my-memory
        nexus memory unregister /my-memory --yes
    """
    try:
        nx: Any = get_filesystem(remote_url, remote_api_key)

        # Get memory info first
        info = nx._workspace_rpc_service.get_memory_info(path)
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
        result = nx._workspace_rpc_service.unregister_memory(path)

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
    remote_url: str | None,
    remote_api_key: str | None,
) -> None:
    """Show information about a registered memory.

    Examples:
        nexus memory info /my-memory
    """
    try:
        nx: Any = get_filesystem(remote_url, remote_api_key)

        info = nx._workspace_rpc_service.get_memory_info(path)

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
