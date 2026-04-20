"""`nexus daemon ...` CLI subcommands (#3804).

Each subcommand imports its dependencies inside the function body so that
``nexus daemon --help`` stays fast and doesn't pull in httpx/sqlite/etc.
until a real invocation runs.

Environment variables:
    NEXUS_KMS_PROVIDER
        Selects the envelope-encryption provider used by ``daemon run``. MVP
        supports only ``"memory"`` (the in-process fake). Other values raise
        ``click.ClickException``.
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any

import click

_DEFAULT_CFG = Path.home() / ".nexus" / "daemon.toml"


@click.group("daemon")
def daemon() -> None:
    """Local nexus-bot daemon commands."""


@daemon.command("join")
@click.option("--server", required=True, help="Server base URL.")
@click.option("--enroll-token", required=True, help="One-shot enroll token from admin.")
@click.option(
    "--config",
    "config_path",
    default=str(_DEFAULT_CFG),
    show_default=True,
    help="Path to write daemon config.",
)
def join_cmd(server: str, enroll_token: str, config_path: str) -> None:
    """Enroll this machine with the server, writing ~/.nexus/daemon.toml."""
    import platform

    import httpx
    import jwt as pyjwt

    from nexus.bricks.auth.daemon.config import DaemonConfig
    from nexus.bricks.auth.daemon.keystore import load_or_create_keypair

    nexus_home = Path(config_path).parent
    key_path = nexus_home / "daemon" / "machine.key"
    jwt_cache = nexus_home / "daemon" / "jwt.cache"
    server_pubkey_path = nexus_home / "daemon" / "server.pub.pem"
    pub_pem = load_or_create_keypair(key_path)

    resp = httpx.post(
        f"{server.rstrip('/')}/v1/daemon/enroll",
        json={
            "enroll_token": enroll_token,
            "pubkey_pem": pub_pem.decode(),
            "daemon_version": _daemon_version(),
            "hostname": platform.node(),
        },
        timeout=30.0,
    )
    if resp.status_code != 200:
        raise click.ClickException(f"enroll failed: {resp.status_code} {resp.text}")
    body = resp.json()

    server_pubkey_path.parent.mkdir(parents=True, exist_ok=True)
    server_pubkey_path.write_text(body["server_pubkey_pem"])

    # Decode JWT (without signature check — we trust the just-joined server) to
    # extract tenant_id + principal_id for the config.
    decoded = pyjwt.decode(body["jwt"], options={"verify_signature": False}, algorithms=["ES256"])
    cfg = DaemonConfig(
        server_url=server.rstrip("/"),
        tenant_id=uuid.UUID(decoded["tenant_id"]),
        principal_id=uuid.UUID(decoded["principal_id"]),
        machine_id=uuid.UUID(body["machine_id"]),
        key_path=key_path,
        jwt_cache_path=jwt_cache,
        server_pubkey_path=server_pubkey_path,
    )
    cfg.save(Path(config_path))

    jwt_cache.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(jwt_cache), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, body["jwt"].encode())
    finally:
        os.close(fd)
    os.chmod(jwt_cache, 0o600)

    click.echo(f"daemon joined: machine_id={cfg.machine_id}")


@daemon.command("run")
@click.option("--config", "config_path", default=str(_DEFAULT_CFG), show_default=True)
def run_cmd(config_path: str) -> None:
    """Main daemon loop: watch source files + push changes + renew JWT."""
    from nexus.bricks.auth.daemon.config import DaemonConfig
    from nexus.bricks.auth.daemon.jwt_client import JwtClient
    from nexus.bricks.auth.daemon.push import Pusher
    from nexus.bricks.auth.daemon.queue import PushQueue
    from nexus.bricks.auth.daemon.runner import DaemonRunner

    cfg = DaemonConfig.load(Path(config_path))
    nexus_home = Path(config_path).parent
    queue = PushQueue(nexus_home / "daemon" / "queue.db")
    jwt_client = JwtClient(
        server_url=cfg.server_url,
        tenant_id=cfg.tenant_id,
        machine_id=cfg.machine_id,
        key_path=cfg.key_path,
        jwt_cache_path=cfg.jwt_cache_path,
        server_pubkey_path=cfg.server_pubkey_path,
    )
    ep = _build_encryption_provider()
    pusher = Pusher(
        server_url=cfg.server_url,
        tenant_id=cfg.tenant_id,
        principal_id=cfg.principal_id,
        machine_id=cfg.machine_id,
        daemon_version=_daemon_version(),
        encryption_provider=ep,
        queue=queue,
        jwt_provider=lambda: jwt_client.current() or jwt_client.refresh_now(),
    )
    watch_target = Path.home() / ".codex" / "auth.json"

    def _refresh_jwt() -> None:
        jwt_client.refresh_now()

    runner = DaemonRunner(
        source_watch_target=watch_target,
        queue=queue,
        pusher=pusher,
        jwt_refresh_every=45 * 60,
        status_path=nexus_home / "daemon" / "status.json",
        jwt_refresh_callable=_refresh_jwt,
    )
    runner.run()


@daemon.command("status")
@click.option("--config", "config_path", default=str(_DEFAULT_CFG), show_default=True)
def status_cmd(config_path: str) -> None:
    """Print daemon status JSON; exit 0=healthy, 1=degraded, 2=stopped."""
    status_path = Path(config_path).parent / "daemon" / "status.json"
    if not status_path.exists():
        click.echo("stopped")
        sys.exit(2)
    data = json.loads(status_path.read_text())
    click.echo(json.dumps(data, indent=2))
    if data["state"] == "healthy":
        sys.exit(0)
    if data["state"] == "degraded":
        sys.exit(1)
    sys.exit(2)


@daemon.command("install")
@click.option("--config", "config_path", default=str(_DEFAULT_CFG), show_default=True)
def install_cmd(config_path: str) -> None:
    """Install launchd plist (macOS only)."""
    from nexus.bricks.auth.daemon.installer import install

    plist_path = install(executable=sys.executable, config_path=Path(config_path))
    click.echo(f"installed: {plist_path}")


@daemon.command("uninstall")
def uninstall_cmd() -> None:
    """Remove launchd plist (macOS only)."""
    from nexus.bricks.auth.daemon.installer import uninstall

    uninstall()
    click.echo("uninstalled")


def _daemon_version() -> str:
    from nexus import __version__

    return __version__


def _build_encryption_provider() -> Any:
    """Select the envelope-encryption provider from ``NEXUS_KMS_PROVIDER``.

    Returns ``Any`` because the MVP's only supported value ("memory") maps to
    ``InMemoryEncryptionProvider``, whose public shape (``wrap_dek``/
    ``unwrap_dek``) differs from the ``encrypt()``-style internal protocol the
    ``Pusher`` expects. Wiring the two together is owned by T18's integration
    layer; this helper only enforces the env-var → constructor selection.
    """
    provider_name = os.environ.get("NEXUS_KMS_PROVIDER", "memory")
    if provider_name == "memory":
        from nexus.bricks.auth.envelope_providers.in_memory import (
            InMemoryEncryptionProvider,
        )

        return InMemoryEncryptionProvider()
    raise click.ClickException(
        f"unsupported NEXUS_KMS_PROVIDER={provider_name!r}; MVP supports only 'memory'"
    )


__all__ = ["daemon"]
