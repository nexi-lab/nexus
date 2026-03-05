"""Nexus CLI Server Commands - Mount, unmount, and serve commands.

This module contains server-related CLI commands for:
- Mounting Nexus filesystem with FUSE
- Unmounting FUSE mounts
- Starting the Nexus RPC server
"""

import logging
import sys
import time
from pathlib import Path
from typing import Any, cast

import click

from nexus import NexusFilesystem
from nexus.cli.utils import (
    BackendConfig,
    add_backend_options,
    console,
    get_filesystem,
    handle_error,
)
from nexus.lib.env import get_database_url
from nexus.lib.sync_bridge import run_sync

logger = logging.getLogger(__name__)


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

        from nexus.lib.sync_bridge import run_sync

        time.sleep(2)  # Wait for server to be fully ready
        console.print("[cyan]🔄 Starting background sync for connector mounts...[/cyan]")

        try:
            mount_svc = cast(Any, nx).mount_service
            all_mounts = run_sync(mount_svc.list_mounts())
            synced_count = 0

            for mount in all_mounts:
                backend_type = mount.get("backend_type", "")
                mount_point = mount.get("mount_point", "")

                # Only sync connector backends (skip local backends)
                if "connector" in backend_type.lower() or backend_type.lower() in ["gcs", "s3"]:
                    try:
                        console.print(f"  Syncing {mount_point} ({backend_type})...")
                        result = run_sync(mount_svc.sync_mount(mount_point, recursive=True))
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


def _is_federation_syntax(source: str, target: str | None) -> bool:
    """Detect federation mount: 2 args with at least one containing ':'."""
    if target is None:
        return False
    return ":" in source or ":" in target


@click.command(name="mount")
@click.argument("source", type=str)
@click.argument("target", type=str, required=False, default=None)
# --- FUSE options ---
@click.option(
    "--mode",
    type=click.Choice(["binary", "text", "smart"]),
    default="smart",
    help="[FUSE] Mount mode: binary (raw), text (parsed), smart (auto-detect)",
    show_default=True,
)
@click.option(
    "--daemon",
    is_flag=True,
    help="[FUSE] Run in background (daemon mode)",
)
@click.option(
    "--allow-other",
    is_flag=True,
    help="[FUSE] Allow other users to access the mount",
)
@click.option(
    "--debug",
    is_flag=True,
    help="Enable debug output",
)
@click.option(
    "--agent-id",
    type=str,
    default=None,
    help="[FUSE] Agent ID for version attribution",
)
# --- Federation options ---
@click.option(
    "--node-id",
    type=int,
    envvar="NEXUS_NODE_ID",
    default=1,
    show_default=True,
    help="[Federation] This node's unique Raft ID",
)
@click.option(
    "--data-dir",
    type=click.Path(),
    envvar="NEXUS_DATA_DIR",
    default="./nexus-data/zones",
    show_default=True,
    help="[Federation] Base directory for zone databases",
)
@click.option(
    "--bind",
    type=str,
    envvar="NEXUS_BIND_ADDR",
    default="0.0.0.0:2126",
    show_default=True,
    help="[Federation] gRPC bind address",
)
@click.option(
    "--tls-cert",
    type=click.Path(exists=True),
    envvar="NEXUS_TLS_CERT",
    default=None,
    help="[Federation] TLS certificate PEM file (mTLS)",
)
@click.option(
    "--tls-key",
    type=click.Path(exists=True),
    envvar="NEXUS_TLS_KEY",
    default=None,
    help="[Federation] TLS private key PEM file (mTLS)",
)
@click.option(
    "--tls-ca",
    type=click.Path(exists=True),
    envvar="NEXUS_TLS_CA",
    default=None,
    help="[Federation] TLS CA certificate PEM file (mTLS)",
)
@add_backend_options
def mount(
    source: str,
    target: str | None,
    mode: str,
    daemon: bool,
    allow_other: bool,
    debug: bool,
    agent_id: str | None,
    node_id: int,
    data_dir: str,
    bind: str,
    tls_cert: str | None,
    tls_key: str | None,
    tls_ca: str | None,
    backend_config: BackendConfig,
) -> None:
    """Mount Nexus filesystem (FUSE or federation).

    \b
    FUSE mode (1 argument):
        nexus mount /mnt/nexus
        nexus mount /mnt/nexus --mode=binary --daemon

    \b
    Federation mode (2 arguments with peer:path):
        nexus mount /local peer:/remote       # share subtree with peer
        nexus mount peer:/remote /local       # join peer's shared subtree
    """
    if _is_federation_syntax(source, target):
        _mount_federation(source, target, node_id, data_dir, bind, tls_cert, tls_key, tls_ca)
    else:
        _mount_fuse(source, mode, daemon, allow_other, debug, agent_id, backend_config)


