"""``nexusd`` entry point — start the Nexus node daemon.

Thin orchestrator that:
1. Parses CLI flags and environment variables
2. Creates a local NexusFS via ``nexus.connect()``
3. Wraps it in a FastAPI app via ``create_app()``
4. Runs uvicorn (blocking until SIGTERM)

The heavy lifting is done by existing modules:
- ``nexus.connect()`` handles profile detection, storage pillar creation
- ``nexus.server.fastapi_server.create_app()`` handles middleware, routes, auth
- ``nexus.server.lifespan`` handles async startup phases and graceful shutdown
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Any

import click

logger = logging.getLogger("nexusd")


@click.command(name="nexusd")
@click.option(
    "--host",
    default=None,
    envvar="NEXUS_HOST",
    help="Bind address (default: 0.0.0.0).",
    show_default=True,
)
@click.option(
    "--port",
    type=int,
    default=None,
    envvar="NEXUS_PORT",
    help="Listen port (default: 2026).",
    show_default=True,
)
@click.option(
    "--config",
    "config_path",
    type=click.Path(exists=True, dir_okay=False),
    default=None,
    envvar="NEXUS_CONFIG_FILE",
    help="Path to YAML config file.",
)
@click.option(
    "--data-dir",
    type=click.Path(),
    default=None,
    envvar="NEXUS_DATA_DIR",
    help="Local data directory (default: ~/.nexus/data).",
)
@click.option(
    "--profile",
    "deployment_profile",
    default=None,
    envvar="NEXUS_PROFILE",
    help="Deployment profile: full, lite, embedded, kernel, cloud, auto (default: auto).",
)
@click.option(
    "--api-key",
    default=None,
    envvar="NEXUS_API_KEY",
    help="Static API key for authentication.",
)
@click.option(
    "--database-url",
    default=None,
    envvar="NEXUS_DATABASE_URL",
    help="PostgreSQL connection URL for RecordStore.",
)
@click.option(
    "--auth-type",
    type=click.Choice(["static", "database", "none"]),
    default=None,
    help="Authentication backend type.",
)
@click.option(
    "--log-level",
    type=click.Choice(["debug", "info", "warning", "error"], case_sensitive=False),
    default=None,
    envvar="NEXUS_LOG_LEVEL",
    help="Logging level (default: info).",
)
@click.option(
    "--workers",
    type=int,
    default=None,
    envvar="NEXUS_WORKERS",
    help="Number of uvicorn workers (default: 1).",
)
@click.version_option(package_name="nexus-ai-fs", prog_name="nexusd")
def main(
    host: str | None,
    port: int | None,
    config_path: str | None,
    data_dir: str | None,
    deployment_profile: str | None,
    api_key: str | None,
    database_url: str | None,
    auth_type: str | None,
    log_level: str | None,
    workers: int | None,
) -> None:
    """Start the Nexus node daemon.

    Starts a long-running process that exposes gRPC/HTTP APIs for file
    operations, search, permissions, and federation.

    \b
    Examples:
        nexusd                                  # defaults
        nexusd --port 2026 --host 0.0.0.0       # explicit bind
        nexusd --config /etc/nexus/config.yaml   # from config file
        nexusd --profile full --log-level debug  # full profile, debug logs
    """
    # --- Defaults -----------------------------------------------------------
    host = host or "0.0.0.0"
    port = port or 2026
    log_level = log_level or "info"
    deployment_profile = deployment_profile or "auto"

    # Configure logging early
    logging.basicConfig(
        level=getattr(logging, log_level.upper()),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    # Guard: daemon cannot run in remote profile
    if deployment_profile == "remote":
        click.echo(
            "Error: nexusd cannot run with profile='remote'. "
            "A daemon cannot be a thin client of another daemon.",
            err=True,
        )
        sys.exit(1)

    # --- Print banner -------------------------------------------------------
    click.echo("")
    click.echo("nexusd — Nexus Node Daemon")
    click.echo(f"  Host:    {host}")
    click.echo(f"  Port:    {port}")
    click.echo(f"  Profile: {deployment_profile}")
    if data_dir:
        click.echo(f"  Data:    {data_dir}")
    if config_path:
        click.echo(f"  Config:  {config_path}")
    if database_url:
        # Redact password in URL for display
        click.echo(f"  DB:      {_redact_url(database_url)}")
    click.echo("")

    # --- Create local NexusFS -----------------------------------------------
    try:
        import nexus

        connect_config: dict[str, object] = {"profile": deployment_profile}

        if data_dir:
            connect_config["data_dir"] = data_dir
        if config_path:
            from nexus.config import load_config

            config_obj = load_config(Path(config_path))
            nx = nexus.connect(config=config_obj)
            if hasattr(nx, "_config"):
                nx._config = config_obj
        else:
            nx = nexus.connect(config=connect_config)

    except Exception as e:
        click.echo(f"Error: Failed to initialize NexusFS: {e}", err=True)
        logger.exception("NexusFS initialization failed")
        sys.exit(1)

    # --- Resolve auth -------------------------------------------------------
    auth_provider = None
    if auth_type == "database":
        if not database_url:
            database_url = os.getenv("NEXUS_DATABASE_URL") or os.getenv("POSTGRES_URL")
        if database_url:
            try:
                from nexus.server.auth.database_auth import DatabaseAuthProvider

                auth_provider = DatabaseAuthProvider(database_url)
                logger.info("Using database authentication")
            except ImportError:
                logger.warning("DatabaseAuthProvider not available, falling back to static")

    # Resolve API key: explicit flag > env var > key file
    if not api_key:
        api_key = os.getenv("NEXUS_API_KEY")
    if not api_key:
        key_file = os.getenv("NEXUS_API_KEY_FILE", "")
        if key_file and Path(key_file).is_file():
            api_key = Path(key_file).read_text().strip()

    # --- Create FastAPI app + run -------------------------------------------
    try:
        from nexus.server.fastapi_server import create_app, run_server

        # nexus.connect() returns NexusFilesystem protocol; create_app expects
        # NexusFS concrete type.  The daemon always creates a local NexusFS so
        # the cast is safe at runtime.
        nx_fs: Any = nx
        app = create_app(
            nexus_fs=nx_fs,
            api_key=api_key,
            auth_provider=auth_provider,
            database_url=database_url,
        )

        click.echo(f"Starting nexusd on {host}:{port}")
        click.echo("Press Ctrl+C to stop")
        click.echo("")

        run_server(app, host=host, port=port, log_level=log_level, workers=workers)

    except KeyboardInterrupt:
        click.echo("\nnexusd stopped")
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        logger.exception("nexusd failed")
        sys.exit(1)


def _redact_url(url: str) -> str:
    """Redact password from database URL for safe logging."""
    try:
        from urllib.parse import urlparse, urlunparse

        parsed = urlparse(url)
        if parsed.password:
            netloc = f"{parsed.username}:****@{parsed.hostname}"
            if parsed.port:
                netloc += f":{parsed.port}"
            return urlunparse(parsed._replace(netloc=netloc))
    except Exception:
        pass
    return url
