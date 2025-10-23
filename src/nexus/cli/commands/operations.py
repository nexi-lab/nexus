"""Operation Log Commands - Audit trail and undo capability.

CAS-backed operation logging for all filesystem operations.
Provides audit trail, undo capability, and debugging support.
"""

from __future__ import annotations

from typing import Any

import click
from rich.table import Table

from nexus.cli.utils import (
    BackendConfig,
    add_backend_options,
    console,
    get_filesystem,
    handle_error,
)


def register_commands(cli: click.Group) -> None:
    """Register operation log commands with the CLI.

    Args:
        cli: The main CLI group to register commands with
    """
    cli.add_command(ops_group)
    cli.add_command(undo)


@click.group(name="ops")
def ops_group() -> None:
    """Operation Log - View operation history.

    Provides audit trail for all filesystem operations.

    Examples:
        nexus ops log
        nexus ops log --agent my-agent --limit 50
        nexus ops log --type write --path /workspace/data.txt
    """
    pass


@ops_group.command(name="log")
@click.option("--agent", "-a", help="Filter by agent ID")
@click.option("--tenant", "-t", help="Filter by tenant ID")
@click.option("--type", "op_type", help="Filter by operation type (write, delete, rename)")
@click.option("--path", "-p", help="Filter by path")
@click.option("--status", "-s", type=click.Choice(["success", "failure"]), help="Filter by status")
@click.option("--limit", "-l", type=int, default=50, help="Maximum number of operations to show")
@add_backend_options
def ops_log(
    agent: str | None,
    tenant: str | None,
    op_type: str | None,
    path: str | None,
    status: str | None,
    limit: int,
    backend_config: BackendConfig,
) -> None:
    """Show operation log with optional filters.

    Displays history of filesystem operations for audit and debugging.

    Examples:
        nexus ops log
        nexus ops log --agent my-agent --limit 100
        nexus ops log --type write --path /workspace/data.txt
        nexus ops log --status failure
    """
    try:
        nx = get_filesystem(backend_config)

        # Access operation logger through metadata store
        from nexus.storage.operation_logger import OperationLogger

        with nx.metadata.SessionLocal() as session:  # type: ignore[attr-defined]
            logger = OperationLogger(session)

            # List operations with filters
            operations = logger.list_operations(
                tenant_id=tenant,
                agent_id=agent,
                operation_type=op_type,
                path=path,
                status=status,
                limit=limit,
            )

            if not operations:
                console.print("[yellow]No operations found[/yellow]")
                nx.close()
                return

            # Display table
            table = Table(title="Operation Log")
            table.add_column("Time", style="cyan")
            table.add_column("Type", style="yellow")
            table.add_column("Path", style="green")
            table.add_column("Agent", style="blue")
            table.add_column("Status")
            table.add_column("Op ID", style="dim")

            for op in operations:
                status_display = "[green]✓[/green]" if op.status == "success" else "[red]✗[/red]"
                created_at = op.created_at.strftime("%Y-%m-%d %H:%M:%S")

                # Truncate operation ID for display
                op_id_short = op.operation_id[:8]

                # For rename operations, show both paths
                path_display = op.path
                if op.operation_type == "rename" and op.new_path:
                    path_display = f"{op.path} → {op.new_path}"

                table.add_row(
                    created_at,
                    op.operation_type,
                    path_display,
                    op.agent_id or "-",
                    status_display,
                    op_id_short,
                )

            console.print(table)
            console.print(f"\n[dim]Showing {len(operations)} operations[/dim]")

        nx.close()

    except Exception as e:
        handle_error(e)