def _mount_federation(
    source: str,
    target: str | None,
    node_id: int,
    data_dir: str,
    bind: str,
    tls_cert: str | None = None,
    tls_key: str | None = None,
    tls_ca: str | None = None,
) -> None:
    """Federation mount — lazy imports nexus.raft to stay decoupled from FUSE."""
    import asyncio

    assert target is not None

    # Parse: which arg has the colon?
    if ":" in source and ":" not in target:
        # nexus mount peer:/remote /local → join
        peer_addr, remote_path = source.split(":", 1)
        local_path = target
        flow = "join"
    elif ":" in target and ":" not in source:
        # nexus mount /local peer:/remote → share
        peer_addr, remote_path = target.split(":", 1)
        local_path = source
        flow = "share"
    else:
        console.print("[red]Error:[/red] Exactly one argument must use peer:path syntax")
        console.print("  Share: nexus mount /local peer:/remote")
        console.print("  Join:  nexus mount peer:/remote /local")
        sys.exit(1)

    try:
        from nexus.raft.federation import NexusFederation
        from nexus.raft.zone_manager import ZoneManager

        mgr = ZoneManager(
            node_id=node_id,
            base_path=data_dir,
            bind_addr=bind,
            tls_cert_path=tls_cert,
            tls_key_path=tls_key,
            tls_ca_path=tls_ca,
        )
        mgr.bootstrap()

        fed = NexusFederation(zone_manager=mgr)

        if flow == "share":
            console.print(f"[cyan]Sharing[/cyan] {local_path}")
            zone_id = asyncio.run(fed.share(local_path))
            console.print(f"[green]Shared as zone '{zone_id}'[/green]")
            console.print(f"  Local: {local_path} → DT_MOUNT → {zone_id}")
            console.print(
                f"  Peer can join with: nexus mount <this-node>:{local_path} <mount-point>"
            )
        else:
            console.print(f"[cyan]Joining[/cyan] {peer_addr}:{remote_path} → {local_path}")
            zone_id = asyncio.run(fed.join(peer_addr, remote_path, local_path))
            console.print(f"[green]Joined zone '{zone_id}'[/green]")
            console.print(f"  Mounted at: {local_path}")

        mgr.shutdown()
    except ImportError:
        console.print(
            "[red]Error:[/red] Federation requires PyO3 build with --features full.\n"
            "Build with: maturin develop -m rust/nexus_raft/Cargo.toml --features full"
        )
        sys.exit(1)
    except Exception as e:
        handle_error(e)


