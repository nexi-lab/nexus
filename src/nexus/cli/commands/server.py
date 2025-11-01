"""Nexus CLI Server Commands - Mount, unmount, and serve commands.

This module contains server-related CLI commands for:
- Mounting Nexus filesystem with FUSE
- Unmounting FUSE mounts
- Starting the Nexus RPC server
"""

from __future__ import annotations

import contextlib
import logging
import sys
import time
from pathlib import Path

import click

from nexus import NexusFilesystem
from nexus.cli.utils import (
    BackendConfig,
    add_backend_options,
    console,
    get_filesystem,
    handle_error,
)


@click.command(name="mount")
@click.argument("mount_point", type=click.Path())
@click.option(
    "--mode",
    type=click.Choice(["binary", "text", "smart"]),
    default="smart",
    help="Mount mode: binary (raw), text (parsed), smart (auto-detect)",
    show_default=True,
)
@click.option(
    "--daemon",
    is_flag=True,
    help="Run in background (daemon mode)",
)
@click.option(
    "--allow-other",
    is_flag=True,
    help="Allow other users to access the mount",
)
@click.option(
    "--debug",
    is_flag=True,
    help="Enable FUSE debug output",
)
@add_backend_options
def mount(
    mount_point: str,
    mode: str,
    daemon: bool,
    allow_other: bool,
    debug: bool,
    backend_config: BackendConfig,
) -> None:
    """Mount Nexus filesystem to a local path.

    Mounts the Nexus filesystem using FUSE, allowing standard Unix tools
    to work seamlessly with Nexus files.

    Mount Modes:
    - binary: Return raw file content (no parsing)
    - text: Parse all files and return text representation
    - smart (default): Auto-detect file type and return appropriate format

    Virtual File Views:
    - .raw/ directory: Access original binary content
    - _parsed.{ext}.md suffix: View parsed markdown (e.g., file_parsed.xlsx.md)

    Examples:
        # Mount in smart mode (default)
        nexus mount /mnt/nexus

        # Mount in binary mode (raw files only)
        nexus mount /mnt/nexus --mode=binary

        # Mount in background
        nexus mount /mnt/nexus --daemon

        # Mount with debug output
        nexus mount /mnt/nexus --debug

        # Use standard Unix tools
        ls /mnt/nexus
        cat /mnt/nexus/workspace/document.xlsx      # Binary content
        cat /mnt/nexus/workspace/document_parsed.xlsx.md  # Parsed markdown
        grep "TODO" /mnt/nexus/workspace/**/*.py
        vim /mnt/nexus/workspace/file.txt
    """
    try:
        from nexus.fuse import mount_nexus

        # Get filesystem instance (handles both remote and local backends)
        nx: NexusFilesystem = get_filesystem(backend_config)

        # Create mount point if it doesn't exist
        mount_path = Path(mount_point)
        mount_path.mkdir(parents=True, exist_ok=True)

        # Display mount info
        console.print("[green]Mounting Nexus filesystem...[/green]")
        console.print(f"  Mount point: [cyan]{mount_point}[/cyan]")
        console.print(f"  Mode: [cyan]{mode}[/cyan]")
        if backend_config.remote_url:
            console.print(f"  Remote URL: [cyan]{backend_config.remote_url}[/cyan]")
        else:
            console.print(f"  Backend: [cyan]{backend_config.backend}[/cyan]")
        if daemon:
            console.print("  [yellow]Running in background (daemon mode)[/yellow]")

        console.print()
        console.print("[bold cyan]Virtual File Views:[/bold cyan]")
        console.print("  • [cyan].raw/[/cyan] - Access original binary content")
        console.print("  • [cyan]file_parsed.{ext}.md[/cyan] - View parsed markdown")
        console.print()

        # Create log file path for daemon mode (before forking)
        log_file = None
        if daemon:
            log_file = f"/tmp/nexus-mount-{int(time.time())}.log"
            console.print(f"  Logs: [cyan]{log_file}[/cyan]")
            console.print()

        if daemon:
            # Daemon mode: double-fork BEFORE mounting
            import os
            import sys

            # First fork
            pid = os.fork()

            if pid > 0:
                # Parent process - wait for intermediate child to exit, then return
                os.waitpid(pid, 0)  # Reap intermediate child to avoid zombies
                console.print(f"[green]✓[/green] Mounted Nexus to [cyan]{mount_point}[/cyan]")
                console.print()
                console.print("[yellow]To unmount:[/yellow]")
                console.print(f"  nexus unmount {mount_point}")
                console.print()
                console.print("[yellow]To view logs:[/yellow]")
                console.print(f"  tail -f {log_file}")
                return

            # Intermediate child - detach and fork again
            os.setsid()  # Create new session and become session leader

            # Second fork
            pid2 = os.fork()

            if pid2 > 0:
                # Intermediate child exits immediately
                # This makes the grandchild process be adopted by init (PID 1)
                os._exit(0)

            # Grandchild (daemon process) - set up logging and redirect I/O
            sys.stdin.close()

            # log_file must be set when daemon=True
            assert log_file is not None, "log_file must be set in daemon mode"

            # Configure logging to file
            logging.basicConfig(
                filename=log_file,
                level=logging.DEBUG if debug else logging.INFO,
                format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
            )

            # Redirect stdout/stderr to log file (for any print statements or uncaught errors)
            sys.stdout = open(log_file, "a")  # noqa: SIM115
            sys.stderr = open(log_file, "a")  # noqa: SIM115

            # Log daemon startup
            logging.info(f"Nexus FUSE daemon starting (PID: {os.getpid()})")
            logging.info(f"Mount point: {mount_point}")
            logging.info(f"Mode: {mode}")
            if backend_config.remote_url:
                logging.info(f"Remote URL: {backend_config.remote_url}")
            else:
                logging.info(f"Backend: {backend_config.backend}")

            # Now mount the filesystem in the daemon process (foreground mode to block)
            try:
                fuse = mount_nexus(
                    nx,
                    mount_point,
                    mode=mode,
                    foreground=True,  # Run in foreground to keep daemon process alive
                    allow_other=allow_other,
                    debug=debug,
                )
                logging.info("Mount completed, waiting for unmount signal...")
            except Exception as e:
                logging.error(f"Failed to mount: {e}", exc_info=True)
                os._exit(1)

            # Exit cleanly when unmounted
            logging.info("Daemon shutting down")
            os._exit(0)

        # Non-daemon mode: mount in background thread
        fuse = mount_nexus(
            nx,
            mount_point,
            mode=mode,
            foreground=False,  # Run in background thread
            allow_other=allow_other,
            debug=debug,
        )

        console.print(f"[green]Mounted Nexus to [cyan]{mount_point}[/cyan][/green]")
        console.print("[yellow]Press Ctrl+C to unmount[/yellow]")

        # Wait for signal (foreground mode)
        try:
            fuse.wait()
        except KeyboardInterrupt:
            console.print("\n[yellow]Unmounting...[/yellow]")
            fuse.unmount()
            console.print("[green]✓[/green] Unmounted")

    except ImportError:
        console.print(
            "[red]Error:[/red] FUSE support not available. "
            "Install with: pip install 'nexus-ai-fs[fuse]'"
        )
        sys.exit(1)
    except Exception as e:
        handle_error(e)


