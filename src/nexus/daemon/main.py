"""``nexusd`` entry point — Nexus node daemon + node-local commands.

Subcommands:
- (default, no subcommand) — start the daemon
- ``nexusd share`` — share a local subtree as a federation zone
- ``nexusd join``  — join a peer's federation zone
"""

from __future__ import annotations

import json as json_mod
import logging
import os
import sys
from pathlib import Path
from typing import Any

import click

from nexus.cli.exit_codes import ExitCode

logger = logging.getLogger("nexusd")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _JsonLogFormatter(logging.Formatter):
    """Structured JSON log formatter for production use."""

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, str] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info and record.exc_info[1]:
            entry["error"] = str(record.exc_info[1])
        return json_mod.dumps(entry)


def _is_nexusd_process(pid: int) -> bool:
    """Check whether *pid* belongs to a running ``nexusd`` process.

    On Linux we inspect ``/proc/<pid>/cmdline``; elsewhere we fall back to
    ``os.kill(pid, 0)`` which only tells us *some* process is alive.  The
    cmdline check prevents false positives after PID reuse — common in Docker
    containers with small PID namespaces after a segfault/crash restart.
    """
    # Fast path: process doesn't exist at all
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False

    # On Linux, verify the process is actually nexusd
    cmdline_path = Path(f"/proc/{pid}/cmdline")
    try:
        cmdline = cmdline_path.read_bytes()
        # /proc/PID/cmdline uses NUL separators; join for easy substring search
        cmdline_str = cmdline.replace(b"\x00", b" ").decode("utf-8", errors="replace")
        return "nexusd" in cmdline_str or "nexus.daemon" in cmdline_str
    except (FileNotFoundError, PermissionError, OSError):
        # Not Linux or can't read — conservatively assume it's nexusd
        return True


def _manage_pid_file() -> Path:
    """Check for stale PID file and write current PID. Returns PID file path."""
    pid_path = Path.home() / ".nexus" / "nexusd.pid"
    pid_path.parent.mkdir(parents=True, exist_ok=True)

    if pid_path.exists():
        try:
            old_pid = int(pid_path.read_text().strip())
        except (ValueError, OSError):
            pid_path.unlink(missing_ok=True)
        else:
            if _is_nexusd_process(old_pid):
                click.echo(f"Error: nexusd is already running (PID {old_pid}).", err=True)
                click.echo(f"PID file: {pid_path}", err=True)
                sys.exit(ExitCode.CONFIG_ERROR)
            # PID doesn't exist or belongs to a different process — stale file
            pid_path.unlink(missing_ok=True)

    pid_path.write_text(str(os.getpid()))
    return pid_path


def _remove_pid_file(pid_path: Path) -> None:
    """Remove PID file on shutdown."""
    pid_path.unlink(missing_ok=True)


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


def _print_lifecycle_summary(nx: Any) -> None:
    """Print one-line service lifecycle summary at startup (Issue #1578).

    Shown for every profile so operators know at a glance whether the
    daemon has persistent workers and hot-swappable services.
    """
    try:
        coordinator = getattr(nx, "_lifecycle_coordinator", None)
        if coordinator is None:
            return

        quadrants = coordinator.classify_all()
        if not quadrants:
            return

        n_persistent = sum(1 for q in quadrants.values() if q.is_persistent)
        n_hot = sum(1 for q in quadrants.values() if q.is_hot_swappable)

        parts: list[str] = [f"{len(quadrants)} services"]
        if n_hot:
            parts.append(f"{n_hot} hot-swappable")
        if n_persistent:
            parts.append(f"{n_persistent} persistent")
        distro = "persistent" if n_persistent else "on-demand"
        parts.append(f"distro={distro}")

        click.echo(f"  Lifecycle: {', '.join(parts)}")
    except Exception:
        pass  # best-effort — never block startup


# ---------------------------------------------------------------------------
# CLI group — bare ``nexusd`` starts the daemon, subcommands are node-local ops
# ---------------------------------------------------------------------------


