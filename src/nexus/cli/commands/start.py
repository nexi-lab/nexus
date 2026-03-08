"""``nexus start`` — federation-ready single-command node startup.

Combines TLS initialization, HTTP + gRPC server startup, and root zone
bootstrap into a single command.  Distinct from:

- ``nexus serve`` — HTTP-only server, no TLS/gRPC/zone bootstrap.
- ``nexus up``    — Docker Compose stack (requires Docker).
"""

from __future__ import annotations

import logging
import os
import sys
from typing import TYPE_CHECKING, Any

import click

if TYPE_CHECKING:
    from nexus.bricks.auth.providers.base import AuthProvider

from nexus.cli.utils import (
    BackendConfig,
    add_backend_options,
    console,
    get_filesystem,
    handle_error,
)

logger = logging.getLogger(__name__)


@click.command(name="start")
@click.option("--host", default="0.0.0.0", help="Server host.")
@click.option("--port", default=2026, type=int, help="HTTP server port.")
@click.option(
    "--grpc-port",
    default=2126,
    type=int,
    envvar="NEXUS_GRPC_PORT",
    help="gRPC server port (default: 2126).",
    show_default=True,
)
@click.option(
    "--zone-id",
    default="default",
    help="Root zone ID for bootstrap.",
    show_default=True,
)
@click.option(
    "--node-id",
    default=1,
    type=int,
    envvar="NEXUS_NODE_ID",
    help="Raft node ID.",
    show_default=True,
)
@click.option(
    "--api-key",
    default=None,
    envvar="NEXUS_API_KEY",
    help="API key for authentication.",
)
@click.option(
    "--auth-type",
    type=click.Choice(["static", "database", "local", "oidc"]),
    default=None,
    help="Authentication type.",
)
@click.option(
    "--skip-tls-init",
    is_flag=True,
    help="Skip automatic TLS certificate generation.",
)
@add_backend_options
def start(
    host: str,
    port: int,
    grpc_port: int,
    zone_id: str,
    node_id: int,
    api_key: str | None,
    auth_type: str | None,
    skip_tls_init: bool,
    backend_config: BackendConfig,
) -> None:
    """Start a federation-ready Nexus node.

    This is a single command that:
    1. Initializes TLS certificates (if not present)
    2. Starts the HTTP + gRPC server
    3. Bootstraps the root zone

    Use this for bare-metal or VM deployments without Docker.

    Examples:
        nexus start
        nexus start --zone-id corp --node-id 1
        nexus start --grpc-port 2126 --auth-type database
        nexus start --skip-tls-init   # if TLS is externally managed
    """
    try:
        # ============================================
        # Step 1: TLS initialization
        # ============================================
        if not skip_tls_init:
            _init_tls(backend_config.data_dir, zone_id, node_id)

        # ============================================
        # Step 2: Configure gRPC + mode
        # ============================================
        os.environ["NEXUS_GRPC_PORT"] = str(grpc_port)

        # Detect federation capability (Rust extensions) for user-facing messages.
        # connect() auto-detects at runtime — no env var needed.
        enforce_permissions = bool(auth_type or api_key)
        try:
            from nexus.raft.zone_manager import _get_py_zone_manager

            if _get_py_zone_manager() is None:
                raise ImportError("PyO3 ZoneManager not available")
            console.print(f"  gRPC: [cyan]port {grpc_port}[/cyan]")
        except (ImportError, RuntimeError):
            console.print("  [yellow]Federation unavailable (Rust extensions not built).[/yellow]")
            console.print("  [yellow]Running with in-memory metastore.[/yellow]")

        # ============================================
        # Step 3: Create filesystem
        # ============================================
        nx = get_filesystem(
            backend_config,
            enforce_permissions=enforce_permissions,
            enforce_zone_isolation=True,
        )

        # ============================================
        # Step 4: Create auth provider
        # ============================================
        auth_provider: AuthProvider | None = None
        if auth_type == "database":
            auth_provider = _create_database_auth()
        elif api_key:
            from nexus.server.auth.factory import create_auth_provider

            auth_provider = create_auth_provider("static", api_key=api_key)

        # ============================================
        # Step 5: Start server
        # ============================================
        console.print()
        console.print("[bold cyan]Starting Nexus federation node...[/bold cyan]")
        console.print(f"  HTTP:  [cyan]{host}:{port}[/cyan]")
        console.print(f"  gRPC:  [cyan]{host}:{grpc_port}[/cyan]")
        console.print(f"  Zone:  [cyan]{zone_id}[/cyan]")
        console.print(f"  Node:  [cyan]{node_id}[/cyan]")
        console.print()
        console.print("[green]Press Ctrl+C to stop[/green]")
        console.print()

        from nexus.lib.env import get_database_url
        from nexus.server.fastapi_server import create_app, run_server

        database_url = get_database_url()
        nx_app: Any = nx
        app = create_app(
            nexus_fs=nx_app,
            api_key=api_key,
            auth_provider=auth_provider,
            database_url=database_url,
            data_dir=backend_config.data_dir,
        )

        # Start background mount sync
        from nexus.cli.commands.server import start_background_mount_sync

        start_background_mount_sync(nx)

        run_server(app, host=host, port=port, log_level="info")

    except KeyboardInterrupt:
        console.print("\n[yellow]Node stopped by user.[/yellow]")
    except Exception as exc:
        handle_error(exc)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _init_tls(data_dir: str | None, zone_id: str, node_id: int) -> None:
    """Initialize TLS certificates if not already present."""
    from pathlib import Path

    from nexus.security.tls.config import ZoneTlsConfig

    base = Path(data_dir) if data_dir else Path(".")
    existing = ZoneTlsConfig.from_data_dir(base)
    if existing is not None:
        from nexus.security.tls.certgen import cert_fingerprint, load_pem_cert

        ca = load_pem_cert(existing.ca_cert_path)
        console.print(
            f"  TLS: [green]already initialized[/green] (CA: {cert_fingerprint(ca)[:16]}...)"
        )
        return

    from nexus.security.tls.certgen import (
        cert_fingerprint,
        generate_node_cert,
        generate_zone_ca,
        save_pem,
    )

    console.print("  TLS: [cyan]generating certificates...[/cyan]")
    tls_dir = base / "tls"
    ca_cert, ca_key = generate_zone_ca(zone_id)
    save_pem(tls_dir / "ca.pem", ca_cert)
    save_pem(tls_dir / "ca-key.pem", ca_key, is_private=True)

    node_cert, node_key = generate_node_cert(node_id, zone_id, ca_cert, ca_key)
    save_pem(tls_dir / "node.pem", node_cert)
    save_pem(tls_dir / "node-key.pem", node_key, is_private=True)

    console.print(f"  TLS: [green]initialized[/green] (CA: {cert_fingerprint(ca_cert)[:16]}...)")


def _create_database_auth() -> AuthProvider:
    """Create a database auth provider (mirrors serve command logic)."""
    import secrets

    from nexus.bricks.auth.providers.database_key import DatabaseAPIKeyAuth
    from nexus.bricks.auth.providers.database_local import DatabaseLocalAuth
    from nexus.factory._record_store import create_record_store
    from nexus.lib.env import get_database_url
    from nexus.server.auth.factory import DiscriminatingAuthProvider

    db_url = get_database_url()
    if not db_url:
        console.print("[red]Error:[/red] Database auth requires NEXUS_DATABASE_URL.")
        sys.exit(1)

    jwt_secret = os.getenv("NEXUS_JWT_SECRET") or secrets.token_urlsafe(32)
    _record_store = create_record_store(db_url=db_url)
    session_factory = _record_store.session_factory

    return DiscriminatingAuthProvider(
        api_key_provider=DatabaseAPIKeyAuth(record_store=_record_store),
        jwt_provider=DatabaseLocalAuth(
            session_factory=session_factory,
            jwt_secret=jwt_secret,
            token_expiry=3600,
        ),
    )


def register_commands(cli: click.Group) -> None:
    """Register start command."""
    cli.add_command(start)
