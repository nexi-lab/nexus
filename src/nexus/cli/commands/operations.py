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
    import asyncio

    asyncio.run(
        _async_ops_diff(path, operation_1, operation_2, show_content, remote_url, remote_api_key)
    )


async def _async_ops_diff(
    path: str,
    operation_1: str,
    operation_2: str,
    show_content: bool,
    remote_url: str | None,
    remote_api_key: str | None,
) -> None:
    try:
        nx = await get_filesystem(remote_url, remote_api_key)

        time_travel = nx.service("time_travel")
        if time_travel is None:
            console.print(
                "[nexus.error]Error:[/nexus.error] Time-travel is only supported with local NexusFS"
            )
            nx.close()
            return

        diff_result = time_travel.diff_operations(path, operation_1, operation_2)
        nx.close()

        # Display results
        console.print(f"\n[bold nexus.value]Diff for {path}[/bold nexus.value]")
        console.print(f"[nexus.muted]Operation 1:[/nexus.muted] {operation_1}")
        console.print(f"[nexus.muted]Operation 2:[/nexus.muted] {operation_2}")
        console.print()

        state_1 = diff_result["operation_1"]
        state_2 = diff_result["operation_2"]

        if not state_1 and not state_2:
            console.print(
                "[nexus.warning]File did not exist at either operation point[/nexus.warning]"
            )
            return

        if not state_1:
            console.print("[nexus.success]File was created[/nexus.success]")
            console.print(f"  Size: {state_2['metadata']['size']:,} bytes")
            console.print(f"  Operation: {state_2['operation_id'][:8]}")
            console.print(f"  Time: {state_2['operation_time']}")
        elif not state_2:
            console.print("[nexus.error]File was deleted[/nexus.error]")
            console.print(f"  Previous size: {state_1['metadata']['size']:,} bytes")
            console.print(f"  Operation: {state_1['operation_id'][:8]}")
            console.print(f"  Time: {state_1['operation_time']}")
        else:
            # Both exist - show changes
            if diff_result["content_changed"]:
                console.print("[nexus.warning]File content changed[/nexus.warning]")
                console.print(
                    f"  Size: {state_1['metadata']['size']:,} → {state_2['metadata']['size']:,} bytes"
                )
                console.print(f"  Size diff: {diff_result['size_diff']:+,} bytes")
            else:
                console.print("[nexus.success]File content unchanged[/nexus.success]")

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
                            console.print(f"[nexus.success]{line}[/nexus.success]")
                        elif line.startswith("-"):
                            console.print(f"[nexus.error]{line}[/nexus.error]")
                        elif line.startswith("@@"):
                            console.print(f"[nexus.value]{line}[/nexus.value]")
                        else:
                            console.print(f"[nexus.muted]{line}[/nexus.muted]")

                except UnicodeDecodeError:
                    console.print(
                        "[nexus.warning]Binary file - content diff not available[/nexus.warning]"
                    )

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
    import asyncio

    asyncio.run(
        _async_ops_log(agent, zone, op_type, path, status, limit, remote_url, remote_api_key)
    )


async def _async_ops_log(
    agent: str | None,
    zone: str | None,
    op_type: str | None,
    path: str | None,
    status: str | None,
    limit: int,
    remote_url: str | None,
    remote_api_key: str | None,
) -> None:
    try:
        nx = await get_filesystem(remote_url, remote_api_key)

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
            console.print("[nexus.warning]No operations found[/nexus.warning]")
            nx.close()
            return

        # Display table
        table = Table(title="Operation Log")
        table.add_column("Time", style="nexus.value")
        table.add_column("Type", style="nexus.warning")
        table.add_column("Path", style="nexus.success")
        table.add_column("Agent", style="nexus.reference")
        table.add_column("Status")
        table.add_column("Op ID", style="nexus.muted")

        for op in operations:
            status_display = (
                "[nexus.success]✓[/nexus.success]"
                if op["status"] == "success"
                else "[nexus.error]✗[/nexus.error]"
            )
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
        console.print(f"\n[nexus.muted]Showing {len(operations)} operations[/nexus.muted]")

        nx.close()

    except Exception as e:
        handle_error(e)


@ops_group.command(name="replay")
@click.option("--limit", "-n", type=int, default=10, help="Number of records to show")
@click.option("--entity-urn", type=str, default=None, help="Filter by entity URN")
@click.option("--from-sequence", type=int, default=0, help="Start from sequence number")
@add_backend_options
@click.pass_context
def ops_replay(
    ctx: click.Context,
    limit: int,
    entity_urn: str | None,
    from_sequence: int,
    remote_url: str | None,
    remote_api_key: str | None,
) -> None:
    """Replay MCL records.

    Shows metadata change log entries from the operation log, ordered
    by sequence number. Useful for debugging and auditing.

    Examples:
        nexus ops replay --limit 5
        nexus ops replay --entity-urn urn:nexus:file:default:abc123
    """
    from nexus.cli.api_client import get_api_client_from_options

    profile_name = (ctx.obj or {}).get("profile")
    client = get_api_client_from_options(remote_url, remote_api_key, profile_name=profile_name)

    params: dict[str, str | int] = {
        "from_sequence": from_sequence,
        "limit": limit,
    }
    if entity_urn:
        params["entity_urn"] = entity_urn

    try:
        result = client.get("/api/v2/ops/replay", params=params)
    except Exception as e:
        console.print(f"[nexus.error]Error:[/nexus.error] {e}")
        raise SystemExit(1) from e

    records = result.get("records", [])
    if not records:
        console.print("[nexus.warning]No MCL records found[/nexus.warning]")
        return

    console.print(f"[bold]MCL Records (from seq {from_sequence}):[/bold]")
    console.print()

    for r in records:
        seq = r.get("sequence_number", "?")
        urn = r.get("entity_urn", "?")
        aspect = r.get("aspect_name", "?")
        change = r.get("change_type", "?")
        ts = r.get("timestamp", "?")
        console.print(f"  #{seq:>6}  {change:>12}  {aspect:20s}  {urn}")
        console.print(f"          {ts}")
        console.print()

    if result.get("has_more"):
        next_cursor = result.get("next_cursor", "?")
        console.print(
            f"[nexus.muted]More records available. Use --from-sequence {next_cursor}[/nexus.muted]"
        )


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
    import asyncio

    asyncio.run(_async_undo(agent, yes, remote_url, remote_api_key))


async def _async_undo(
    agent: str | None, yes: bool, remote_url: str | None, remote_api_key: str | None
) -> None:
    try:
        nx = await get_filesystem(remote_url, remote_api_key)

        ops_service = nx.service("operations")
        if ops_service is None:
            raise click.ClickException("Undo requires a local NexusFS instance")

        last_op = ops_service.get_last_operation(
            agent_id=agent,
            status="success",
        )

        if not last_op:
            console.print("[nexus.warning]No operations to undo[/nexus.warning]")
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
            console.print(f"  [nexus.warning]Warning: {result['message']}[/nexus.warning]")

        console.print(
            f"\n[nexus.success]✓[/nexus.success] Undid operation: {last_op['operation_type']}"
        )

        nx.close()

    except Exception as e:
        handle_error(e)
