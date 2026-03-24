"""Context versioning commands — workspace branching + explore() (Issue #1315)."""

from typing import Any

import click
from rich.console import Console
from rich.table import Table

from nexus.cli.utils import add_backend_options, get_filesystem, handle_error

console = Console()


def _get_branch_service(nx: Any) -> Any:
    """Extract ContextBranchService via ServiceRegistry (Issue #1771)."""
    return nx.service("context_branch") if nx else None


@click.group(name="context")
def context_group() -> None:
    """Context versioning — workspace branching and exploration.

    Git-like branching on top of workspace snapshots for agent context management.
    Branches are metadata-only pointers — zero-copy, instant creation.

    Examples:
        nexus context commit /workspace --message "Checkpoint"
        nexus context branch /workspace --name feature-x
        nexus context checkout /workspace --target feature-x
        nexus context merge /workspace --source feature-x
        nexus context log /workspace
        nexus context branches /workspace
        nexus context explore /workspace --description "Try new approach"
        nexus context finish /workspace --branch try-new-approach --outcome merge
    """
    pass


@context_group.command(name="commit")
@click.argument("workspace", type=str)
@click.option("--message", "-m", default=None, help="Commit message")
@click.option("--branch", "-b", default=None, help="Branch to commit to (default: current)")
@add_backend_options
def commit_cmd(
    workspace: str,
    message: str | None,
    branch: str | None,
    remote_url: str | None,
    remote_api_key: str | None,
) -> None:
    """Create a snapshot and advance branch HEAD.

    Examples:
        nexus context commit /workspace -m "Before refactor"
        nexus context commit /workspace -m "Feature work" -b feature-x
    """
    try:
        nx = get_filesystem(remote_url, remote_api_key)
        svc = _get_branch_service(nx)
        if not svc:
            console.print("[red]Context branching not available (service not configured)[/red]")
            return
        result = svc.commit(workspace, message=message, branch_name=branch)
        snap = result["snapshot"]
        console.print(
            f"[green]✓[/green] Committed v{snap['snapshot_number']} on branch '{result['branch']}'"
        )
        if snap.get("description"):
            console.print(f"  Message: {snap['description']}")
        nx.close()
    except Exception as e:
        handle_error(e)


@context_group.command(name="branch")
@click.argument("workspace", type=str)
@click.option("--name", "-n", required=True, help="Branch name")
@click.option("--from-branch", default=None, help="Fork from this branch (default: current)")
@add_backend_options
def branch_cmd(
    workspace: str,
    name: str,
    from_branch: str | None,
    remote_url: str | None,
    remote_api_key: str | None,
) -> None:
    """Create a new named branch.

    Examples:
        nexus context branch /workspace --name feature-x
        nexus context branch /workspace --name hotfix --from-branch main
    """
    try:
        nx = get_filesystem(remote_url, remote_api_key)
        svc = _get_branch_service(nx)
        if not svc:
            console.print("[red]Context branching not available[/red]")
            return
        result = svc.create_branch(workspace, name, from_branch=from_branch)
        console.print(
            f"[green]✓[/green] Created branch '{result.branch_name}' "
            f"from '{result.parent_branch or 'main'}'"
        )
        if result.fork_point_id:
            console.print(f"  Fork point: {result.fork_point_id[:12]}...")
        nx.close()
    except Exception as e:
        handle_error(e)


@context_group.command(name="checkout")
@click.argument("workspace", type=str)
@click.option("--target", "-t", required=True, help="Branch name to switch to")
@add_backend_options
def checkout_cmd(
    workspace: str,
    target: str,
    remote_url: str | None,
    remote_api_key: str | None,
) -> None:
    """Switch to a different branch and restore its workspace state.

    Examples:
        nexus context checkout /workspace --target feature-x
        nexus context checkout /workspace --target main
    """
    try:
        nx = get_filesystem(remote_url, remote_api_key)
        svc = _get_branch_service(nx)
        if not svc:
            console.print("[red]Context branching not available[/red]")
            return
        result = svc.checkout(workspace, target)
        console.print(f"[green]✓[/green] Switched to branch '{result['branch']}'")
        if result.get("restore_info"):
            ri = result["restore_info"]
            console.print(
                f"  Restored {ri.get('files_restored', 0)} files, "
                f"deleted {ri.get('files_deleted', 0)} files"
            )
        nx.close()
    except Exception as e:
        handle_error(e)


