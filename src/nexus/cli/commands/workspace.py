"""Workspace snapshot and versioning commands."""

from __future__ import annotations

import click
from rich.console import Console
from rich.table import Table

from nexus.cli.utils import BackendConfig, get_filesystem, handle_error

console = Console()


@click.group(name="workspace")
def workspace_group() -> None:
    """Workspace snapshot and version control commands.

    Manage workspace snapshots for time-travel debugging and rollback.

    Examples:
        # Create a snapshot
        nexus workspace snapshot --agent agent1 --description "Before refactor"

        # View snapshot history
        nexus workspace log --agent agent1

        # Restore to previous snapshot
        nexus workspace restore --agent agent1 --snapshot 5

        # Compare two snapshots
        nexus workspace diff --agent agent1 --snapshot1 5 --snapshot2 10
    """
    pass


@workspace_group.command(name="snapshot")
@click.option("--agent", "-a", required=True, help="Agent identifier")
@click.option("--tenant", "-t", default=None, help="Tenant identifier (optional)")
@click.option("--description", "-d", default=None, help="Snapshot description")
@click.option("--tag", "-g", multiple=True, help="Tags for categorization (can specify multiple)")
@click.option("--data-dir", default=None, help="Data directory for local backend")
@click.option("--config", default=None, help="Path to configuration file")
def snapshot_cmd(
    agent: str,
    tenant: str | None,
    description: str | None,
    tag: tuple[str, ...],
    data_dir: str | None,
    config: str | None,
) -> None:
    """Create a snapshot of agent's workspace.

    Captures the complete state of the workspace for later restore.

    Examples:
        nexus workspace snapshot --agent agent1 --description "Before major refactor"
        nexus workspace snapshot --agent agent1 --tag experiment --tag v1.0
    """
    try:
        backend_config = BackendConfig(data_dir=data_dir or "./nexus-data", config_path=config)
        nx = get_filesystem(backend_config)
        # Override agent_id and tenant_id if provided
        nx.agent_id = agent
        nx.tenant_id = tenant
        tags = list(tag) if tag else None

        with console.status(f"[bold cyan]Creating snapshot for agent '{agent}'..."):
            result = nx.workspace_snapshot(
                agent_id=agent,
                description=description,
                tags=tags,
            )

        console.print(
            f"[green]✓[/green] Created snapshot #{result['snapshot_number']} "
            f"({result['file_count']} files, {_format_size(result['total_size_bytes'])})"
        )
        console.print(f"  Snapshot ID: {result['snapshot_id']}")
        console.print(f"  Manifest hash: {result['manifest_hash'][:16]}...")
        if description:
            console.print(f"  Description: {description}")
        if tags:
            console.print(f"  Tags: {', '.join(tags)}")

        nx.close()

    except Exception as e:
        handle_error(e)


@workspace_group.command(name="log")
@click.option("--agent", "-a", required=True, help="Agent identifier")
@click.option("--tenant", "-t", default=None, help="Tenant identifier (optional)")
@click.option("--limit", "-n", default=20, help="Maximum number of snapshots to show")
@click.option("--data-dir", default=None, help="Data directory for local backend")
@click.option("--config", default=None, help="Path to configuration file")
def log_cmd(
    agent: str,
    tenant: str | None,
    limit: int,
    data_dir: str | None,
    config: str | None,
) -> None:
    """Show snapshot history for agent's workspace.

    Lists all snapshots in reverse chronological order.

    Examples:
        nexus workspace log --agent agent1
        nexus workspace log --agent agent1 --limit 50
    """
    try:
        backend_config = BackendConfig(data_dir=data_dir or "./nexus-data", config_path=config)
        nx = get_filesystem(backend_config)
        nx.agent_id = agent
        nx.tenant_id = tenant

        snapshots = nx.workspace_log(agent_id=agent, limit=limit)

        if not snapshots:
            console.print(f"[yellow]No snapshots found for agent '{agent}'[/yellow]")
            nx.close()
            return

        # Create table
        table = Table(title=f"Workspace Snapshots for {agent}")
        table.add_column("#", justify="right", style="cyan")
        table.add_column("Created", style="green")
        table.add_column("Files", justify="right")
        table.add_column("Size", justify="right")
        table.add_column("Description")
        table.add_column("Tags", style="dim")

        for snap in snapshots:
            created_at = snap["created_at"].strftime("%Y-%m-%d %H:%M:%S")
            tags_str = ", ".join(snap["tags"]) if snap["tags"] else ""

            table.add_row(
                str(snap["snapshot_number"]),
                created_at,
                str(snap["file_count"]),
                _format_size(snap["total_size_bytes"]),
                snap["description"] or "",
                tags_str,
            )

        console.print(table)
        console.print(f"\n[dim]Showing {len(snapshots)} snapshot(s)[/dim]")

        nx.close()

    except Exception as e:
        handle_error(e)