@click.group(invoke_without_command=True)
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
    help="Deployment profile: full, lite, embedded, cloud, auto (default: auto).",
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
    "--log-format",
    type=click.Choice(["text", "json"], case_sensitive=False),
    default="text",
    envvar="NEXUS_LOG_FORMAT",
    help="Log output format (default: text).",
)
@click.option(
    "--workers",
    type=int,
    default=None,
    envvar="NEXUS_WORKERS",
    help="Number of uvicorn workers (default: 1).",
)
@click.version_option(package_name="nexus-ai-fs", prog_name="nexusd")
@click.pass_context
def main(
    ctx: click.Context,
    host: str | None,
    port: int | None,
    config_path: str | None,
    data_dir: str | None,
    deployment_profile: str | None,
    api_key: str | None,
    database_url: str | None,
    auth_type: str | None,
    log_level: str | None,
    log_format: str,
    workers: int | None,
) -> None:
    """Nexus node daemon.

    Start a long-running process that exposes gRPC/HTTP APIs for file
    operations, search, permissions, and federation.

    \b
    Examples:
        nexusd                                  # start daemon (defaults)
        nexusd --port 2026 --host 0.0.0.0       # explicit bind
        nexusd --config /etc/nexus/config.yaml   # from config file
        nexusd share /data/shared                # share a subtree
        nexusd join peer1:2126 /shared /local    # join a peer's zone
    """
    # If a subcommand was invoked, skip daemon startup
    if ctx.invoked_subcommand is not None:
        return

    # --- Defaults -----------------------------------------------------------
    host = host or "0.0.0.0"
    port = port or 2026
    log_level = log_level or "info"

    deployment_profile = deployment_profile or "auto"

    # Configure logging early
    _log_level = getattr(logging, log_level.upper())
    if log_format == "json":
        handler = logging.StreamHandler()
        handler.setFormatter(_JsonLogFormatter())
        logging.basicConfig(level=_log_level, handlers=[handler])
    else:
        logging.basicConfig(
            level=_log_level,
            format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        )

    # --- PID file -----------------------------------------------------------
    pid_path = _manage_pid_file()
    ready_path = Path.home() / ".nexus" / "nexusd.ready"

    # Guard: daemon cannot run in remote profile
    if deployment_profile == "remote":
        _remove_pid_file(pid_path)
        click.echo(
            "Error: nexusd cannot run with profile='remote'. "
            "A daemon cannot be a thin client of another daemon.",
            err=True,
        )
        sys.exit(ExitCode.CONFIG_ERROR)

    try:
        # --- Print banner ---------------------------------------------------
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
            click.echo(f"  DB:      {_redact_url(database_url)}")

        click.echo("")

        # --- Create local NexusFS -------------------------------------------
        try:
            import asyncio

            import nexus

            connect_config: dict[str, object] = {"profile": deployment_profile}

            if data_dir:
                connect_config["data_dir"] = data_dir

            # Forward --database-url to NexusFS so SecretsService /
            # PasswordVaultService / ReBAC etc. get a wired record_store.
            # Previously the flag was only consumed by DatabaseAPIKeyAuth
            # below, which surprised callers who expected the obvious
            # "wire the DB" semantics.
            if database_url:
                connect_config["database_url"] = database_url

            # Respect NEXUS_ENFORCE_PERMISSIONS env var
            import os as _os

            _enforce = _os.environ.get("NEXUS_ENFORCE_PERMISSIONS", "")
            if _enforce.lower() in ("true", "1", "yes"):
                connect_config["enforce_permissions"] = True
            if connect_config.get("enforce_permissions"):
                click.echo("  Perms:   enforce=True")
            if config_path:
                from nexus.config import load_config

                config_obj = load_config(Path(config_path))
                nx = asyncio.run(nexus.connect(config=config_obj))
            else:
                nx = asyncio.run(nexus.connect(config=connect_config))

        except Exception as e:
            click.echo(f"Error: Failed to initialize NexusFS: {e}", err=True)
            logger.exception("NexusFS initialization failed")
            sys.exit(ExitCode.INTERNAL_ERROR)

        # --- Service lifecycle summary (Issue #1578) -------------------------
        _print_lifecycle_summary(nx)

        # --- Resolve auth ---------------------------------------------------
        auth_provider: Any = None
        if auth_type == "database":
            if not database_url:
                database_url = os.getenv("POSTGRES_URL")
            if database_url:
                try:
                    from nexus.bricks.auth.providers.database_key import DatabaseAPIKeyAuth
                    from nexus.storage.record_store import SQLAlchemyRecordStore

                    record_store = SQLAlchemyRecordStore(database_url)
                    auth_provider = DatabaseAPIKeyAuth(record_store)
                    logger.info("Using database authentication")
                except Exception:
                    logger.warning("DatabaseAPIKeyAuth not available, falling back to static")

        # Resolve API key: explicit flag > env var (handled by Click) > key file
        if not api_key:
            key_file = os.getenv("NEXUS_API_KEY_FILE", "")
            if key_file and Path(key_file).is_file():
                api_key = Path(key_file).read_text().strip()

        # Fallback: StaticAPIKeyAuth when NEXUS_API_KEY is set but no DB auth
        if auth_provider is None and api_key:
            from nexus.bricks.auth.providers.static_key import StaticAPIKeyAuth

            static_provider = StaticAPIKeyAuth(
                {api_key: {"subject_type": "user", "subject_id": "admin", "is_admin": True}}
            )

            # Chain with DatabaseAPIKeyAuth so agent keys generated at
            # registration are also validated (Issue #3250).
            _record_store = getattr(nx, "_record_store", None) if nx else None
            logger.info(
                "Auth chain: nx=%s, _record_store=%s",
                type(nx).__name__ if nx else None,
                type(_record_store).__name__ if _record_store else None,
            )
            if _record_store is not None:
                try:
                    from nexus.bricks.auth.providers.database_key import DatabaseAPIKeyAuth
                    from nexus.server.auth.factory import _ChainedAPIKeyAuth

                    db_provider = DatabaseAPIKeyAuth(_record_store, require_expiry=False)
                    auth_provider = _ChainedAPIKeyAuth(static_provider, db_provider)
                    logger.info(
                        "Using static + database API key authentication (agent key fallback)"
                    )
                except Exception as exc:
                    auth_provider = static_provider
                    logger.warning("Auth chain fallback failed: %s", exc, exc_info=True)
            else:
                auth_provider = static_provider
                logger.info("Using static API key authentication (no database)")

        # --- Create FastAPI app + run ---------------------------------------
        from nexus.server.fastapi_server import create_app, run_server

        nx_fs: Any = nx
        app = create_app(
            nexus_fs=nx_fs,
            api_key=api_key,
            auth_provider=auth_provider,
            database_url=database_url,
        )

        # --- Ready file -----------------------------------------------------
        ready_path.write_text(f"{host}:{port}\n")

        click.echo(f"Starting nexusd on {host}:{port}")
        click.echo("Press Ctrl+C to stop")
        click.echo("")

        run_server(app, host=host, port=port, log_level=log_level, workers=workers)

    except KeyboardInterrupt:
        click.echo("\nnexusd stopped")
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        logger.exception("nexusd failed")
        sys.exit(ExitCode.INTERNAL_ERROR)
    finally:
        _remove_pid_file(pid_path)
        ready_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# nexusd share — share a local subtree as a federation zone
