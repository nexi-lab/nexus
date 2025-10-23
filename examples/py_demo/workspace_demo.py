#!/usr/bin/env python3
"""Workspace Versioning Demo - Time-Travel for Agent Workspaces

This script demonstrates workspace snapshot and restore functionality using
the Python API for time-travel debugging and rollback capabilities.

Features demonstrated:
- Creating workspace snapshots with metadata
- Viewing snapshot history
- Comparing snapshots (diff)
- Restoring workspace to previous state
- CAS-backed deduplication (zero storage overhead)
"""

import json
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from nexus import LocalBackend, NexusFS

console = Console()


def print_step(step_num: int, title: str):
    """Print a formatted step header."""
    console.print(f"\n[bold blue]Step {step_num}: {title}[/bold blue]")


def print_success(message: str):
    """Print a success message."""
    console.print(f"[green]✓ {message}[/green]")


def print_warning(message: str):
    """Print a warning message."""
    console.print(f"[yellow]⚠ {message}[/yellow]")


def print_error(message: str):
    """Print an error message."""
    console.print(f"[red]✗ {message}[/red]")


def main():
    """Run the workspace versioning demo."""
    import os
    import shutil

    console.print(
        Panel.fit(
            "[bold blue]Nexus Workspace Versioning Demo[/bold blue]\n"
            "Time-Travel for Agent Workspaces",
            border_style="blue",
        )
    )

    # Setup
    print_step(1, "Setting up Nexus")

    # Use a fixed temp directory and clean it up at the start
    temp_dir = Path("/tmp/nexus-workspace-demo-py")
    if temp_dir.exists():
        shutil.rmtree(temp_dir)
    temp_dir.mkdir(parents=True, exist_ok=True)

    backend = LocalBackend(temp_dir / "storage")

    # Check if PostgreSQL is configured
    db_url = os.environ.get("NEXUS_DATABASE_URL")
    if db_url:
        console.print(
            f"[dim]Using PostgreSQL: {db_url.split('@')[1] if '@' in db_url else 'configured'}[/dim]"
        )
        # Use PostgreSQL but clean up old demo data first
        nx = NexusFS(
            backend=backend,
            agent_id="demo-agent",
            tenant_id="demo-tenant",
            auto_parse=False,
            enforce_permissions=False,
        )
        # Clean up old snapshots for this demo agent
        from sqlalchemy import delete

        from nexus.storage.models import WorkspaceSnapshotModel

        with nx.metadata.SessionLocal() as session:
            stmt = delete(WorkspaceSnapshotModel).where(
                WorkspaceSnapshotModel.tenant_id == "demo-tenant",
                WorkspaceSnapshotModel.agent_id == "demo-agent",
            )
            session.execute(stmt)
            session.commit()
        console.print("[dim]Cleaned up old demo data from PostgreSQL[/dim]")
    else:
        # Use SQLite
        nx = NexusFS(
            backend=backend,
            db_path=temp_dir / "metadata.db",
            agent_id="demo-agent",
            tenant_id="demo-tenant",
            auto_parse=False,
            enforce_permissions=False,
        )
    print_success(f"Nexus initialized at {temp_dir}")

    # Create initial workspace state
    print_step(2, "Creating initial workspace files")
    workspace_prefix = "/workspace/demo-tenant/demo-agent"

    nx.write(
        f"{workspace_prefix}/README.md",
        b"""# My Project

This is the initial version of my project.
""",
    )

    nx.write(
        f"{workspace_prefix}/config.json",
        json.dumps({"name": "demo-project", "version": "1.0.0", "debug": False}, indent=2).encode(),
    )

    nx.write(
        f"{workspace_prefix}/data/users.json",
        json.dumps([{"id": 1, "name": "Alice"}, {"id": 2, "name": "Bob"}], indent=2).encode(),
    )

    print_success("Created 3 files")

    # Create snapshot 1
    print_step(3, "Creating Snapshot #1 (Initial State)")
    snap1 = nx.workspace_snapshot(description="Initial project setup", tags=["v1.0", "stable"])
    console.print(f"  Snapshot ID: {snap1['snapshot_id']}")
    console.print(f"  Files: {snap1['file_count']}")
    console.print(f"  Size: {snap1['total_size_bytes']} bytes")
    print_success("Snapshot #1 created")

    # Make some changes
    print_step(4, "Making changes to workspace")

    # Update config
    nx.write(
        f"{workspace_prefix}/config.json",
        json.dumps(
            {
                "name": "demo-project",
                "version": "1.1.0",
                "debug": True,
                "features": ["logging", "metrics"],
            },
            indent=2,
        ).encode(),
    )

    # Add new file
    nx.write(
        f"{workspace_prefix}/data/products.json",
        json.dumps(
            [
                {"id": 1, "name": "Widget", "price": 9.99},
                {"id": 2, "name": "Gadget", "price": 19.99},
            ],
            indent=2,
        ).encode(),
    )

    # Update README
    nx.write(
        f"{workspace_prefix}/README.md",
        b"""# My Project

This is version 1.1 with new features!

## Features
- Logging
- Metrics
- User management
- Product catalog
""",
    )

    print_success("Made changes: updated 2 files, added 1 new file")

    # Create snapshot 2
    print_step(5, "Creating Snapshot #2 (With Features)")
    snap2 = nx.workspace_snapshot(
        description="Added logging and metrics features", tags=["v1.1", "development"]
    )
    console.print(f"  Files: {snap2['file_count']}")
    print_success("Snapshot #2 created")

    # Make breaking changes
    print_step(6, "Making breaking changes")

    # Delete a file
    nx.delete(f"{workspace_prefix}/data/users.json")

    # Break the config
    nx.write(
        f"{workspace_prefix}/config.json",
        json.dumps(
            {
                "name": "demo-project-BROKEN",
                "version": "2.0.0-alpha",
                "debug": True,
                "experimental": True,
                "DANGER": "This config is broken!",
            },
            indent=2,
        ).encode(),
    )

    print_error("Oh no! Made breaking changes")

    # Create snapshot 3
    print_step(7, "Creating Snapshot #3 (Broken State)")
    nx.workspace_snapshot(
        description="Experimental changes (broken)", tags=["v2.0-alpha", "broken"]
    )
    print_success("Snapshot #3 created")

    # View snapshot history
    print_step(8, "Viewing Snapshot History")
    snapshots = nx.workspace_log(limit=10)

    table = Table(title="Workspace Snapshots")
    table.add_column("#", justify="right", style="cyan")
    table.add_column("Description")
    table.add_column("Tags", style="dim")
    table.add_column("Files", justify="right")
    table.add_column("Created")

    for snap in reversed(snapshots):  # Show oldest first
        tags_str = ", ".join(snap["tags"]) if snap["tags"] else ""
        created = snap["created_at"].strftime("%H:%M:%S")

        table.add_row(
            str(snap["snapshot_number"]),
            snap["description"] or "",
            tags_str,
            str(snap["file_count"]),
            created,
        )

    console.print(table)

    # Compare snapshots
    print_step(9, "Comparing Snapshots #1 and #2")
    diff_1_2 = nx.workspace_diff(snapshot_1=1, snapshot_2=2)

    console.print(f"\n[green]Added ({len(diff_1_2['added'])} files):[/green]")
    for file in diff_1_2["added"]:
        console.print(f"  + {file['path']}")

    console.print(f"\n[yellow]Modified ({len(diff_1_2['modified'])} files):[/yellow]")
    for file in diff_1_2["modified"]:
        console.print(f"  ~ {file['path']}")

    console.print(f"\n[dim]Unchanged: {diff_1_2['unchanged']} files[/dim]")

    print_step(10, "Comparing Snapshots #2 and #3")
    diff_2_3 = nx.workspace_diff(snapshot_1=2, snapshot_2=3)

    console.print(f"\n[red]Removed ({len(diff_2_3['removed'])} files):[/red]")
    for file in diff_2_3["removed"]:
        console.print(f"  - {file['path']}")

    console.print(f"\n[yellow]Modified ({len(diff_2_3['modified'])} files):[/yellow]")
    for file in diff_2_3["modified"]:
        console.print(f"  ~ {file['path']}")

    # Time-travel: restore to good state
    print_step(11, "Time-Travel - Restoring to Snapshot #2")
    print_warning("Restoring workspace to the last working state...")

    result = nx.workspace_restore(snapshot_number=2)

    console.print(f"  Files restored: {result['files_restored']}")
    console.print(f"  Files deleted: {result['files_deleted']}")
    print_success("Workspace restored to Snapshot #2")

    # Verify restoration
    print_step(12, "Verifying Restoration")

    # Check config
    config_content = nx.read(f"{workspace_prefix}/config.json")
    config = json.loads(config_content.decode())
    console.print("\n[bold]Config file:[/bold]")
    console.print(json.dumps(config, indent=2))

    # Check if users file is back
    if nx.exists(f"{workspace_prefix}/data/users.json"):
        print_success("Users file restored successfully")
        users = json.loads(nx.read(f"{workspace_prefix}/data/users.json").decode())
        console.print(f"  Users: {users}")
    else:
        print_error("Users file not found")

    # List current workspace
    print_step(13, "Current Workspace State")
    files = nx.list(path=workspace_prefix, details=True)
    console.print(f"\nWorkspace has {len(files)} files:")
    for file in files:
        # Extract relative path
        rel_path = (
            file["path"][len(workspace_prefix) :]
            if file["path"].startswith(workspace_prefix)
            else file["path"]
        )
        size_kb = file["size"] / 1024
        console.print(f"  {rel_path:40s} {size_kb:8.2f} KB")

    # Cleanup
    nx.close()

    # Summary
    console.print(
        Panel.fit(
            "[bold green]Demo Complete![/bold green]\n\n"
            "[bold]Key Takeaways:[/bold]\n"
            "  • Created 3 snapshots tracking workspace evolution\n"
            "  • Compared different versions to see what changed\n"
            "  • Restored workspace to previous working state\n"
            "  • All content deduplicated using CAS (no storage waste!)\n\n"
            f"Data location: {temp_dir}",
            border_style="green",
        )
    )

    console.print("\n[dim]Try the API yourself:[/dim]")
    console.print("""
from nexus import LocalBackend, NexusFS

# Initialize
nx = NexusFS(backend, agent_id="my-agent")

# Create snapshot
snapshot = nx.workspace_snapshot(description="Before changes")

# View history
history = nx.workspace_log(limit=10)

# Compare versions
diff = nx.workspace_diff(snapshot_1=1, snapshot_2=2)

# Restore
result = nx.workspace_restore(snapshot_number=1)
""")


if __name__ == "__main__":
    main()