@click.command(name="undo")
@click.option("--agent", "-a", help="Filter by agent ID (undo last operation by this agent)")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation")
@add_backend_options
def undo(agent: str | None, yes: bool, backend_config: BackendConfig) -> None:
    """Undo the last successful operation.

    Reverts the most recent filesystem operation.

    Examples:
        nexus undo
        nexus undo --agent my-agent
        nexus undo --yes
    """
    try:
        nx = get_filesystem(backend_config)

        from nexus.storage.operation_logger import OperationLogger

        with nx.metadata.SessionLocal() as session:  # type: ignore[attr-defined]
            logger = OperationLogger(session)

            # Get last successful operation
            last_op = logger.get_last_operation(
                agent_id=agent,
                status="success",
            )

            if not last_op:
                console.print("[yellow]No operations to undo[/yellow]")
                nx.close()
                return

            # Show operation details
            console.print("\n[bold]Last Operation:[/bold]")
            console.print(f"  Type: {last_op.operation_type}")
            console.print(f"  Path: {last_op.path}")
            if last_op.new_path:
                console.print(f"  New Path: {last_op.new_path}")
            console.print(f"  Time: {last_op.created_at.strftime('%Y-%m-%d %H:%M:%S')}")
            console.print(f"  Agent: {last_op.agent_id or 'N/A'}")

            if not yes:
                confirmed = click.confirm("\nUndo this operation?")
                if not confirmed:
                    console.print("Cancelled")
                    nx.close()
                    return

            # Perform undo based on operation type
            _undo_operation(nx, logger, last_op)

            console.print(f"\n[green]✓[/green] Undid operation: {last_op.operation_type}")

        nx.close()

    except Exception as e:
        handle_error(e)


def _undo_operation(nx: Any, logger: Any, operation: Any) -> None:
    """Undo a specific operation.

    Args:
        nx: NexusFS instance
        logger: OperationLogger instance
        operation: OperationLogModel to undo
    """
    if operation.operation_type == "write":
        # Restore previous version if exists, otherwise delete
        if operation.snapshot_hash:
            # Read old content from CAS and restore
            old_content = nx.backend.read_content(operation.snapshot_hash)
            nx.write(operation.path, old_content)
            console.print(f"  Restored previous version of {operation.path}")
        else:
            # File didn't exist before, so delete it
            nx.delete(operation.path)
            console.print(f"  Deleted {operation.path} (was newly created)")

    elif operation.operation_type == "delete":
        # Restore deleted file from snapshot
        if operation.snapshot_hash:
            content = nx.backend.read_content(operation.snapshot_hash)
            nx.write(operation.path, content)

            # Restore metadata if available
            if operation.metadata_snapshot:
                metadata = logger.get_metadata_snapshot(operation)
                if metadata and (
                    metadata.get("owner") or metadata.get("group") or metadata.get("mode")
                ):
                    # Restore permissions
                    from nexus.core.permissions import OperationContext

                    context = OperationContext(
                        user=nx.agent_id or "system",
                        groups=[],
                        is_admin=True,
                        is_system=True,
                    )
                    if metadata.get("owner"):
                        nx.chown(operation.path, metadata["owner"], context=context)
                    if metadata.get("group"):
                        nx.chgrp(operation.path, metadata["group"], context=context)
                    if metadata.get("mode") is not None:
                        nx.chmod(operation.path, metadata["mode"], context=context)

            console.print(f"  Restored deleted file: {operation.path}")
        else:
            console.print(
                f"  [yellow]Warning: Cannot restore {operation.path} (no snapshot)[/yellow]"
            )

    elif operation.operation_type == "rename":
        # Rename back to original path
        if operation.new_path:
            # Check if new path still exists
            if nx.exists(operation.new_path):
                nx.rename(operation.new_path, operation.path)
                console.print(f"  Renamed {operation.new_path} back to {operation.path}")
            else:
                console.print(
                    f"  [yellow]Warning: Cannot undo rename - {operation.new_path} no longer exists[/yellow]"
                )
        else:
            console.print("  [yellow]Warning: Cannot undo rename - missing new_path[/yellow]")

    else:
        console.print(
            f"  [yellow]Warning: Undo not implemented for {operation.operation_type}[/yellow]"
        )