@click.command(name="unmount")
@click.argument("mount_point", type=click.Path(exists=True))
def unmount(mount_point: str) -> None:
    """Unmount a Nexus filesystem.

    Examples:
        nexus unmount /mnt/nexus
    """
    try:
        import platform
        import subprocess

        system = platform.system()

        console.print(f"[yellow]Unmounting {mount_point}...[/yellow]")

        try:
            if system == "Darwin":  # macOS
                subprocess.run(
                    ["umount", mount_point],
                    check=True,
                    capture_output=True,
                )
            elif system == "Linux":
                subprocess.run(
                    ["fusermount", "-u", mount_point],
                    check=True,
                    capture_output=True,
                )
            else:
                console.print(f"[red]Error:[/red] Unsupported platform: {system}")
                sys.exit(1)

            console.print(f"[green]✓[/green] Unmounted [cyan]{mount_point}[/cyan]")
        except subprocess.CalledProcessError as e:
            error_msg = e.stderr.decode() if e.stderr else str(e)
            console.print(f"[red]Error:[/red] Failed to unmount: {error_msg}")
            sys.exit(1)

    except Exception as e:
        handle_error(e)


@click.command(name="serve")
@click.option("--host", default="0.0.0.0", help="Server host (default: 0.0.0.0)")
@click.option("--port", default=8080, type=int, help="Server port (default: 8080)")
@click.option(
    "--api-key",
    default=None,
    help="API key for authentication (optional, for simple static key auth)",
)
@click.option(
    "--auth-type",
    type=click.Choice(["static", "database", "local", "oidc", "multi-oidc"]),
    default=None,
    help="Authentication type (static, database, local, oidc, multi-oidc)",
)
@click.option(
    "--init",
    is_flag=True,
    help="Initialize server (create admin user, API key, and workspace)",
)
@click.option(
    "--reset",
    is_flag=True,
    help="Reset database to clean state before initialization (DESTRUCTIVE)",
)
@click.option(
    "--admin-user",
    default="admin",
    help="Admin username for initialization (default: admin)",
)
@add_backend_options
def serve(
    host: str,
    port: int,
    api_key: str | None,
    auth_type: str | None,
    init: bool,
    reset: bool,
    admin_user: str,
    backend_config: BackendConfig,
) -> None:
    """Start Nexus RPC server.

    Exposes all NexusFileSystem operations through a JSON-RPC API over HTTP.
    This allows remote clients (including FUSE mounts) to access Nexus over the network.

    The server provides direct endpoints for all NFS methods:
    - read, write, delete, exists
    - list, glob, grep
    - mkdir, rmdir, is_directory

    Examples:
        # Start server with local backend (no authentication)
        nexus serve

        # First-time setup with database auth (creates admin user & API key)
        nexus serve --auth-type database --init

        # Clean setup for testing/demos (reset DB + init)
        nexus serve --auth-type database --init --reset

        # Restart server (already initialized)
        nexus serve --auth-type database

        # Custom port and admin user
        nexus serve --auth-type database --init --port 8080 --admin-user alice

        # Connect from Python
        from nexus.remote import RemoteNexusFS
        nx = RemoteNexusFS("http://localhost:8080", api_key="<admin-key>")
        nx.write("/workspace/file.txt", b"Hello, World!")

        # Mount with FUSE
        from nexus.fuse import mount_nexus
        mount_nexus(nx, "/mnt/nexus")
    """
    import logging
    import os
    import subprocess

    # Set up logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    try:
        # ============================================
        # Validation
        # ============================================
        if reset and not init:
            console.print("[red]Error:[/red] --reset requires --init flag")
            console.print(
                "[yellow]Hint:[/yellow] Use: nexus serve --auth-type database --init --reset"
            )
            sys.exit(1)

        if init and auth_type != "database":
            console.print("[red]Error:[/red] --init requires --auth-type database")
            console.print("[yellow]Hint:[/yellow] Use: nexus serve --auth-type database --init")
            sys.exit(1)

        # ============================================
        # Port Cleanup
        # ============================================
        console.print(f"[yellow]Checking port {port}...[/yellow]")

        try:
            # Try to find and kill any process using the port
            import shutil

            if shutil.which("lsof"):
                result = subprocess.run(
                    ["lsof", "-ti", f":{port}"],
                    capture_output=True,
                    text=True,
                )
                if result.stdout.strip():
                    pid = result.stdout.strip()
                    console.print(f"[yellow]⚠️  Port {port} is in use by process {pid}[/yellow]")
                    console.print("[yellow]   Killing process...[/yellow]")
                    subprocess.run(["kill", "-9", pid], check=False)
                    time.sleep(1)
                    console.print(f"[green]✓[/green] Port {port} is now available")
                else:
                    console.print(f"[green]✓[/green] Port {port} is available")
            else:
                # Fallback for systems without lsof
                result = subprocess.run(
                    ["netstat", "-an"],
                    capture_output=True,
                    text=True,
                )
                if f":{port}" in result.stdout and "LISTEN" in result.stdout:
                    console.print(f"[yellow]⚠️  Port {port} appears to be in use[/yellow]")
                    console.print(
                        f"[yellow]   Please manually stop the process using port {port}[/yellow]"
                    )
                else:
                    console.print(f"[green]✓[/green] Port {port} is available")
        except Exception as e:
            console.print(f"[yellow]⚠️  Could not check port status: {e}[/yellow]")

        console.print()

        # Import server components
        from nexus.server.rpc_server import NexusRPCServer

        # Determine authentication configuration first (needed for permissions logic)
        has_auth = bool(auth_type or api_key)

        # Server mode permissions logic:
        # - No auth → enforce_permissions=False (everyone is anonymous)
        # - With auth → enforce_permissions=True (secure by default)
        enforce_permissions = has_auth

        # Get filesystem instance with appropriate permissions
        nx = get_filesystem(backend_config, enforce_permissions=enforce_permissions)

        # Safety check: Server should never use RemoteNexusFS (would create circular dependency)
        from nexus.remote import RemoteNexusFS

        if isinstance(nx, RemoteNexusFS):
            console.print(
                "[red]Error:[/red] Server cannot use RemoteNexusFS (circular dependency detected)"
            )
            console.print("[yellow]Hint:[/yellow] Unset NEXUS_URL environment variable:")
            console.print("  unset NEXUS_URL")
            console.print("  nexus serve ...")
            sys.exit(1)

        # Create authentication provider
        auth_provider = None
        if auth_type == "database":
            # Database authentication - requires database connection
            import os

            from sqlalchemy import create_engine
            from sqlalchemy.orm import sessionmaker

            from nexus.server.auth.factory import create_auth_provider

            db_url = os.getenv("NEXUS_DATABASE_URL")
            if not db_url:
                console.print(
                    "[red]Error:[/red] Database authentication requires NEXUS_DATABASE_URL"
                )
                sys.exit(1)

            engine = create_engine(db_url)
            session_factory = sessionmaker(bind=engine)
            auth_provider = create_auth_provider("database", session_factory=session_factory)
        elif api_key:
            # Simple static API key authentication (backward compatibility)
            # This is the old behavior - just pass api_key to server
            pass
        # Future: add support for other auth types (local, oidc, etc.)

        # ============================================
        # Database Reset (if requested)
        # ============================================
        if reset:
            db_url = os.getenv("NEXUS_DATABASE_URL")
            if not db_url:
                console.print("[red]Error:[/red] NEXUS_DATABASE_URL environment variable not set")
                sys.exit(1)

            console.print("[bold red]⚠️  WARNING: Database Reset[/bold red]")
            console.print("[yellow]This will DELETE ALL existing data:[/yellow]")
            console.print("  • All users and API keys")
            console.print("  • All files and metadata")
            console.print("  • All permissions and relationships")
            console.print()

            from sqlalchemy import create_engine, text

            engine = create_engine(db_url)

            # List of tables to clear (in dependency order)
            tables_to_clear = [
                # Auth tables
                "refresh_tokens",
                "api_keys",
                "users",
                # ReBAC and audit
                "rebac_check_cache",
                "rebac_changelog",
                "admin_bypass_audit",
                "operation_log",
                "rebac_tuples",
                # File tables
                "content_chunks",
                "document_chunks",
                "version_history",
                "file_metadata",
                "file_paths",
                # Memory and workspace
                "memories",
                "memory_configs",
                "workspace_snapshots",
                "workspace_configs",
                # Workflow tables
                "workflow_executions",
                "workflows",
                # Mount configs
                "mount_configs",
            ]

            deleted_counts = {}
            console.print("[yellow]Clearing database tables...[/yellow]")

            for table_name in tables_to_clear:
                try:
                    with engine.connect() as conn:
                        trans = conn.begin()
                        try:
                            cursor_result = conn.execute(text(f"DELETE FROM {table_name}"))
                            count = cursor_result.rowcount
                            trans.commit()
                            deleted_counts[table_name] = count
                            if count > 0:
                                console.print(
                                    f"  [dim]Deleted {count} rows from {table_name}[/dim]"
                                )
                        except Exception:
                            trans.rollback()
                            # Ignore table doesn't exist errors
                            pass
                except Exception:
                    pass

            total = sum(deleted_counts.values())
            if total > 0:
                console.print(
                    f"[green]✓[/green] Cleared {total} total rows from {len(deleted_counts)} tables"
                )
            else:
                console.print("[green]✓[/green] Database was already empty")
            console.print()

            # Clear filesystem data
            data_dir = backend_config.data_dir
            if data_dir and Path(data_dir).exists():
                console.print(f"[yellow]Clearing filesystem data: {data_dir}[/yellow]")
                import shutil

                for item in Path(data_dir).iterdir():
                    if item.is_dir():
                        shutil.rmtree(item)
                    else:
                        item.unlink()
                console.print("[green]✓[/green] Cleared filesystem data")
                console.print()

        # ============================================
        # Initialization (if requested)
        # ============================================
        if init:
            console.print("[bold green]🔧 Initializing Nexus Server[/bold green]")
            console.print()

            # Get database URL
            db_url = os.getenv("NEXUS_DATABASE_URL")
            if not db_url:
                console.print("[red]Error:[/red] NEXUS_DATABASE_URL environment variable not set")
                sys.exit(1)

            # Create admin user and API key
            console.print("[yellow]Creating admin user and API key...[/yellow]")

            from datetime import UTC, datetime, timedelta

            from sqlalchemy import create_engine
            from sqlalchemy.orm import sessionmaker

            from nexus.core.entity_registry import EntityRegistry
            from nexus.server.auth.database_key import DatabaseAPIKeyAuth

            engine = create_engine(db_url)
            Session = sessionmaker(bind=engine)

            # Register user in entity registry (for agent permission inheritance)
            entity_registry = EntityRegistry(Session)
            tenant_id = "default"

            # User might already exist, ignore errors
            with contextlib.suppress(Exception):
                entity_registry.register_entity(
                    entity_type="user",
                    entity_id=admin_user,
                    parent_type="tenant",
                    parent_id=tenant_id,
                )

            # Create API key using DatabaseAPIKeyAuth
            try:
                with Session() as session:
                    # Calculate expiry (90 days)
                    expires_at = datetime.now(UTC) + timedelta(days=90)

                    key_id, admin_api_key = DatabaseAPIKeyAuth.create_key(
                        session,
                        user_id=admin_user,
                        name="Admin key (created by init)",
                        tenant_id=tenant_id,
                        is_admin=True,
                        expires_at=expires_at,
                    )
                    session.commit()

                    console.print(f"[green]✓[/green] Created admin user: {admin_user}")
                    console.print("[green]✓[/green] Created admin API key")

                # Create workspace directory
                console.print()
                console.print("[yellow]Setting up workspace...[/yellow]")

                # Create /workspace directory using direct filesystem access
                # We need to bypass the nx object since permissions are enforced
                # Instead, use the underlying backend directly
                from nexus.backends.local import LocalBackend

                data_dir = backend_config.data_dir
                backend = LocalBackend(data_dir)

                try:
                    # Create /workspace directory directly via backend
                    try:
                        backend.mkdir("/workspace")
                        console.print("[green]✓[/green] Created /workspace")
                    except Exception as mkdir_err:
                        # Directory might already exist, check and ignore
                        if "already exists" in str(mkdir_err).lower():
                            console.print("[green]✓[/green] /workspace already exists")
                        else:
                            raise

                    # Grant admin user ownership
                    from nexus.core.rebac_manager import ReBACManager

                    rebac = ReBACManager(engine)
                    rebac.rebac_write(
                        subject=("user", admin_user),
                        relation="direct_owner",
                        object=("file", "/workspace"),
                        tenant_id="default",
                    )
                    console.print(
                        f"[green]✓[/green] Granted '{admin_user}' ownership of /workspace"
                    )

                except Exception as workspace_err:
                    console.print(
                        f"[yellow]⚠️  Warning: Could not setup workspace: {workspace_err}[/yellow]"
                    )
                    console.print("[yellow]   You may need to manually create /workspace[/yellow]")

                # Display API key
                console.print()
                console.print("[bold cyan]" + "=" * 60 + "[/bold cyan]")
                console.print("[bold green]✅ Initialization Complete![/bold green]")
                console.print("[bold cyan]" + "=" * 60 + "[/bold cyan]")
                console.print()
                console.print("[bold yellow]IMPORTANT: Save this API key securely![/bold yellow]")
                console.print()
                console.print(f"[bold cyan]Admin API Key:[/bold cyan] {admin_api_key}")
                console.print()
                console.print("[yellow]Add to your ~/.bashrc or ~/.zshrc:[/yellow]")
                console.print(f"  export NEXUS_API_KEY='{admin_api_key}'")
                console.print(f"  export NEXUS_URL='http://localhost:{port}'")
                console.print()

                # Save to .nexus-admin-env file
                env_file = Path(".nexus-admin-env")
                env_file.write_text(
                    f"# Nexus Admin Environment\n"
                    f"# Created: {datetime.now()}\n"
                    f"# User: {admin_user}\n"
                    f"export NEXUS_API_KEY='{admin_api_key}'\n"
                    f"export NEXUS_URL='http://localhost:{port}'\n"
                    f"export NEXUS_DATABASE_URL='{db_url}'\n"
                )
                console.print("[green]✓[/green] Saved to .nexus-admin-env")
                console.print()
                console.print("[bold cyan]" + "=" * 60 + "[/bold cyan]")
                console.print()

            except Exception as e:
                console.print(f"[red]Error during initialization:[/red] {e}")
                raise

        # Create and start server
        console.print("[green]Starting Nexus RPC server...[/green]")
        console.print(f"  Host: [cyan]{host}[/cyan]")
        console.print(f"  Port: [cyan]{port}[/cyan]")
        console.print(f"  Backend: [cyan]{backend_config.backend}[/cyan]")
        if backend_config.backend == "gcs":
            console.print(f"  GCS Bucket: [cyan]{backend_config.gcs_bucket}[/cyan]")
        else:
            console.print(f"  Data Dir: [cyan]{backend_config.data_dir}[/cyan]")

        if auth_provider:
            console.print(f"  Authentication: [yellow]{auth_type}[/yellow]")
            console.print("  Permissions: [green]Enabled[/green]")
        elif api_key:
            console.print("  Authentication: [yellow]Static API key[/yellow]")
            console.print("  Permissions: [green]Enabled[/green]")
        else:
            console.print("  Authentication: [yellow]None (open access)[/yellow]")
            console.print("  Permissions: [yellow]Disabled[/yellow]")
            console.print()
            console.print("  [bold red]⚠️  WARNING: No authentication configured[/bold red]")
            console.print(
                "  [yellow]Server is running in open access mode - anyone can read/write files[/yellow]"
            )
            console.print("  [yellow]For production, use: --auth-type database|local|oidc[/yellow]")

        console.print()
        console.print("[bold cyan]Endpoints:[/bold cyan]")
        console.print(f"  Health check: [cyan]http://{host}:{port}/health[/cyan]")
        console.print(f"  RPC methods: [cyan]http://{host}:{port}/api/nfs/{{method}}[/cyan]")
        console.print()
        console.print("[yellow]Connect from Python:[/yellow]")
        console.print("  from nexus.remote import RemoteNexusFS")
        console.print(f'  nx = RemoteNexusFS("http://{host}:{port}"', end="")
        if api_key or auth_provider:
            console.print(', api_key="<your-key>")')
        else:
            console.print(")")
        console.print("  nx.write('/workspace/file.txt', b'Hello!')")
        console.print()
        console.print("[green]Press Ctrl+C to stop server[/green]")

        server = NexusRPCServer(
            nexus_fs=nx,
            host=host,
            port=port,
            api_key=api_key,
            auth_provider=auth_provider,
        )

        server.serve_forever()

    except KeyboardInterrupt:
        console.print("\n[yellow]Server stopped by user[/yellow]")
    except Exception as e:
        handle_error(e)


def register_commands(cli: click.Group) -> None:
    """Register server commands with the CLI.

    Args:
        cli: The Click group to register commands to
    """
    cli.add_command(mount)
    cli.add_command(unmount)
    cli.add_command(serve)