@workspace_group.command(name="restore")
@click.option("--agent", "-a", required=True, help="Agent identifier")
@click.option("--tenant", "-t", default=None, help="Tenant identifier (optional)")
@click.option("--snapshot", "-s", required=True, type=int, help="Snapshot number to restore")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompt")
@click.option("--data-dir", default=None, help="Data directory for local backend")
@click.option("--config", default=None, help="Path to configuration file")
def restore_cmd(
    agent: str,
    tenant: str | None,
    snapshot: int,
    yes: bool,
    data_dir: str | None,
    config: str | None,
) -> None:
    """Restore workspace to a previous snapshot.

    WARNING: This will overwrite current workspace state!

    Examples:
        nexus workspace restore --agent agent1 --snapshot 5
        nexus workspace restore --agent agent1 --snapshot 10 --yes
    """
    try:
        backend_config = BackendConfig(data_dir=data_dir or "./nexus-data", config_path=config)
        nx = get_filesystem(backend_config)
        nx.agent_id = agent
        nx.tenant_id = tenant

        # Get snapshot info
        snapshots = nx.workspace_log(agent_id=agent, limit=1000)
        snap_info = None
        for s in snapshots:
            if s["snapshot_number"] == snapshot:
                snap_info = s
                break

        if not snap_info:
            console.print(f"[red]✗[/red] Snapshot #{snapshot} not found")
            nx.close()
            return

        # Confirm
        if not yes:
            console.print(f"[yellow]⚠[/yellow]  About to restore workspace to snapshot #{snapshot}")
            console.print(f"    Created: {snap_info['created_at'].strftime('%Y-%m-%d %H:%M:%S')}")
            console.print(f"    Files: {snap_info['file_count']}")
            if snap_info["description"]:
                console.print(f"    Description: {snap_info['description']}")
            console.print("\n[red]This will overwrite the current workspace state![/red]")

            if not click.confirm("Continue?"):
                console.print("[yellow]Cancelled[/yellow]")
                nx.close()
                return

        # Perform restore
        with console.status(f"[bold cyan]Restoring snapshot #{snapshot}..."):
            result = nx.workspace_restore(snapshot_number=snapshot, agent_id=agent)

        console.print(
            f"[green]✓[/green] Restored snapshot #{snapshot} "
            f"({result['files_restored']} files restored, "
            f"{result['files_deleted']} files deleted)"
        )

        nx.close()

    except Exception as e:
        handle_error(e)


@workspace_group.command(name="diff")
@click.option("--agent", "-a", required=True, help="Agent identifier")
@click.option("--tenant", "-t", default=None, help="Tenant identifier (optional)")
@click.option("--snapshot1", "-s1", required=True, type=int, help="First snapshot number")
@click.option("--snapshot2", "-s2", required=True, type=int, help="Second snapshot number")
@click.option("--data-dir", default=None, help="Data directory for local backend")
@click.option("--config", default=None, help="Path to configuration file")
def diff_cmd(
    agent: str,
    tenant: str | None,
    snapshot1: int,
    snapshot2: int,
    data_dir: str | None,
    config: str | None,
) -> None:
    """Compare two workspace snapshots.

    Shows files added, removed, and modified between snapshots.

    Examples:
        nexus workspace diff --agent agent1 --snapshot1 5 --snapshot2 10
    """
    try:
        backend_config = BackendConfig(data_dir=data_dir or "./nexus-data", config_path=config)
        nx = get_filesystem(backend_config)
        nx.agent_id = agent
        nx.tenant_id = tenant

        with console.status("[bold cyan]Computing diff between snapshots..."):
            diff = nx.workspace_diff(snapshot_1=snapshot1, snapshot_2=snapshot2, agent_id=agent)

        # Display header
        console.print(f"\n[bold]Diff: Snapshot #{snapshot1} → Snapshot #{snapshot2}[/bold]")
        console.print(
            f"[dim]{diff['snapshot_1']['created_at'].strftime('%Y-%m-%d %H:%M:%S')} → "
            f"{diff['snapshot_2']['created_at'].strftime('%Y-%m-%d %H:%M:%S')}[/dim]\n"
        )

        # Added files
        if diff["added"]:
            console.print(f"[green]Added ({len(diff['added'])} files):[/green]")
            for file in diff["added"][:20]:  # Limit to 20
                console.print(f"  + {file['path']} ({_format_size(file['size'])})")
            if len(diff["added"]) > 20:
                console.print(f"  [dim]... and {len(diff['added']) - 20} more[/dim]")
            console.print()

        # Removed files
        if diff["removed"]:
            console.print(f"[red]Removed ({len(diff['removed'])} files):[/red]")
            for file in diff["removed"][:20]:
                console.print(f"  - {file['path']} ({_format_size(file['size'])})")
            if len(diff["removed"]) > 20:
                console.print(f"  [dim]... and {len(diff['removed']) - 20} more[/dim]")
            console.print()

        # Modified files
        if diff["modified"]:
            console.print(f"[yellow]Modified ({len(diff['modified'])} files):[/yellow]")
            for file in diff["modified"][:20]:
                size_change = file["new_size"] - file["old_size"]
                size_str = f"{_format_size(file['old_size'])} → {_format_size(file['new_size'])}"
                if size_change > 0:
                    size_str += f" (+{_format_size(size_change)})"
                elif size_change < 0:
                    size_str += f" ({_format_size(size_change)})"
                console.print(f"  ~ {file['path']} ({size_str})")
            if len(diff["modified"]) > 20:
                console.print(f"  [dim]... and {len(diff['modified']) - 20} more[/dim]")
            console.print()

        # Summary
        console.print(
            f"[dim]Summary: "
            f"{len(diff['added'])} added, "
            f"{len(diff['removed'])} removed, "
            f"{len(diff['modified'])} modified, "
            f"{diff['unchanged']} unchanged[/dim]"
        )

        nx.close()

    except Exception as e:
        handle_error(e)


def _format_size(size_bytes: int) -> str:
    """Format size in bytes to human-readable format."""
    size = float(size_bytes)
    for unit in ["B", "KB", "MB", "GB"]:
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def register_commands(cli: click.Group) -> None:
    """Register workspace commands to CLI."""
    cli.add_command(workspace_group)
