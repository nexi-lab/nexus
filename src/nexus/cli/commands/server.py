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


def start_background_mount_sync(nx: NexusFilesystem) -> None:
    """Start background thread to sync connector mounts after server is ready.

    This function starts a daemon thread that syncs all connector backends
    (GCS, S3, etc.) without blocking server startup. The sync begins 2 seconds
    after the thread starts to ensure the server is fully initialized.

    Args:
        nx: NexusFilesystem instance to sync mounts from

    Note:
        - Runs in daemon thread (won't prevent server shutdown)
        - Only syncs connector backends (skips local backends)
        - Errors are logged but don't crash the server
    """
    import threading

    def sync_connector_mounts_background() -> None:
        """Background thread worker that performs the actual sync."""
        import time

        time.sleep(2)  # Wait for server to be fully ready
        console.print("[cyan]🔄 Starting background sync for connector mounts...[/cyan]")

        try:
            all_mounts = nx.list_mounts()
            synced_count = 0

            for mount in all_mounts:
                backend_type = mount.get("backend_type", "")
                mount_point = mount.get("mount_point", "")

                # Only sync connector backends (skip local backends)
                if "connector" in backend_type.lower() or backend_type.lower() in ["gcs", "s3"]:
                    try:
                        console.print(f"  Syncing {mount_point} ({backend_type})...")
                        result = nx.sync_mount(mount_point, recursive=True)  # type: ignore[attr-defined]
                        console.print(
                            f"  [green]✓[/green] {mount_point}: {result['files_scanned']} scanned, "
                            f"{result['files_created']} created, "
                            f"{result['files_updated']} updated"
                        )
                        synced_count += 1
                    except Exception as sync_error:
                        console.print(
                            f"  [yellow]⚠️ [/yellow] Failed to sync {mount_point}: {sync_error}"
                        )

            console.print(
                f"[green]✅ Background sync complete! Synced {synced_count} mounts[/green]"
            )

        except Exception as e:
            console.print(f"[yellow]⚠️  Background sync failed: {e}[/yellow]")

    # Start sync in background (daemon=True = non-blocking, won't prevent shutdown)
    threading.Thread(
        target=sync_connector_mounts_background,
        daemon=True,
        name="mount-sync-thread",
    ).start()


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
@click.option(
    "--agent-id",
    type=str,
    default=None,
    help="Agent ID for version attribution (e.g., 'my-agent'). "
    "When set, file modifications will be attributed to this agent.",
)
@add_backend_options
def mount(
    mount_point: str,
    mode: str,
    daemon: bool,
    allow_other: bool,
    debug: bool,
    agent_id: str | None,
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

        # Set agent_id on remote filesystem for version attribution (issue #418)
        # Only RemoteNexusFS has a settable agent_id property
        if agent_id and hasattr(nx, "_agent_id"):
            nx.agent_id = agent_id  # type: ignore[misc]

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
        if agent_id:
            console.print(f"  Agent ID: [cyan]{agent_id}[/cyan]")

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
            # Note: sys is already imported at module level

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
@click.option(
    "--async/--no-async",
    "use_async",
    default=True,
    help="Use async FastAPI server (default: enabled, 10-50x throughput improvement)",
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
    use_async: bool,
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
        # - Check NEXUS_ENFORCE_PERMISSIONS environment variable first
        # - If not set, default: No auth → False, With auth → True
        enforce_permissions_env = os.getenv("NEXUS_ENFORCE_PERMISSIONS", "").lower()
        if enforce_permissions_env in ("false", "0", "no", "off"):
            enforce_permissions = False
            console.print(
                "[yellow]⚠️  Permissions DISABLED by NEXUS_ENFORCE_PERMISSIONS "
                "environment variable[/yellow]"
            )
        elif enforce_permissions_env in ("true", "1", "yes", "on"):
            enforce_permissions = True
            console.print(
                "[green]✓ Permissions ENABLED by NEXUS_ENFORCE_PERMISSIONS "
                "environment variable[/green]"
            )
        else:
            # Default: enable permissions when auth is configured (secure by default)
            enforce_permissions = has_auth
            if has_auth:
                console.print("[green]✓ Permissions enabled (authentication configured)[/green]")

        # Check NEXUS_ALLOW_ADMIN_BYPASS environment variable
        # Default: True for better developer experience
        # Set to "false" explicitly to disable admin bypass for stricter security
        allow_admin_bypass = True
        allow_admin_bypass_env = os.getenv("NEXUS_ALLOW_ADMIN_BYPASS", "").lower()
        if allow_admin_bypass_env in ("false", "0", "no", "off"):
            allow_admin_bypass = False
            console.print(
                "[yellow]⚠️  Admin bypass DISABLED by NEXUS_ALLOW_ADMIN_BYPASS=false[/yellow]"
            )

        # IMPORTANT: Server must always use local NexusFS, never RemoteNexusFS
        # Use force_local=True to prevent circular dependency even if NEXUS_URL is set
        nx = get_filesystem(
            backend_config,
            enforce_permissions=enforce_permissions,
            force_local=True,  # Force local mode to prevent RemoteNexusFS
            allow_admin_bypass=allow_admin_bypass,
        )

        # Load backends from config file if specified
        if backend_config.config_path:
            from pathlib import Path as PathlibPath

            from nexus.cli.utils import create_backend_from_config
            from nexus.config import load_config
            from nexus.core.nexus_fs import NexusFS

            try:
                cfg = load_config(PathlibPath(backend_config.config_path))
                # Store config on NexusFS for OAuth factory and other components
                if isinstance(nx, NexusFS):
                    nx._config = cfg
                if cfg.backends:
                    # Type check: backends can only be mounted on NexusFS, not RemoteNexusFS
                    if not isinstance(nx, NexusFS):
                        console.print(
                            "[yellow]⚠️  Warning: Multi-backend configuration is only supported for local NexusFS instances[/yellow]"
                        )
                    else:
                        console.print()
                        console.print("[bold cyan]Loading backends from config...[/bold cyan]")
                        for backend_def in cfg.backends:
                            backend_type = backend_def.get("type")
                            mount_point = backend_def.get("mount_point")
                            backend_cfg = backend_def.get("config", {})
                            priority = backend_def.get("priority", 0)
                            readonly = backend_def.get("readonly", False)

                            if not backend_type or not mount_point:
                                console.print(
                                    "[yellow]⚠️  Warning: Skipping backend with missing type or mount_point[/yellow]"
                                )
                                continue

                            try:
                                # Check if mount exists in database (takes precedence over config)
                                saved_mount = None
                                if (
                                    mount_point != "/"
                                    and hasattr(nx, "mount_manager")
                                    and nx.mount_manager is not None
                                ):
                                    saved_mount = nx.mount_manager.get_mount(mount_point)

                                if saved_mount is not None:
                                    # Database version exists - it overrides config
                                    # (User may have customized this mount via API/CLI)
                                    console.print(
                                        f"  [dim]→ Mount {mount_point} using database version (overrides config)[/dim]"
                                    )
                                    # Skip config mount - database version already loaded by load_all_saved_mounts()
                                    continue

                                # No database override - use config version
                                # Check if mount already exists in router (shouldn't happen, but be safe)
                                existing_mounts = nx.list_mounts()
                                mount_already_loaded = any(
                                    m["mount_point"] == mount_point for m in existing_mounts
                                )

                                # Create backend instance
                                backend = create_backend_from_config(backend_type, backend_cfg)

                                # Add mount to router
                                nx.router.add_mount(
                                    mount_point,
                                    backend,
                                    priority,
                                    readonly,
                                    replace=mount_already_loaded,
                                )

                                readonly_str = " (read-only)" if readonly else ""
                                console.print(
                                    f"  [green]✓[/green] Mounted {backend_type} backend at {mount_point}{readonly_str}"
                                )

                                # NOTE: Config-defined mounts are NOT automatically saved to database
                                # They serve as defaults. Database is for user customizations (via API/CLI)
                                # If user wants to persist a modified version, they save it via API
                                # Priority: Database (runtime) > Config (default)

                                # Auto-grant permissions to admin user for this mount point
                                # This ensures the admin can list/read/write files in config-mounted backends
                                if mount_point != "/":  # Skip root mount (already has permissions)
                                    try:
                                        # Grant direct_owner to admin for full access
                                        nx.rebac_create(
                                            subject=("user", admin_user),
                                            relation="direct_owner",
                                            object=("file", mount_point),
                                        )
                                        console.print(
                                            f"    [dim]→ Granted {admin_user} permissions to {mount_point}[/dim]"
                                        )
                                    except Exception as perm_error:
                                        console.print(
                                            f"    [yellow]⚠️  Could not grant permissions to {mount_point}: {perm_error}[/yellow]"
                                        )

                                    # Auto-sync connector backends to discover existing files
                                    # Connector backends provide direct path mapping to external storage
                                    if "connector" in backend_type.lower() and hasattr(
                                        backend, "list_dir"
                                    ):
                                        try:
                                            console.print(
                                                f"    [dim]→ Syncing metadata from {backend_type}...[/dim]"
                                            )
                                            sync_result = nx.sync_mount(mount_point, recursive=True)
                                            if sync_result["files_added"] > 0:
                                                console.print(
                                                    f"    [dim]→ Discovered {sync_result['files_added']} files[/dim]"
                                                )
                                            if sync_result["errors"]:
                                                console.print(
                                                    f"    [yellow]⚠️  Sync errors: {len(sync_result['errors'])}[/yellow]"
                                                )
                                        except Exception as sync_error:
                                            console.print(
                                                f"    [yellow]⚠️  Could not sync {mount_point}: {sync_error}[/yellow]"
                                            )
                            except Exception as e:
                                console.print(
                                    f"[yellow]⚠️  Warning: Failed to mount {backend_type} at {mount_point}: {e}[/yellow]"
                                )
                                continue
            except Exception as e:
                console.print(
                    f"[yellow]⚠️  Warning: Could not load backends from config: {e}[/yellow]"
                )

        # Safety check: Server should never use RemoteNexusFS (would create circular dependency)
        # This should never trigger due to NEXUS_URL clearing above, but kept as defensive check
        from nexus.remote import RemoteNexusFS

        if isinstance(nx, RemoteNexusFS):
            console.print(
                "[red]Error:[/red] Server cannot use RemoteNexusFS (circular dependency detected)"
            )
            console.print("[yellow]This is unexpected - please report this bug.[/yellow]")
            console.print("[yellow]Workaround:[/yellow] Unset NEXUS_URL environment variable:")
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

        elif auth_type == "local":
            # Local username/password authentication with JWT tokens
            import os

            from sqlalchemy import create_engine
            from sqlalchemy.orm import sessionmaker

            from nexus.server.auth.factory import create_auth_provider

            db_url = os.getenv("NEXUS_DATABASE_URL")
            if not db_url:
                console.print("[red]Error:[/red] Local authentication requires NEXUS_DATABASE_URL")
                sys.exit(1)

            jwt_secret = os.getenv("NEXUS_JWT_SECRET")
            if not jwt_secret:
                console.print(
                    "[yellow]⚠️  Warning:[/yellow] NEXUS_JWT_SECRET not set, generating random secret"
                )
                console.print(
                    "[yellow]   For production, set: export NEXUS_JWT_SECRET='your-secret-key'[/yellow]"
                )
                import secrets

                jwt_secret = secrets.token_urlsafe(32)

            engine = create_engine(db_url)
            session_factory = sessionmaker(bind=engine)
            auth_provider = create_auth_provider(
                "local", session_factory=session_factory, jwt_secret=jwt_secret
            )
            console.print("[green]✓[/green] Local authentication enabled (username/password + JWT)")

        elif auth_type == "oidc":
            # Single OIDC provider authentication
            import os

            from nexus.server.auth.factory import create_auth_provider

            oidc_issuer = os.getenv("NEXUS_OIDC_ISSUER")
            oidc_audience = os.getenv("NEXUS_OIDC_AUDIENCE")

            if not oidc_issuer or not oidc_audience:
                console.print("[red]Error:[/red] OIDC authentication requires:")
                console.print("  export NEXUS_OIDC_ISSUER='https://accounts.google.com'")
                console.print("  export NEXUS_OIDC_AUDIENCE='your-client-id'")
                sys.exit(1)

            auth_provider = create_auth_provider("oidc", issuer=oidc_issuer, audience=oidc_audience)
            console.print(f"[green]✓[/green] OIDC authentication enabled (issuer: {oidc_issuer})")

        elif auth_type == "multi-oidc":
            # Multiple OIDC providers (Google, Microsoft, GitHub, etc.)
            # Load provider configs from environment
            # Format: NEXUS_OIDC_PROVIDERS='{"google":{"issuer":"...","audience":"..."},...}'
            import json
            import os

            from nexus.server.auth.factory import create_auth_provider

            oidc_providers_json = os.getenv("NEXUS_OIDC_PROVIDERS")
            if not oidc_providers_json:
                console.print("[red]Error:[/red] Multi-OIDC authentication requires:")
                console.print(
                    '  export NEXUS_OIDC_PROVIDERS=\'{"google":{"issuer":"...","audience":"..."}}\''
                )
                sys.exit(1)

            try:
                providers = json.loads(oidc_providers_json)
            except json.JSONDecodeError as e:
                console.print(f"[red]Error:[/red] Invalid NEXUS_OIDC_PROVIDERS JSON: {e}")
                sys.exit(1)

            auth_provider = create_auth_provider("multi-oidc", providers=providers)
            console.print(
                f"[green]✓[/green] Multi-OIDC authentication enabled ({len(providers)} providers)"
            )

        elif auth_type == "static":
            # Static API key authentication (deprecated, use database instead)
            from nexus.server.auth.factory import create_auth_provider

            if not api_key:
                console.print("[red]Error:[/red] Static authentication requires --api-key")
                console.print(
                    "[yellow]Hint:[/yellow] Use: nexus serve --auth-type static --api-key 'your-key'"
                )
                sys.exit(1)

            auth_provider = create_auth_provider("static", api_key=api_key)
            console.print("[yellow]⚠️  Static API key authentication (deprecated)[/yellow]")
            console.print("[yellow]   Consider using --auth-type database for production[/yellow]")

        elif api_key:
            # Backward compatibility: --api-key without --auth-type defaults to static
            from nexus.server.auth.factory import create_auth_provider

            auth_provider = create_auth_provider("static", api_key=api_key)
            console.print("[yellow]⚠️  Using static API key authentication (deprecated)[/yellow]")
            console.print(
                "[yellow]   Consider using: nexus serve --auth-type database --init[/yellow]"
            )

        elif auth_type:
            console.print(f"[red]Error:[/red] Unknown auth type: {auth_type}")
            sys.exit(1)

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
                "oauth_credentials",  # OAuth tokens (v0.7.0)
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

        # ============================================
        # Cache Warming (Optional Performance Optimization)
        # ============================================
        # Warm up caches to improve first-request performance
        # This preloads commonly accessed paths and permissions
        start_time = time.time()

        console.print("[yellow]Warming caches...[/yellow]", end="")

        cache_stats_before = None
        if (
            hasattr(nx, "_rebac_manager")
            and nx._rebac_manager
            and hasattr(nx._rebac_manager, "get_cache_stats")
        ):
            with contextlib.suppress(Exception):
                cache_stats_before = nx._rebac_manager.get_cache_stats()

        warmed_count = 0
        try:
            # Warm up common paths (non-blocking, best effort)
            common_paths = ["/", "/workspace", "/tmp", "/data"]
            for path in common_paths:
                with contextlib.suppress(Exception):
                    # Check if path exists and warm permission cache
                    if nx.exists(path):
                        # List directory to warm listing cache
                        with contextlib.suppress(Exception):
                            nx.list(path, recursive=False, details=False)
                            warmed_count += 1

            elapsed = time.time() - start_time
            console.print(f" [green]✓[/green] ({warmed_count} paths, {elapsed:.2f}s)")

            # Show cache stats if available
            if cache_stats_before:
                with contextlib.suppress(Exception):
                    cache_stats_after = nx._rebac_manager.get_cache_stats()  # type: ignore[attr-defined]
                    l2_before = cache_stats_before.get("l2_size", 0)
                    l2_after = cache_stats_after.get("l2_size", 0)
                    l2_warmed = l2_after - l2_before

                    if l2_warmed > 0:
                        console.print(f"  [dim]L2 permission cache: +{l2_warmed} entries[/dim]")

        except Exception as e:
            console.print(f" [yellow]⚠ [/yellow] ({str(e)})")

        console.print()
        console.print("[green]Press Ctrl+C to stop server[/green]")

        if use_async:
            # Use FastAPI async server
            from nexus.server.fastapi_server import create_app, run_server

            console.print()
            console.print("[bold cyan]🚀 Using FastAPI async server[/bold cyan]")
            console.print("  [dim]10-50x throughput improvement under concurrent load[/dim]")

            # Get database URL for async operations
            database_url = os.getenv("NEXUS_DATABASE_URL")

            app = create_app(
                nexus_fs=nx,  # type: ignore[arg-type]
                api_key=api_key,
                auth_provider=auth_provider,
                database_url=database_url,
            )

            # Start background sync for connector mounts (non-blocking)
            start_background_mount_sync(nx)

            run_server(app, host=host, port=port, log_level="info")
        else:
            # Use traditional ThreadingHTTPServer
            server = NexusRPCServer(
                nexus_fs=nx,
                host=host,
                port=port,
                api_key=api_key,
                auth_provider=auth_provider,
            )

            # Start background sync for connector mounts (non-blocking)
            start_background_mount_sync(nx)

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
