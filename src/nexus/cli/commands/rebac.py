"""ReBAC (Relationship-Based Access Control) CLI commands.

Manage authorization relationships using Zanzibar-style ReBAC.
Enables team-based permissions, hierarchical access, and dynamic inheritance.
"""

from __future__ import annotations

import sys

import click
from rich.table import Table

from nexus.cli.utils import (
    BackendConfig,
    add_backend_options,
    console,
    get_filesystem,
    handle_error,
)
from nexus.core.nexus_fs import NexusFS


@click.group(name="rebac")
def rebac() -> None:
    """Relationship-Based Access Control (ReBAC) commands.

    Manage authorization relationships using Zanzibar-style ReBAC.
    Enables team-based permissions, hierarchical access, and dynamic inheritance.

    Examples:
        nexus rebac create agent alice member-of group eng-team
        nexus rebac check agent alice read file file123
        nexus rebac expand read file file123
        nexus rebac delete <tuple-id>
    """
    pass


@rebac.command(name="create")
@click.argument("subject_type", type=str)
@click.argument("subject_id", type=str)
@click.argument("relation", type=str)
@click.argument("object_type", type=str)
@click.argument("object_id", type=str)
@click.option("--expires", type=str, default=None, help="Expiration time (ISO format)")
@add_backend_options
def rebac_create(
    subject_type: str,
    subject_id: str,
    relation: str,
    object_type: str,
    object_id: str,
    expires: str | None,
    backend_config: BackendConfig,
) -> None:
    """Create a relationship tuple.

    Creates a (subject, relation, object) tuple representing a relationship.

    Examples:
        # Alice is member of eng-team
        nexus rebac create agent alice member-of group eng-team

        # Eng-team owns file123
        nexus rebac create group eng-team owner-of file file123

        # Parent folder has child folder
        nexus rebac create file parent-folder parent-of file child-folder

        # Temporary access (expires in 1 hour)
        nexus rebac create agent bob viewer-of file secret --expires 2025-12-31T23:59:59
    """
    try:
        from pathlib import Path

        from nexus.core.rebac_manager import ReBACManager

        nx = get_filesystem(backend_config)

        # Only Embedded mode supports ReBAC
        if not isinstance(nx, NexusFS):
            console.print("[red]Error:[/red] ReBAC is only available in embedded mode")
            nx.close()
            sys.exit(1)

        # Get database path
        db_path = Path(backend_config.data_dir) / "metadata.db"
        rebac_mgr = ReBACManager(db_path=str(db_path))

        # Parse expiration time if provided
        expires_at = None
        if expires:
            from datetime import datetime

            try:
                expires_at = datetime.fromisoformat(expires)
            except ValueError:
                console.print(f"[red]Error:[/red] Invalid date format: {expires}")
                console.print("Use ISO format: 2025-12-31T23:59:59")
                nx.close()
                sys.exit(1)

        # Create tuple
        tuple_id = rebac_mgr.rebac_write(
            subject=(subject_type, subject_id),
            relation=relation,
            object=(object_type, object_id),
            expires_at=expires_at,
        )

        rebac_mgr.close()
        nx.close()

        console.print("[green]✓[/green] Created relationship tuple")
        console.print(f"  Tuple ID: [cyan]{tuple_id}[/cyan]")
        console.print(f"  Subject: [yellow]{subject_type}:{subject_id}[/yellow]")
        console.print(f"  Relation: [magenta]{relation}[/magenta]")
        console.print(f"  Object: [yellow]{object_type}:{object_id}[/yellow]")
        if expires_at:
            console.print(f"  Expires: [dim]{expires_at.isoformat()}[/dim]")

    except Exception as e:
        handle_error(e)


@rebac.command(name="delete")
@click.argument("tuple_id", type=str)
@add_backend_options
def rebac_delete_cmd(
    tuple_id: str,
    backend_config: BackendConfig,
) -> None:
    """Delete a relationship tuple.

    Examples:
        nexus rebac delete 550e8400-e29b-41d4-a716-446655440000
    """
    try:
        from pathlib import Path

        from nexus.core.rebac_manager import ReBACManager

        nx = get_filesystem(backend_config)

        # Only Embedded mode supports ReBAC
        if not isinstance(nx, NexusFS):
            console.print("[red]Error:[/red] ReBAC is only available in embedded mode")
            nx.close()
            sys.exit(1)

        # Get database path
        db_path = Path(backend_config.data_dir) / "metadata.db"
        rebac_mgr = ReBACManager(db_path=str(db_path))

        # Delete tuple
        deleted = rebac_mgr.rebac_delete(tuple_id)

        rebac_mgr.close()
        nx.close()

        if deleted:
            console.print(f"[green]✓[/green] Deleted relationship tuple [cyan]{tuple_id}[/cyan]")
        else:
            console.print(f"[yellow]Tuple not found:[/yellow] {tuple_id}")

    except Exception as e:
        handle_error(e)


