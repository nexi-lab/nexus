"""Operation Log Commands - Audit trail and undo capability.

CAS-backed operation logging for all filesystem operations.
Provides audit trail, undo capability, and debugging support.
"""

import click
from rich.table import Table

from nexus.cli.utils import (
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


@ops_group.command(name="diff")
@click.argument("path", type=str)
@click.argument("operation_1", type=str)
@click.argument("operation_2", type=str)
@click.option("--show-content", is_flag=True, help="Show content diff (for text files)")
@add_backend_options
def ops_diff(
    path: str,
    operation_1: str,
    operation_2: str,
    show_content: bool,
    remote_url: str | None,
    remote_api_key: str | None,
) -> None:
    """Compare file state between two operation points.

    Time-travel debugging: Compare what a file looked like at two different
    operation points to understand how it changed.

    Examples:
        nexus ops diff /workspace/data.txt op_abc123 op_def456
        nexus ops diff /workspace/code.py op_abc123 op_def456 --show-content
    """
    try:
        nx = get_filesystem(remote_url, remote_api_key)

        time_travel = nx.service("time_travel")
        if time_travel is None:
            console.print("[red]Error:[/red] Time-travel is only supported with local NexusFS")
            nx.close()
            return

        diff_result = time_travel.diff_operations(path, operation_1, operation_2)
        nx.close()

        # Display results
        console.print(f"\n[bold cyan]Diff for {path}[/bold cyan]")
        console.print(f"[dim]Operation 1:[/dim] {operation_1}")
        console.print(f"[dim]Operation 2:[/dim] {operation_2}")
        console.print()

        state_1 = diff_result["operation_1"]
        state_2 = diff_result["operation_2"]

        if not state_1 and not state_2:
            console.print("[yellow]File did not exist at either operation point[/yellow]")
            return

        if not state_1:
            console.print("[green]File was created[/green]")
            console.print(f"  Size: {state_2['metadata']['size']:,} bytes")
            console.print(f"  Operation: {state_2['operation_id'][:8]}")
            console.print(f"  Time: {state_2['operation_time']}")
        elif not state_2:
            console.print("[red]File was deleted[/red]")
            console.print(f"  Previous size: {state_1['metadata']['size']:,} bytes")
            console.print(f"  Operation: {state_1['operation_id'][:8]}")
            console.print(f"  Time: {state_1['operation_time']}")
        else:
            # Both exist - show changes
            if diff_result["content_changed"]:
                console.print("[yellow]File content changed[/yellow]")
                console.print(
                    f"  Size: {state_1['metadata']['size']:,} → {state_2['metadata']['size']:,} bytes"
                )
                console.print(f"  Size diff: {diff_result['size_diff']:+,} bytes")
            else:
                console.print("[green]File content unchanged[/green]")

            console.print()
            console.print("[bold]Operation 1:[/bold]")
            console.print(f"  Op ID: {state_1['operation_id'][:8]}")
            console.print(f"  Time: {state_1['operation_time']}")
            console.print(f"  Size: {state_1['metadata']['size']:,} bytes")

            console.print()
            console.print("[bold]Operation 2:[/bold]")
            console.print(f"  Op ID: {state_2['operation_id'][:8]}")
            console.print(f"  Time: {state_2['operation_time']}")
            console.print(f"  Size: {state_2['metadata']['size']:,} bytes")

            # Show content diff if requested
            if show_content and diff_result["content_changed"]:
                console.print()
                console.print("[bold]Content Diff:[/bold]")

                try:
                    import difflib

                    text_1 = state_1["content"].decode("utf-8").splitlines(keepends=True)
                    text_2 = state_2["content"].decode("utf-8").splitlines(keepends=True)

                    diff_lines = difflib.unified_diff(
                        text_1,
                        text_2,
                        fromfile=f"Operation {operation_1[:8]}",
                        tofile=f"Operation {operation_2[:8]}",
                        lineterm="",
                    )

                    for line in diff_lines:
                        line = line.rstrip()
                        if line.startswith("+++") or line.startswith("---"):
                            console.print(f"[bold]{line}[/bold]")
                        elif line.startswith("+"):
                            console.print(f"[green]{line}[/green]")
                        elif line.startswith("-"):
                            console.print(f"[red]{line}[/red]")
                        elif line.startswith("@@"):
                            console.print(f"[cyan]{line}[/cyan]")
                        else:
                            console.print(f"[dim]{line}[/dim]")

                except UnicodeDecodeError:
                    console.print("[yellow]Binary file - content diff not available[/yellow]")

    except Exception as e:
        handle_error(e)


@ops_group.command(name="log")
@click.option("--agent", "-a", help="Filter by agent ID")
@click.option("--zone", "-z", help="Filter by zone ID")
@click.option("--type", "op_type", help="Filter by operation type (write, delete, rename)")
@click.option("--path", "-p", help="Filter by path")
@click.option("--status", "-s", type=click.Choice(["success", "failure"]), help="Filter by status")
@click.option("--limit", "-l", type=int, default=50, help="Maximum number of operations to show")
@add_backend_options
def ops_log(
    agent: str | None,
    zone: str | None,
    op_type: str | None,
    path: str | None,
    status: str | None,
    limit: int,
    remote_url: str | None,
    remote_api_key: str | None,
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
        nx = get_filesystem(remote_url, remote_api_key)

        ops_service = nx.service("operations")
        if ops_service is None:
            raise click.ClickException("Operation log requires a local NexusFS instance")

        # Support prefix matching: --path /demo/ matches all paths under /demo/
        # Use path_pattern (SQL LIKE with * → %) when path ends with /
        # or contains *, otherwise exact match.
        _path = path
        _path_pattern = None
        if path and (path.endswith("/") or "*" in path):
            _path = None
            _path_pattern = path.rstrip("/") + "/*" if path.endswith("/") else path

        operations = ops_service.list_operations(
            zone_id=zone,
            agent_id=agent,
            operation_type=op_type,
            path=_path,
            path_pattern=_path_pattern,
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
            status_display = "[green]✓[/green]" if op["status"] == "success" else "[red]✗[/red]"
            created_at = op["created_at"].strftime("%Y-%m-%d %H:%M:%S")

            # Truncate operation ID for display
            op_id_short = op["operation_id"][:8]

            # For rename operations, show both paths
            path_display = op["path"]
            if op["operation_type"] == "rename" and op["new_path"]:
                path_display = f"{op['path']} → {op['new_path']}"

            table.add_row(
                created_at,
                op["operation_type"],
                path_display,
                op["agent_id"] or "-",
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
def undo(agent: str | None, yes: bool, remote_url: str | None, remote_api_key: str | None) -> None:
    """Undo the last successful operation.

    Reverts the most recent filesystem operation.

    Examples:
        nexus undo
        nexus undo --agent my-agent
        nexus undo --yes
    """
    try:
        nx = get_filesystem(remote_url, remote_api_key)

        ops_service = nx.service("operations")
        if ops_service is None:
            raise click.ClickException("Undo requires a local NexusFS instance")

        last_op = ops_service.get_last_operation(
            agent_id=agent,
            status="success",
        )

        if not last_op:
            console.print("[yellow]No operations to undo[/yellow]")
            nx.close()
            return

        # Show operation details
        console.print("\n[bold]Last Operation:[/bold]")
        console.print(f"  Type: {last_op['operation_type']}")
        console.print(f"  Path: {last_op['path']}")
        if last_op["new_path"]:
            console.print(f"  New Path: {last_op['new_path']}")
        console.print(f"  Time: {last_op['created_at'].strftime('%Y-%m-%d %H:%M:%S')}")
        console.print(f"  Agent: {last_op['agent_id'] or 'N/A'}")

        if not yes:
            confirmed = click.confirm("\nUndo this operation?")
            if not confirmed:
                console.print("Cancelled")
                nx.close()
                return

        # Perform undo via service layer (S24: Operations Undo)
        result = ops_service.undo_by_id(last_op["operation_id"])

        if result["success"]:
            console.print(f"  {result['message']}")
        else:
            console.print(f"  [yellow]Warning: {result['message']}[/yellow]")

        console.print(f"\n[green]✓[/green] Undid operation: {last_op['operation_type']}")

        nx.close()

    except Exception as e:
        handle_error(e)