def _mount_fuse(
    mount_point: str,
    mode: str,
    daemon: bool,
    allow_other: bool,
    debug: bool,
    agent_id: str | None,
    backend_config: BackendConfig,
) -> None:
    """FUSE mount — lazy imports nexus.fuse to stay decoupled from federation."""
    try:
        from nexus.fuse import mount_nexus

        # Get filesystem instance (handles both remote and local backends)
        nx: NexusFilesystem = get_filesystem(backend_config)

        # Set agent_id on remote filesystem for version attribution (issue #418)
        # Only REMOTE profile NexusFS has a settable agent_id property
        if agent_id and hasattr(nx, "_agent_id"):
            nx.agent_id = agent_id  # type: ignore[attr-defined]  # allowed

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

            # Configure logging to file with secret redaction (Issue #86)
            from nexus.server.logging_processors import RedactingFormatter

            _fuse_formatter = RedactingFormatter(
                "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
            )
            _fuse_handler = logging.FileHandler(log_file)
            _fuse_handler.setFormatter(_fuse_formatter)
            logging.root.handlers.clear()
            logging.root.addHandler(_fuse_handler)
            logging.root.setLevel(logging.DEBUG if debug else logging.INFO)

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
@click.option("--port", default=2026, type=int, help="Server port (default: 2026)")
@click.option(
    "--profile",
    type=click.Choice(["minimal", "embedded", "lite", "full", "cloud", "remote", "auto"]),
    default=None,
    envvar="NEXUS_PROFILE",
    help="Deployment profile (minimal, embedded, lite, full, cloud, remote, auto)",
)
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
    "--enable-memory-paging/--no-memory-paging",
    "enable_memory_paging",
    default=True,
    help="Enable MemGPT 3-tier memory paging (Issue #1258, default: enabled)",
)
@click.option(
    "--memory-main-capacity",
    default=100,
    type=int,
    help="Main context capacity for memory paging (default: 100)",
)
@click.option(
    "--memory-recall-max-age-hours",
    default=24.0,
    type=float,
    help="Max age in hours before recall→archival paging (default: 24.0)",
)
@add_backend_options
def serve(
    host: str,
    port: int,
    profile: str | None,
    api_key: str | None,
    auth_type: str | None,
    init: bool,
    reset: bool,
    admin_user: str,
    enable_memory_paging: bool,
    memory_main_capacity: int,
    memory_recall_max_age_hours: float,
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
        nexus serve --auth-type database --init --port 2026 --admin-user alice

        # Connect from Python
        import nexus
        nx = nexus.connect(config={"mode": "remote", "url": "http://localhost:2026", "api_key": "<admin-key>"})
        nx.sys_write("/workspace/file.txt", b"Hello, World!")

        # Mount with FUSE
        from nexus.fuse import mount_nexus
        mount_nexus(nx, "/mnt/nexus")
    """
    import os
    import subprocess

    from nexus.server.logging_config import configure_logging

    # Issue #2194: Propagate --profile to environment so factory picks it up
    if profile is not None:
        os.environ["NEXUS_PROFILE"] = profile
        logger.info("Deployment profile set via CLI: %s", profile)

    # Set up structured logging with secret redaction (Issue #86 + #1002)
    # configure_logging() reads NEXUS_LOG_REDACTION_ENABLED env var internally
    configure_logging(env=os.getenv("NEXUS_ENV", "dev"), log_level="INFO")

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
        # Port Check (do not automatically kill)
        # ============================================
        try:
            # Check if port is in use (only LISTEN state, not stale connections)
            import shutil

            if shutil.which("lsof"):
                result = subprocess.run(
                    ["lsof", "-ti", f":{port}", "-sTCP:LISTEN"],
                    capture_output=True,
                    text=True,
                )
                if result.stdout.strip():
                    pids = result.stdout.strip().split("\n")
                    console.print(f"[red]ERROR: Port {port} is already in use[/red]")
                    console.print()

                    # Show detailed process info
                    console.print(f"Process(es) running on port {port}:")
                    proc_result = subprocess.run(
                        ["lsof", "-i", f":{port}", "-sTCP:LISTEN"],
                        capture_output=True,
                        text=True,
                    )
                    if proc_result.stdout:
                        # Skip header line
                        for line in proc_result.stdout.split("\n")[1:]:
                            if line.strip():
                                console.print(f"  {line}")

                    console.print()
                    console.print("Stop the server first using one of these commands:")
                    console.print(
                        "   ./scripts/local-demo.sh --stop              # Stop local Nexus server"
                    )
                    console.print(
                        "   ./scripts/docker-demo.sh --stop             # Stop Docker-based server"
                    )
                    console.print()
                    console.print("Or manually kill the process(es):")
                    for pid in pids:
                        if pid.strip():
                            console.print(f"   kill {pid.strip()}          # Graceful shutdown")
                            console.print(
                                f"   kill -9 {pid.strip()}       # Force kill (if needed)"
                            )
                    console.print()
                    sys.exit(1)
            else:
                # Fallback for systems without lsof
                result = subprocess.run(
                    ["netstat", "-an"],
                    capture_output=True,
                    text=True,
                )
                if f":{port}" in result.stdout and "LISTEN" in result.stdout:
                    console.print(f"[red]ERROR: Port {port} is already in use[/red]")
                    console.print()
                    console.print("Stop the server first or manually kill the process")
                    console.print()
                    sys.exit(1)
        except Exception as e:
            console.print(f"[yellow]⚠️  Could not check port status: {e}[/yellow]")

        console.print()

        # Determine authentication configuration first (needed for permissions logic)
        has_auth = bool(auth_type or api_key)

        # Load config file early if specified to read permission settings
        cfg = None
        if backend_config.config_path:
            from pathlib import Path as PathlibPath

            from nexus.config import load_config

            try:
                cfg = load_config(PathlibPath(backend_config.config_path))
                console.print(f"[dim]→ Config loaded from {backend_config.config_path}[/dim]")
                console.print(
                    f"[dim]  enforce_permissions={getattr(cfg, 'enforce_permissions', 'NOT SET')}[/dim]"
                )
                console.print(
                    f"[dim]  enforce_zone_isolation={getattr(cfg, 'enforce_zone_isolation', 'NOT SET')}[/dim]"
                )
            except Exception as e:
                console.print(f"[yellow]⚠️  Warning: Failed to load config file: {e}[/yellow]")

        # Server mode permissions logic:
        # Priority: 1. Environment variable, 2. Config file, 3. Default (has_auth)
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
        elif cfg and hasattr(cfg, "enforce_permissions"):
            # Use config file value
            enforce_permissions = cfg.enforce_permissions
            console.print(
                f"[{'yellow' if not enforce_permissions else 'green'}]"
                f"{'⚠️  Permissions DISABLED' if not enforce_permissions else '✓ Permissions ENABLED'} "
                f"by config file[/{'yellow' if not enforce_permissions else 'green'}]"
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

        # Check NEXUS_ENFORCE_ZONE_ISOLATION environment variable
        # Priority: 1. Environment variable, 2. Config file, 3. Default (True)
        enforce_zone_isolation_env = os.getenv("NEXUS_ENFORCE_ZONE_ISOLATION", "").lower()
        if enforce_zone_isolation_env in ("false", "0", "no", "off"):
            enforce_zone_isolation = False
            console.print(
                "[yellow]⚠️  Zone isolation DISABLED by NEXUS_ENFORCE_ZONE_ISOLATION=false[/yellow]"
            )
            console.print("[yellow]   WARNING: Cross-zone data access is now possible![/yellow]")
        elif enforce_zone_isolation_env in ("true", "1", "yes", "on"):
            enforce_zone_isolation = True
            console.print("[green]✓ Zone isolation ENABLED by NEXUS_ENFORCE_ZONE_ISOLATION[/green]")
        elif cfg and hasattr(cfg, "enforce_zone_isolation"):
            # Use config file value
            enforce_zone_isolation = cfg.enforce_zone_isolation
            console.print(
                f"[{'yellow' if not enforce_zone_isolation else 'green'}]"
                f"{'⚠️  Zone isolation DISABLED' if not enforce_zone_isolation else '✓ Zone isolation ENABLED'} "
                f"by config file[/{'yellow' if not enforce_zone_isolation else 'green'}]"
            )
            if not enforce_zone_isolation:
                console.print(
                    "[yellow]   WARNING: Cross-zone data access is now possible![/yellow]"
                )
        else:
            # Default: enable zone isolation for security
            enforce_zone_isolation = True

        # Determine server deployment mode from NEXUS_MODE env var
        # Server always runs local NexusFS (never REMOTE profile)
        raw_server_mode = os.getenv("NEXUS_MODE", "standalone")

        if raw_server_mode == "remote":
            console.print(
                "[red]Error:[/red] Server cannot run in mode='remote' "
                "(a server cannot be a thin client of another server)"
            )
            sys.exit(1)

        if raw_server_mode not in ("standalone", "federation"):
            console.print(f"[red]Error:[/red] Unknown NEXUS_MODE: '{raw_server_mode}'")
            console.print("[yellow]Allowed values:[/yellow] standalone, federation")
            sys.exit(1)

        console.print(f"  Mode: [cyan]{raw_server_mode}[/cyan]")
        _active_profile = os.getenv("NEXUS_PROFILE", "full")
        console.print(f"  Profile: [cyan]{_active_profile}[/cyan]")

        nx = get_filesystem(
            backend_config,
            enforce_permissions=enforce_permissions,
            server_mode=raw_server_mode,
            allow_admin_bypass=allow_admin_bypass,
            enforce_zone_isolation=enforce_zone_isolation,
            enable_memory_paging=enable_memory_paging,
            memory_main_capacity=memory_main_capacity,
            memory_recall_max_age_hours=memory_recall_max_age_hours,
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
                    # Type check: backends can only be mounted on NexusFS, not remote NexusFS
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
                                # Create backend instance with record store for caching
                                backend = create_backend_from_config(
                                    backend_type,
                                    backend_cfg,
                                    record_store=nx._record_store,
                                )

                                # Add mount to router
                                nx.router.add_mount(
                                    mount_point,
                                    backend,
                                    readonly=readonly,
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
                                        nx.rebac_service.rebac_create_sync(
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
                                            _mount_svc = cast(Any, nx).mount_service
                                            console.print(
                                                f"    [dim]→ Syncing metadata from {backend_type}...[/dim]"
                                            )
                                            sync_result = run_sync(
                                                _mount_svc.sync_mount(mount_point, recursive=True)
                                            )
                                            if sync_result["files_created"] > 0:
                                                console.print(
                                                    f"    [dim]→ Discovered {sync_result['files_created']} files[/dim]"
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

        # Safety check: Server should never use a remote NexusFS (would create circular dependency)
        # This should never trigger due to NEXUS_URL clearing above, but kept as defensive check
        from nexus.storage.remote_metastore import RemoteMetastore

        if hasattr(nx, "metadata") and isinstance(nx.metadata, RemoteMetastore):
            console.print(
                "[red]Error:[/red] Server cannot use remote NexusFS (circular dependency detected)"
            )
            console.print("[yellow]This is unexpected - please report this bug.[/yellow]")
            console.print("[yellow]Workaround:[/yellow] Unset NEXUS_URL environment variable:")
            console.print("  unset NEXUS_URL")
            console.print("  nexus serve ...")
            sys.exit(1)

        # Create authentication provider
        from nexus.bricks.auth.providers.base import AuthProvider

        auth_provider: AuthProvider | None = None
        if auth_type == "database":
            # Database authentication with both API keys (sk-*) and JWT tokens
            import os

            from nexus.bricks.auth.providers.database_key import DatabaseAPIKeyAuth
            from nexus.bricks.auth.providers.database_local import DatabaseLocalAuth
            from nexus.factory._record_store import create_record_store
            from nexus.server.auth.factory import DiscriminatingAuthProvider

            db_url = get_database_url()
            if not db_url:
                console.print(
                    "[red]Error:[/red] Database authentication requires NEXUS_DATABASE_URL"
                )
                sys.exit(1)

            jwt_secret = os.getenv("NEXUS_JWT_SECRET")
            if not jwt_secret:
                import secrets

                jwt_secret = secrets.token_urlsafe(32)
                console.print(
                    "[yellow]⚠️  Warning:[/yellow] NEXUS_JWT_SECRET not set, using auto-generated secret"
                )
                console.print(
                    "[yellow]   For production, set: export NEXUS_JWT_SECRET='your-secret-key'[/yellow]"
                )

            _record_store = create_record_store(db_url=db_url)
            session_factory = _record_store.session_factory

            # Create composite provider that routes tokens to appropriate handler
            auth_provider = DiscriminatingAuthProvider(
                api_key_provider=DatabaseAPIKeyAuth(record_store=_record_store),
                jwt_provider=DatabaseLocalAuth(
                    session_factory=session_factory,
                    jwt_secret=jwt_secret,
                    token_expiry=3600,
                ),
            )
            console.print(
                "[green]✓[/green] Database authentication enabled (API keys + username/password)"
            )

        elif auth_type == "local":
            # Local username/password authentication with JWT tokens (database-backed)
            import os

            from nexus.bricks.auth.providers.database_local import DatabaseLocalAuth
            from nexus.factory._record_store import create_record_store

            db_url = get_database_url()
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

            _record_store = create_record_store(db_url=db_url)
            session_factory = _record_store.session_factory
            # Use DatabaseLocalAuth directly (not LocalAuth) for user registration/login endpoints
            auth_provider = DatabaseLocalAuth(
                session_factory=session_factory,
                jwt_secret=jwt_secret,
                token_expiry=3600,
            )
            console.print("[green]✓[/green] Local authentication enabled (username/password + JWT)")

        elif auth_type == "oidc":
            # Single OIDC provider authentication
            import os

            from nexus.server.auth.factory import create_auth_provider  # stays in server

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

            from nexus.server.auth.factory import create_auth_provider  # stays in server

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
            from nexus.server.auth.factory import create_auth_provider  # stays in server

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
            from nexus.server.auth.factory import create_auth_provider  # stays in server

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
            db_url = get_database_url()
            if not db_url:
                console.print("[red]Error:[/red] NEXUS_DATABASE_URL environment variable not set")
                sys.exit(1)

            console.print("[bold red]⚠️  WARNING: Database Reset[/bold red]")
            console.print("[yellow]This will DELETE ALL existing data:[/yellow]")
            console.print("  • All users and API keys")
            console.print("  • All files and metadata")
            console.print("  • All permissions and relationships")
            console.print()

            from sqlalchemy import table

            from nexus.factory._record_store import create_record_store

            _record_store = create_record_store(db_url=db_url)
            engine = _record_store.engine

            # List of tables to clear (in dependency order).
            # Uses SQLAlchemy table() construct — never f-string SQL.
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

            for name in tables_to_clear:
                try:
                    tbl = table(name)
                    with engine.connect() as conn:
                        trans = conn.begin()
                        try:
                            cursor_result = conn.execute(tbl.delete())
                            count = cursor_result.rowcount
                            trans.commit()
                            deleted_counts[name] = count
                            if count > 0:
                                console.print(f"  [dim]Deleted {count} rows from {name}[/dim]")
                        except Exception:
                            trans.rollback()
                            # Table might not exist — non-fatal
                except Exception as e:
                    console.print(f"  [dim]DB cleanup error: {e}[/dim]")

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
            db_url = get_database_url()
            if not db_url:
                console.print("[red]Error:[/red] NEXUS_DATABASE_URL environment variable not set")
                sys.exit(1)

            # Create admin user and API key
            console.print("[yellow]Creating admin user and API key...[/yellow]")

            from datetime import UTC, datetime, timedelta

            from nexus.bricks.auth.providers.database_key import DatabaseAPIKeyAuth
            from nexus.bricks.rebac.entity_registry import EntityRegistry
            from nexus.contracts.constants import ROOT_ZONE_ID
            from nexus.factory._record_store import create_record_store

            _record_store = create_record_store(db_url=db_url)
            Session = _record_store.session_factory

            # Register user in entity registry (for agent permission inheritance)
            entity_registry = EntityRegistry(_record_store)
            zone_id = ROOT_ZONE_ID

            # User might already exist, ignore errors
            try:
                entity_registry.register_entity(
                    entity_type="user",
                    entity_id=admin_user,
                    parent_type="zone",
                    parent_id=zone_id,
                )
            except Exception as e:
                logger.debug("Entity registration skipped (may already exist): %s", e)

            # Create API key using DatabaseAPIKeyAuth
            try:
                with Session() as session:
                    # Calculate expiry (90 days)
                    expires_at = datetime.now(UTC) + timedelta(days=90)

                    key_id, admin_api_key = DatabaseAPIKeyAuth.create_key(
                        session,
                        user_id=admin_user,
                        name="Admin key (created by init)",
                        zone_id=zone_id,
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
                    from nexus.bricks.rebac.manager import (
                        ReBACManager,
                    )

                    rebac = ReBACManager(engine)
                    rebac.rebac_write(
                        subject=("user", admin_user),
                        relation="direct_owner",
                        object=("file", "/workspace"),
                        zone_id=ROOT_ZONE_ID,
                    )
                    console.print(
                        f"[green]✓[/green] Granted '{admin_user}' ownership of /workspace"
                    )

                except Exception as workspace_err:
                    console.print(
                        f"[yellow]⚠️  Warning: Could not setup workspace: {workspace_err}[/yellow]"
                    )
                    console.print("[yellow]   You may need to manually create /workspace[/yellow]")

                # Create default zones for multi-zone support
                console.print()
                console.print("[yellow]Creating default zones...[/yellow]")
                try:
                    from nexus.bricks.auth.zone_helpers import create_zone as _create_zone

                    _default_zones = [
                        ("corp", "Default Organization"),
                        ("corp-eng", "Engineering Team"),
                    ]
                    with Session() as _zsession:
                        for _zid, _zname in _default_zones:
                            try:
                                _create_zone(session=_zsession, zone_id=_zid, name=_zname)
                                console.print(f"[green]✓[/green] Created zone: {_zid}")
                            except ValueError:
                                console.print(f"[green]✓[/green] Zone {_zid} already exists")
                except Exception as zone_err:
                    console.print(
                        f"[yellow]⚠️  Warning: Could not create zones: {zone_err}[/yellow]"
                    )

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
        console.print("  import nexus")
        if api_key or auth_provider:
            console.print(
                f'  nx = nexus.connect(config={{"mode": "remote", "url": "http://{host}:{port}", "api_key": "<your-key>"}})'
            )
        else:
            console.print(
                f'  nx = nexus.connect(config={{"mode": "remote", "url": "http://{host}:{port}"}})'
            )
        console.print("  nx.sys_write('/workspace/file.txt', b'Hello!')")
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
            try:
                cache_stats_before = nx._rebac_manager.get_cache_stats()
            except Exception as e:
                logger.debug("Failed to get cache stats before warmup: %s", e)

        warmed_count = 0
        try:
            # Warm up common paths (non-blocking, best effort)
            common_paths = ["/", "/workspace", "/tmp", "/data"]
            for path in common_paths:
                try:
                    # Check if path exists and warm permission cache
                    if nx.sys_access(path):
                        # List directory to warm listing cache
                        try:
                            nx.sys_readdir(path, recursive=False, details=False)
                            warmed_count += 1
                        except Exception as e:
                            logger.debug("Failed to warm listing cache for %s: %s", path, e)
                except Exception as e:
                    logger.debug("Failed to warm permission cache for %s: %s", path, e)

            elapsed = time.time() - start_time
            console.print(f" [green]✓[/green] ({warmed_count} paths, {elapsed:.2f}s)")

            # Show cache stats if available
            if cache_stats_before:
                try:
                    cache_stats_after = nx._rebac_manager.get_cache_stats()  # type: ignore[attr-defined]
                    l2_before = cache_stats_before.get("l2_size", 0)
                    l2_after = cache_stats_after.get("l2_size", 0)
                    l2_warmed = l2_after - l2_before

                    if l2_warmed > 0:
                        console.print(f"  [dim]L2 permission cache: +{l2_warmed} entries[/dim]")
                except Exception as e:
                    logger.debug("Failed to get cache stats after warmup: %s", e)

        except Exception as e:
            console.print(f" [yellow]⚠ [/yellow] ({str(e)})")

        console.print()
        console.print("[green]Press Ctrl+C to stop server[/green]")

        from nexus.server.fastapi_server import create_app, run_server

        console.print()
        console.print("[bold cyan]🚀 Using FastAPI async server[/bold cyan]")
        console.print("  [dim]10-50x throughput improvement under concurrent load[/dim]")

        # Get database URL for async operations
        database_url = get_database_url()

        app = create_app(
            nexus_fs=nx,  # type: ignore[arg-type]
            api_key=api_key,
            auth_provider=auth_provider,
            database_url=database_url,
            data_dir=backend_config.data_dir,
        )

        # Start background sync for connector mounts (non-blocking)
        start_background_mount_sync(nx)

        run_server(app, host=host, port=port, log_level="info")

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
