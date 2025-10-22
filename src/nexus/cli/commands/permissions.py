"""Permission commands - chmod, chown, chgrp, getfacl, setfacl."""

from __future__ import annotations

import sys

import click

from nexus.cli.utils import (
    BackendConfig,
    add_backend_options,
    console,
    get_filesystem,
    handle_error,
)
from nexus.core.nexus_fs import NexusFS


def register_commands(cli: click.Group) -> None:
    """Register all permission commands."""
    cli.add_command(chmod_cmd)
    cli.add_command(chown_cmd)
    cli.add_command(chgrp_cmd)
    cli.add_command(getfacl_cmd)
    cli.add_command(setfacl_cmd)


@click.command(name="chmod")
@click.argument("mode", type=str)
@click.argument("path", type=str)
@add_backend_options
def chmod_cmd(
    mode: str,
    path: str,
    backend_config: BackendConfig,
) -> None:
    """Change file mode (permissions).

    Mode can be specified as octal (e.g., '755', '0o644') or
    symbolic (e.g., 'rwxr-xr-x').

    Examples:
        nexus chmod 755 /workspace/script.sh
        nexus chmod 0o644 /workspace/data.txt
        nexus chmod rwxr-xr-x /workspace/file.txt
    """
    try:
        nx = get_filesystem(backend_config)

        # Note: Only Embedded mode supports permissions
        if not isinstance(nx, NexusFS):
            console.print("[red]Error:[/red] chmod is only available in embedded mode")
            nx.close()
            sys.exit(1)

        # Check if file exists
        if not nx.exists(path):
            console.print(f"[red]Error:[/red] File not found: {path}")
            nx.close()
            sys.exit(1)

        # Use chmod method with permission checks
        nx.chmod(path, mode)
        nx.close()

        from nexus.core.permissions import FileMode, parse_mode

        mode_int = parse_mode(mode) if isinstance(mode, str) else mode
        mode_obj = FileMode(mode_int)
        console.print(
            f"[green]✓[/green] Changed mode of [cyan]{path}[/cyan] to [yellow]{mode_obj}[/yellow]"
        )
    except Exception as e:
        handle_error(e)


@click.command(name="chown")
@click.argument("owner", type=str)
@click.argument("path", type=str)
@add_backend_options
def chown_cmd(
    owner: str,
    path: str,
    backend_config: BackendConfig,
) -> None:
    """Change file owner.

    Examples:
        nexus chown alice /workspace/file.txt
        nexus chown bob /workspace/data/
    """
    try:
        nx = get_filesystem(backend_config)

        # Note: Only Embedded mode supports permissions
        if not isinstance(nx, NexusFS):
            console.print("[red]Error:[/red] chown is only available in embedded mode")
            nx.close()
            sys.exit(1)

        # Check if file exists
        if not nx.exists(path):
            console.print(f"[red]Error:[/red] File not found: {path}")
            nx.close()
            sys.exit(1)

        # Use chown method with permission checks
        nx.chown(path, owner)
        nx.close()

        console.print(
            f"[green]✓[/green] Changed owner of [cyan]{path}[/cyan] to [yellow]{owner}[/yellow]"
        )
    except Exception as e:
        handle_error(e)


@click.command(name="chgrp")
@click.argument("group", type=str)
@click.argument("path", type=str)
@add_backend_options
def chgrp_cmd(
    group: str,
    path: str,
    backend_config: BackendConfig,
) -> None:
    """Change file group.

    Examples:
        nexus chgrp developers /workspace/code/
        nexus chgrp admins /workspace/config.yaml
    """
    try:
        nx = get_filesystem(backend_config)

        # Note: Only Embedded mode supports permissions
        if not isinstance(nx, NexusFS):
            console.print("[red]Error:[/red] chgrp is only available in embedded mode")
            nx.close()
            sys.exit(1)

        # Check if file exists
        if not nx.exists(path):
            console.print(f"[red]Error:[/red] File not found: {path}")
            nx.close()
            sys.exit(1)

        # Use chgrp method with permission checks
        nx.chgrp(path, group)
        nx.close()

        console.print(
            f"[green]✓[/green] Changed group of [cyan]{path}[/cyan] to [yellow]{group}[/yellow]"
        )
    except Exception as e:
        handle_error(e)