@rebac.command(name="check")
@click.argument("subject_type", type=str)
@click.argument("subject_id", type=str)
@click.argument("permission", type=str)
@click.argument("object_type", type=str)
@click.argument("object_id", type=str)
@add_backend_options
def rebac_check_cmd(
    subject_type: str,
    subject_id: str,
    permission: str,
    object_type: str,
    object_id: str,
    backend_config: BackendConfig,
) -> None:
    """Check if subject has permission on object.

    Uses graph traversal and caching to determine if permission is granted.

    Examples:
        # Does alice have read permission on file123?
        nexus rebac check agent alice read file file123

        # Does bob have write permission on workspace?
        nexus rebac check agent bob write workspace main

        # Does eng-team have owner permission on project?
        nexus rebac check group eng-team owner file project-folder
    """
    try:
        from pathlib import Path

        from nexus.core.rebac_manager import ReBACManager

        nx = get_filesystem(backend_config)

        # Only Embedded mode supports ReBAC
        if not isinstance(nx, NexusFS):
            console.print("[red]Error:[/red] ReBAC is only available in embedded mode")
            nx.close()
            sys.exit(1)

        # Get database path
        db_path = Path(backend_config.data_dir) / "metadata.db"
        rebac_mgr = ReBACManager(db_path=str(db_path))

        # Check permission
        granted = rebac_mgr.rebac_check(
            subject=(subject_type, subject_id),
            permission=permission,
            object=(object_type, object_id),
        )

        rebac_mgr.close()
        nx.close()

        # Display result
        if granted:
            console.print("[green]✓ GRANTED[/green]")
            console.print(
                f"  [yellow]{subject_type}:{subject_id}[/yellow] has [magenta]{permission}[/magenta] on [yellow]{object_type}:{object_id}[/yellow]"
            )
        else:
            console.print("[red]✗ DENIED[/red]")
            console.print(
                f"  [yellow]{subject_type}:{subject_id}[/yellow] does NOT have [magenta]{permission}[/magenta] on [yellow]{object_type}:{object_id}[/yellow]"
            )

    except Exception as e:
        handle_error(e)


@rebac.command(name="expand")
@click.argument("permission", type=str)
@click.argument("object_type", type=str)
@click.argument("object_id", type=str)
@add_backend_options
def rebac_expand_cmd(
    permission: str,
    object_type: str,
    object_id: str,
    backend_config: BackendConfig,
) -> None:
    """Find all subjects with a given permission on an object.

    Uses recursive graph traversal to find all subjects.

    Examples:
        # Who has read permission on file123?
        nexus rebac expand read file file123

        # Who has write permission on workspace?
        nexus rebac expand write workspace main

        # Who owns the project folder?
        nexus rebac expand owner file project-folder
    """
    try:
        from pathlib import Path

        from nexus.core.rebac_manager import ReBACManager

        nx = get_filesystem(backend_config)

        # Only Embedded mode supports ReBAC
        if not isinstance(nx, NexusFS):
            console.print("[red]Error:[/red] ReBAC is only available in embedded mode")
            nx.close()
            sys.exit(1)

        # Get database path
        db_path = Path(backend_config.data_dir) / "metadata.db"
        rebac_mgr = ReBACManager(db_path=str(db_path))

        # Expand permission
        subjects = rebac_mgr.rebac_expand(
            permission=permission,
            object=(object_type, object_id),
        )

        rebac_mgr.close()
        nx.close()

        # Display results
        if not subjects:
            console.print(
                f"[yellow]No subjects found with[/yellow] [magenta]{permission}[/magenta] [yellow]on[/yellow] [cyan]{object_type}:{object_id}[/cyan]"
            )
            return

        console.print(
            f"[green]Found {len(subjects)} subjects[/green] with [magenta]{permission}[/magenta] on [cyan]{object_type}:{object_id}[/cyan]"
        )
        console.print()

        table = Table(title=f"Subjects with '{permission}' permission")
        table.add_column("Subject Type", style="yellow")
        table.add_column("Subject ID", style="cyan")

        for subj_type, subj_id in sorted(subjects):
            table.add_row(subj_type, subj_id)

        console.print(table)

    except Exception as e:
        handle_error(e)


def register_commands(cli: click.Group) -> None:
    """Register ReBAC commands with the CLI.

    Args:
        cli: The Click CLI group to register commands with
    """
    cli.add_command(rebac)
