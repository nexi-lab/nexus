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

    Manage workspace registration and snapshots for time-travel debugging and rollback.
    Workspaces use explicit path-based registration with ReBAC permissions.

    Examples:
        # Register a workspace
        nexus workspace register /my-workspace --name main --description "My workspace"

        # List all workspaces
        nexus workspace list

        # Create a snapshot
        nexus workspace snapshot /my-workspace --description "Before refactor"

        # View snapshot history
        nexus workspace log /my-workspace

        # Restore to previous snapshot
        nexus workspace restore /my-workspace --snapshot 5

        # Compare snapshots
        nexus workspace diff /my-workspace --snapshot1 3 --snapshot2 5

        # Unregister a workspace
        nexus workspace unregister /my-workspace
    """
    pass


@workspace_group.command(name="register")
@click.argument("path", type=str)
@click.option("--name", "-n", default=None, help="Friendly name for workspace")
@click.option("--description", "-d", default="", help="Description of workspace")
@click.option("--created-by", default=None, help="User/agent who created it")
@click.option("--data-dir", default=None, help="Data directory for local backend")
@click.option("--config", default=None, help="Path to configuration file")
def register_cmd(
    path: str,
    name: str | None,
    description: str,
    created_by: str | None,
    data_dir: str | None,
    config: str | None,
) -> None:
    """Register a directory as a workspace.

    Workspaces support snapshots, versioning, and rollback functionality.

    Examples:
        nexus workspace register /my-workspace --name main
        nexus workspace register /projects/alpha --name alpha --description "Alpha project workspace"
    """
    try:
        backend_config = BackendConfig(data_dir=data_dir or "./nexus-data", config_path=config)
        nx = get_filesystem(backend_config)

        result = nx.register_workspace(
            path=path,
            name=name,
            description=description,
            created_by=created_by,
        )

        console.print(f"[green]✓[/green] Registered workspace: {result['path']}")
        if result["name"]:
            console.print(f"  Name: {result['name']}")
        if result["description"]:
            console.print(f"  Description: {result['description']}")
        if result["created_by"]:
            console.print(f"  Created by: {result['created_by']}")

        nx.close()

    except Exception as e:
        handle_error(e)


@workspace_group.command(name="list")
@click.option("--data-dir", default=None, help="Data directory for local backend")
@click.option("--config", default=None, help="Path to configuration file")
def list_cmd(
    data_dir: str | None,
    config: str | None,
) -> None:
    """List all registered workspaces.

    Examples:
        nexus workspace list
    """
    try:
        backend_config = BackendConfig(data_dir=data_dir or "./nexus-data", config_path=config)
        nx = get_filesystem(backend_config)

        workspaces = nx.list_workspaces()

        if not workspaces:
            console.print("[yellow]No workspaces registered[/yellow]")
            nx.close()
            return

        # Create table
        table = Table(title="Registered Workspaces")
        table.add_column("Path", style="cyan")
        table.add_column("Name", style="green")
        table.add_column("Description")
        table.add_column("Created By", style="dim")

        for ws in workspaces:
            table.add_row(
                ws["path"],
                ws["name"] or "",
                ws["description"] or "",
                ws["created_by"] or "",
            )

        console.print(table)
        console.print(f"\n[dim]{len(workspaces)} workspace(s) registered[/dim]")

        nx.close()

    except Exception as e:
        handle_error(e)


@workspace_group.command(name="unregister")
@click.argument("path", type=str)
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompt")
@click.option("--data-dir", default=None, help="Data directory for local backend")
@click.option("--config", default=None, help="Path to configuration file")
def unregister_cmd(
    path: str,
    yes: bool,
    data_dir: str | None,
    config: str | None,
) -> None:
    """Unregister a workspace (does NOT delete files).

    This removes the workspace from the registry but keeps all files intact.

    Examples:
        nexus workspace unregister /my-workspace
        nexus workspace unregister /my-workspace --yes
    """
    try:
        backend_config = BackendConfig(data_dir=data_dir or "./nexus-data", config_path=config)
        nx = get_filesystem(backend_config)

        # Get workspace info first
        info = nx.get_workspace_info(path)
        if not info:
            console.print(f"[red]✗[/red] Workspace not registered: {path}")
            nx.close()
            return

        # Confirm
        if not yes:
            console.print(f"[yellow]⚠[/yellow]  About to unregister workspace: {path}")
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
        result = nx.unregister_workspace(path)

        if result:
            console.print(f"[green]✓[/green] Unregistered workspace: {path}")
        else:
            console.print(f"[red]✗[/red] Failed to unregister workspace: {path}")

        nx.close()

    except Exception as e:
        handle_error(e)


@workspace_group.command(name="info")
@click.argument("path", type=str)
@click.option("--data-dir", default=None, help="Data directory for local backend")
@click.option("--config", default=None, help="Path to configuration file")
def info_cmd(
    path: str,
    data_dir: str | None,
    config: str | None,
) -> None:
    """Show information about a registered workspace.

    Examples:
        nexus workspace info /my-workspace
    """
    try:
        backend_config = BackendConfig(data_dir=data_dir or "./nexus-data", config_path=config)
        nx = get_filesystem(backend_config)

        info = nx.get_workspace_info(path)

        if not info:
            console.print(f"[red]✗[/red] Workspace not registered: {path}")
            nx.close()
            return

        console.print(f"[bold]Workspace: {info['path']}[/bold]\n")
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


@workspace_group.command(name="snapshot")
@click.argument("path", type=str)
@click.option("--description", "-d", default=None, help="Snapshot description")
@click.option("--tag", "-g", multiple=True, help="Tags for categorization (can specify multiple)")
@click.option("--data-dir", default=None, help="Data directory for local backend")
@click.option("--config", default=None, help="Path to configuration file")
def snapshot_cmd(
    path: str,
    description: str | None,
    tag: tuple[str, ...],
    data_dir: str | None,
    config: str | None,
) -> None:
    """Create a snapshot of a workspace.

    Captures the complete state of the workspace for later restore.

    Examples:
        nexus workspace snapshot /my-workspace --description "Before major refactor"
        nexus workspace snapshot /my-workspace --tag experiment --tag v1.0
    """
    try:
        backend_config = BackendConfig(data_dir=data_dir or "./nexus-data", config_path=config)
        nx = get_filesystem(backend_config)
        tags = list(tag) if tag else None

        with console.status(f"[bold cyan]Creating snapshot for workspace '{path}'..."):
            result = nx.workspace_snapshot(
                workspace_path=path,
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
@click.argument("path", type=str)
@click.option("--limit", "-n", default=20, help="Maximum number of snapshots to show")
@click.option("--data-dir", default=None, help="Data directory for local backend")
@click.option("--config", default=None, help="Path to configuration file")
def log_cmd(
    path: str,
    limit: int,
    data_dir: str | None,
    config: str | None,
) -> None:
    """Show snapshot history for a workspace.

    Lists all snapshots in reverse chronological order.

    Examples:
        nexus workspace log /my-workspace
        nexus workspace log /my-workspace --limit 50
    """
    try:
        backend_config = BackendConfig(data_dir=data_dir or "./nexus-data", config_path=config)
        nx = get_filesystem(backend_config)

        snapshots = nx.workspace_log(workspace_path=path, limit=limit)

        if not snapshots:
            console.print(f"[yellow]No snapshots found for workspace '{path}'[/yellow]")
            nx.close()
            return

        # Create table
        table = Table(title=f"Workspace Snapshots for {path}")
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
@click.argument("path", type=str)
@click.option("--snapshot", "-s", required=True, type=int, help="Snapshot number to restore")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompt")
@click.option("--data-dir", default=None, help="Data directory for local backend")
@click.option("--config", default=None, help="Path to configuration file")
def restore_cmd(
    path: str,
    snapshot: int,
    yes: bool,
    data_dir: str | None,
    config: str | None,
) -> None:
    """Restore workspace to a previous snapshot.

    WARNING: This will overwrite current workspace state!

    Examples:
        nexus workspace restore /my-workspace --snapshot 5
        nexus workspace restore /my-workspace --snapshot 10 --yes
    """
    try:
        backend_config = BackendConfig(data_dir=data_dir or "./nexus-data", config_path=config)
        nx = get_filesystem(backend_config)

        # Get snapshot info
        snapshots = nx.workspace_log(workspace_path=path, limit=1000)
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
            result = nx.workspace_restore(snapshot_number=snapshot, workspace_path=path)

        console.print(
            f"[green]✓[/green] Restored snapshot #{snapshot} "
            f"({result['files_restored']} files restored, "
            f"{result['files_deleted']} files deleted)"
        )

        nx.close()

    except Exception as e:
        handle_error(e)


@workspace_group.command(name="diff")
@click.argument("path", type=str)
@click.option("--snapshot1", "-s1", required=True, type=int, help="First snapshot number")
@click.option("--snapshot2", "-s2", required=True, type=int, help="Second snapshot number")
@click.option("--data-dir", default=None, help="Data directory for local backend")
@click.option("--config", default=None, help="Path to configuration file")
def diff_cmd(
    path: str,
    snapshot1: int,
    snapshot2: int,
    data_dir: str | None,
    config: str | None,
) -> None:
    """Compare two workspace snapshots.

    Shows files added, removed, and modified between snapshots.

    Examples:
        nexus workspace diff /my-workspace --snapshot1 5 --snapshot2 10
    """
    try:
        backend_config = BackendConfig(data_dir=data_dir or "./nexus-data", config_path=config)
        nx = get_filesystem(backend_config)

        with console.status("[bold cyan]Computing diff between snapshots..."):
            diff = nx.workspace_diff(
                snapshot_1=snapshot1, snapshot_2=snapshot2, workspace_path=path
            )

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