@context_group.command(name="merge")
@click.argument("workspace", type=str)
@click.option("--source", "-s", required=True, help="Branch to merge from")
@click.option("--target", "-t", default=None, help="Branch to merge into (default: current)")
@click.option(
    "--strategy",
    type=click.Choice(["fail", "source-wins"]),
    default="fail",
    help="Conflict resolution strategy",
)
@add_backend_options
def merge_cmd(
    workspace: str,
    source: str,
    target: str | None,
    strategy: str,
    remote_url: str | None,
    remote_api_key: str | None,
) -> None:
    """Merge a branch into another.

    Examples:
        nexus context merge /workspace --source feature-x
        nexus context merge /workspace --source feature-x --target main --strategy source-wins
    """
    try:
        nx = get_filesystem(remote_url, remote_api_key)
        svc = _get_branch_service(nx)
        if not svc:
            console.print("[red]Context branching not available[/red]")
            return
        result = svc.merge(workspace, source, target_branch=target, strategy=strategy)
        if result.fast_forward:
            console.print(
                f"[green]✓[/green] Fast-forward merge: '{source}' → '{target or 'current'}'"
            )
        else:
            console.print(
                f"[green]✓[/green] Merged '{source}' → '{target or 'current'}' "
                f"(+{result.files_added} -{result.files_removed} ~{result.files_modified})"
            )
        nx.close()
    except Exception as e:
        handle_error(e)


@context_group.command(name="log")
@click.argument("workspace", type=str)
@click.option("--limit", "-l", default=20, help="Maximum entries to show")
@add_backend_options
def log_cmd(
    workspace: str,
    limit: int,
    remote_url: str | None,
    remote_api_key: str | None,
) -> None:
    """Show snapshot history for a workspace.

    Examples:
        nexus context log /workspace
        nexus context log /workspace --limit 5
    """
    try:
        nx = get_filesystem(remote_url, remote_api_key)
        svc = _get_branch_service(nx)
        if not svc:
            console.print("[red]Context branching not available[/red]")
            return
        snapshots = svc.log(workspace, limit=limit)

        if not snapshots:
            console.print("[dim]No snapshots found[/dim]")
            nx.close()
            return

        table = Table(title=f"Context Log: {workspace}")
        table.add_column("#", style="bold")
        table.add_column("Description")
        table.add_column("Created", style="dim")

        for s in snapshots:
            table.add_row(
                str(s.get("snapshot_number", "")),
                s.get("description", "") or "[dim]no message[/dim]",
                str(s.get("created_at", ""))[:19],
            )

        console.print(table)
        nx.close()
    except Exception as e:
        handle_error(e)


@context_group.command(name="branches")
@click.argument("workspace", type=str)
@click.option("--all", "show_all", is_flag=True, help="Include inactive branches")
@add_backend_options
def branches_cmd(
    workspace: str,
    show_all: bool,
    remote_url: str | None,
    remote_api_key: str | None,
) -> None:
    """List all branches for a workspace.

    Examples:
        nexus context branches /workspace
        nexus context branches /workspace --all
    """
    try:
        nx = get_filesystem(remote_url, remote_api_key)
        svc = _get_branch_service(nx)
        if not svc:
            console.print("[red]Context branching not available[/red]")
            return
        branches = svc.list_branches(workspace, include_inactive=show_all)

        if not branches:
            console.print("[dim]No branches found[/dim]")
            nx.close()
            return

        table = Table(title=f"Branches: {workspace}")
        table.add_column("Branch", style="bold")
        table.add_column("Status")
        table.add_column("Current")
        table.add_column("HEAD")
        table.add_column("Parent")

        for b in branches:
            status_color = {"active": "green", "merged": "blue", "discarded": "red"}.get(
                b.status, "white"
            )
            table.add_row(
                b.branch_name,
                f"[{status_color}]{b.status}[/{status_color}]",
                "[green]*[/green]" if b.is_current else "",
                (b.head_snapshot_id or "")[:12],
                b.parent_branch or "",
            )

        console.print(table)
        nx.close()
    except Exception as e:
        handle_error(e)