# ---------------------------------------------------------------------------


@main.command("share")
@click.argument("path", type=str)
@click.option(
    "--zone-id",
    type=str,
    default=None,
    help="Explicit zone ID for the shared subtree (auto-generated if omitted).",
)
@click.option("--remote-url", default=None, envvar="NEXUS_URL", help="Running nexusd URL.")
@click.option("--remote-api-key", default=None, envvar="NEXUS_API_KEY", help="API key.")
def share_cmd(
    path: str,
    zone_id: str | None,
    remote_url: str | None,
    remote_api_key: str | None,
) -> None:
    """Share a local subtree as a federation zone.

    Tells the running nexusd to create a new zone from a local path
    so that peers can join it.

    \b
    Examples:
        nexusd share /data/shared
        nexusd share /data/shared --zone-id my-shared-zone
    """
    from nexus.cli.utils import console, rpc_call

    try:
        data = rpc_call(
            remote_url, remote_api_key, "federation_share", local_path=path, zone_id=zone_id
        )
        new_zone = data.get("zone_id", "unknown")
        console.print(f"[nexus.success]Shared '{path}' as federation zone[/nexus.success]")
        console.print(f"  Zone ID: [nexus.reference]{new_zone}[/nexus.reference]")
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


# ---------------------------------------------------------------------------
# nexusd join — join a peer's federation zone
# ---------------------------------------------------------------------------


@main.command("join")
@click.argument("peer_addr", type=str)
@click.argument("remote_path", type=str)
@click.argument("local_path", type=str)
@click.option("--remote-url", default=None, envvar="NEXUS_URL", help="Running nexusd URL.")
@click.option("--remote-api-key", default=None, envvar="NEXUS_API_KEY", help="API key.")
def join_cmd(
    peer_addr: str,
    remote_path: str,
    local_path: str,
    remote_url: str | None,
    remote_api_key: str | None,
) -> None:
    """Join a peer's federation zone.

    Tells the running nexusd to connect to a remote peer and replicate
    a shared subtree locally.

    \b
    Examples:
        nexusd join peer1:2126 /shared /local/shared
        nexusd join 10.0.0.5:2126 /data /mnt/data
    """
    from nexus.cli.utils import console, rpc_call

    try:
        data = rpc_call(
            remote_url,
            remote_api_key,
            "federation_join",
            peer_addr=peer_addr,
            remote_path=remote_path,
            local_path=local_path,
        )
        joined_zone = data.get("zone_id", "unknown")
        console.print(f"[nexus.success]Joined federation zone from {peer_addr}[/nexus.success]")
        console.print(f"  Zone ID:     [nexus.reference]{joined_zone}[/nexus.reference]")
        console.print(f"  Remote path: {remote_path}")
        console.print(f"  Local path:  {local_path}")
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)