@click.command(name="getfacl")
@click.argument("path", type=str)
@add_backend_options
def getfacl_cmd(
    path: str,
    backend_config: BackendConfig,
) -> None:
    """Display Access Control List (ACL) for a file.

    Examples:
        nexus getfacl /workspace/file.txt
        nexus getfacl /workspace/data/
    """
    try:
        nx = get_filesystem(backend_config)

        # Note: Only Embedded mode supports ACLs
        if not isinstance(nx, NexusFS):
            console.print("[red]Error:[/red] getfacl is only available in embedded mode")
            nx.close()
            sys.exit(1)

        # Check if file exists
        if not nx.exists(path):
            console.print(f"[red]Error:[/red] File not found: {path}")
            nx.close()
            sys.exit(1)

        # Get file metadata
        file_meta = nx.metadata.get(path)
        if not file_meta:
            console.print(f"[red]Error:[/red] File not found: {path}")
            nx.close()
            sys.exit(1)

        # Display file info
        console.print(f"[bold]# file: {path}[/bold]")
        console.print(f"# owner: {file_meta.owner or 'N/A'}")
        console.print(f"# group: {file_meta.group or 'N/A'}")

        if file_meta.mode is not None:
            from nexus.core.permissions import FileMode

            mode_obj = FileMode(file_meta.mode)
            console.print(f"# mode: {oct(file_meta.mode)} ({mode_obj})")
        else:
            console.print("# mode: N/A")

        # Get ACL entries from database
        from sqlalchemy import select

        from nexus.storage.models import ACLEntryModel

        # Get path_id using public API
        path_id = nx.metadata.get_path_id(path)
        if path_id:
            with nx.metadata.SessionLocal() as session:
                stmt = select(ACLEntryModel).where(ACLEntryModel.path_id == path_id)
                acl_entries = session.scalars(stmt).all()

                if acl_entries:
                    console.print()
                    console.print("[bold]# ACL entries:[/bold]")
                    for entry in acl_entries:
                        deny_prefix = "deny:" if entry.deny else ""
                        if entry.identifier:
                            console.print(
                                f"{deny_prefix}{entry.entry_type}:{entry.identifier}:{entry.permissions}"
                            )
                        else:
                            console.print(f"{deny_prefix}{entry.entry_type}:{entry.permissions}")
                else:
                    console.print()
                    console.print("[dim]# No ACL entries[/dim]")

        nx.close()

    except Exception as e:
        handle_error(e)


@click.command(name="setfacl")
@click.argument("acl_entry", type=str)
@click.argument("path", type=str)
@click.option("--remove", "-x", is_flag=True, help="Remove ACL entry")
@add_backend_options
def setfacl_cmd(
    acl_entry: str,
    path: str,
    remove: bool,
    backend_config: BackendConfig,
) -> None:
    """Set or remove Access Control List (ACL) entry.

    ACL Entry Format:
        user:<username>:rwx    - Grant user permissions
        group:<groupname>:r-x  - Grant group permissions
        deny:user:<username>   - Deny user access

    Examples:
        # Grant alice read+write
        nexus setfacl user:alice:rw- /workspace/file.txt

        # Grant developers group read+execute
        nexus setfacl group:developers:r-x /workspace/code/

        # Deny bob access
        nexus setfacl deny:user:bob /workspace/secret.txt

        # Remove ACL entry
        nexus setfacl user:alice:rwx /workspace/file.txt --remove
    """
    try:
        from nexus.core.acl import ACLEntry

        nx = get_filesystem(backend_config)

        # Note: Only Embedded mode supports ACLs
        if not isinstance(nx, NexusFS):
            console.print("[red]Error:[/red] setfacl is only available in embedded mode")
            nx.close()
            sys.exit(1)

        # Check if file exists
        if not nx.exists(path):
            console.print(f"[red]Error:[/red] File not found: {path}")
            nx.close()
            sys.exit(1)

        # Parse ACL entry
        try:
            entry = ACLEntry.from_string(acl_entry)
        except ValueError as e:
            console.print(f"[red]Error:[/red] Invalid ACL entry: {e}")
            nx.close()
            sys.exit(1)

        # Get file metadata to find path_id
        file_meta = nx.metadata.get(path)
        if not file_meta:
            console.print(f"[red]Error:[/red] File not found: {path}")
            nx.close()
            sys.exit(1)

        # Get path_id using public API
        path_id = nx.metadata.get_path_id(path)
        if path_id:
            from sqlalchemy import delete

            from nexus.storage.models import ACLEntryModel

            with nx.metadata.SessionLocal() as session:
                if remove:
                    # Remove ACL entry
                    stmt = delete(ACLEntryModel).where(
                        ACLEntryModel.path_id == path_id,
                        ACLEntryModel.entry_type == entry.entry_type.value,
                        ACLEntryModel.identifier == entry.identifier,
                    )
                    result = session.execute(stmt)
                    session.commit()

                    if result.rowcount > 0:  # type: ignore[attr-defined]
                        console.print(
                            f"[green]✓[/green] Removed ACL entry [yellow]{acl_entry}[/yellow] "
                            f"from [cyan]{path}[/cyan]"
                        )
                    else:
                        console.print("[yellow]No matching ACL entry found to remove[/yellow]")
                else:
                    # Add ACL entry
                    # First remove existing entry for same type+identifier
                    stmt = delete(ACLEntryModel).where(
                        ACLEntryModel.path_id == path_id,
                        ACLEntryModel.entry_type == entry.entry_type.value,
                        ACLEntryModel.identifier == entry.identifier,
                    )
                    session.execute(stmt)

                    # Create new entry
                    acl_model = ACLEntryModel(
                        path_id=path_id,
                        entry_type=entry.entry_type.value,
                        identifier=entry.identifier,
                        permissions=entry.to_string().split(":")[-1],  # Get rwx part
                        deny=entry.deny,
                    )
                    session.add(acl_model)
                    session.commit()

                    console.print(
                        f"[green]✓[/green] Added ACL entry [yellow]{acl_entry}[/yellow] "
                        f"to [cyan]{path}[/cyan]"
                    )

        nx.close()

    except Exception as e:
        handle_error(e)