@context_group.command(name="diff")
@click.argument("workspace", type=str)
@click.option("--from", "from_ref", required=True, help="First snapshot ID")
@click.option("--to", "to_ref", required=True, help="Second snapshot ID")
@add_backend_options
def diff_cmd(
    workspace: str,
    from_ref: str,
    to_ref: str,
    remote_url: str | None,
    remote_api_key: str | None,
) -> None:
    """Compare two snapshots.

    Examples:
        nexus context diff /workspace --from snap-id-1 --to snap-id-2
    """
    try:
        nx = get_filesystem(remote_url, remote_api_key)
        svc = _get_branch_service(nx)
        if not svc:
            console.print("[red]Context branching not available[/red]")
            return
        result = svc.diff(workspace, from_ref, to_ref)
        added = result.get("added", [])
        removed = result.get("removed", [])
        modified = result.get("modified", [])
        console.print(
            f"[green]+{len(added)}[/green] added, [red]-{len(removed)}[/red] removed, [yellow]~{len(modified)}[/yellow] modified"
        )
        for f in added:
            console.print(f"  [green]+[/green] {f['path']}")
        for f in removed:
            console.print(f"  [red]-[/red] {f['path']}")
        for f in modified:
            console.print(f"  [yellow]~[/yellow] {f['path']}")
        nx.close()
    except Exception as e:
        handle_error(e)


@context_group.command(name="explore")
@click.argument("workspace", type=str)
@click.option("--description", "-d", required=True, help="Description of exploration")
@add_backend_options
def explore_cmd(
    workspace: str,
    description: str,
    remote_url: str | None,
    remote_api_key: str | None,
) -> None:
    """Start an exploration: auto-commit + create branch + checkout.

    Examples:
        nexus context explore /workspace -d "Try event sourcing"
    """
    try:
        nx = get_filesystem(remote_url, remote_api_key)
        svc = _get_branch_service(nx)
        if not svc:
            console.print("[red]Context branching not available[/red]")
            return
        result = svc.explore(workspace, description)
        console.print(f"[green]✓[/green] Exploring on branch '{result.branch_name}'")
        if result.skipped_commit:
            console.print("  [dim]Skipped auto-commit (workspace unchanged)[/dim]")
        if result.fork_point_snapshot_id:
            console.print(f"  Fork point: {result.fork_point_snapshot_id[:12]}...")
        nx.close()
    except Exception as e:
        handle_error(e)


@context_group.command(name="finish")
@click.argument("workspace", type=str)
@click.option("--branch", "-b", required=True, help="Exploration branch to finish")
@click.option(
    "--outcome",
    type=click.Choice(["merge", "discard"]),
    default="merge",
    help="Merge changes back or discard",
)
@click.option(
    "--strategy",
    type=click.Choice(["fail", "source-wins"]),
    default="source-wins",
    help="Conflict strategy for merge",
)
@add_backend_options
def finish_cmd(
    workspace: str,
    branch: str,
    outcome: str,
    strategy: str,
    remote_url: str | None,
    remote_api_key: str | None,
) -> None:
    """Finish an exploration: merge or discard the branch.

    Examples:
        nexus context finish /workspace -b try-event-sourcing --outcome merge
        nexus context finish /workspace -b bad-idea --outcome discard
    """
    try:
        nx = get_filesystem(remote_url, remote_api_key)
        svc = _get_branch_service(nx)
        if not svc:
            console.print("[red]Context branching not available[/red]")
            return
        result = svc.finish_explore(workspace, branch, outcome=outcome, strategy=strategy)
        if result["outcome"] == "merged":
            console.print(f"[green]✓[/green] Merged '{branch}' into '{result['merged_into']}'")
        else:
            console.print(
                f"[yellow]✓[/yellow] Discarded branch '{branch}', returned to '{result['returned_to']}'"
            )
        nx.close()
    except Exception as e:
        handle_error(e)


def register_commands(cli: click.Group) -> None:
    """Register context versioning commands."""
    cli.add_command(context_group)
