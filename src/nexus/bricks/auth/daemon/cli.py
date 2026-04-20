"""`nexus daemon ...` CLI subcommands (#3804).

State is per-profile at ``~/.nexus/daemons/<profile>/`` so the same laptop
can enroll into multiple Nexus servers / tenants without clobbering each
other's keypair, JWT cache, or queue. The ``--profile NAME`` flag picks
which enrollment to operate on; default is derived from the server URL
on ``join`` and required (or auto-picked when unambiguous) elsewhere.

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

_NEXUS_HOME = Path.home() / ".nexus"
_KEYRING_SERVICE_PREFIX = "com.nexus.daemon"


def _keyring_service_for(profile: str) -> str:
    """Keyring entries are namespaced per profile (see jwt_cache.make_jwt_cache)."""
    return f"{_KEYRING_SERVICE_PREFIX}.{profile}"


def _profile_paths(profile: str) -> dict[str, Path]:
    """All per-profile state paths under ``~/.nexus/daemons/<profile>/``."""
    from nexus.bricks.auth.daemon.config import profile_dir

    d = profile_dir(_NEXUS_HOME, profile)
    return {
        "dir": d,
        "config": d / "daemon.toml",
        "key": d / "machine.key",
        "jwt_cache": d / "jwt.cache",
        "server_pubkey": d / "server.pub.pem",
        "queue": d / "queue.db",
        "status": d / "status.json",
    }


def _resolve_profile(
    profile: str | None,
    *,
    required_action: str,
) -> str:
    """Resolve an explicit ``--profile`` flag or auto-pick when unambiguous.

    Raises ``click.ClickException`` if 0 or >1 profiles exist and none was
    specified — telling the user exactly what to run next.
    """
    from nexus.bricks.auth.daemon.config import list_profiles

    if profile is not None:
        return profile
    profiles = list_profiles(_NEXUS_HOME)
    if len(profiles) == 1:
        return profiles[0]
    if not profiles:
        raise click.ClickException(
            f"no daemon profiles enrolled; run `nexus daemon join ...` before `{required_action}`"
        )
    raise click.ClickException(
        f"multiple profiles enrolled ({', '.join(profiles)}); pass --profile NAME to `{required_action}`"
    )


@click.group("daemon")
def daemon() -> None:
    """Local nexus-bot daemon commands (per-profile at ~/.nexus/daemons/<profile>/)."""


@daemon.command("join")
@click.option("--server", required=True, help="Server base URL.")
@click.option("--enroll-token", required=True, help="One-shot enroll token from admin.")
@click.option(
    "--profile",
    default=None,
    help="Profile name (default: sanitized server host, e.g. 'localhost-2026').",
)
def join_cmd(server: str, enroll_token: str, profile: str | None) -> None:
    """Enroll this machine with the server, writing ~/.nexus/daemons/<profile>/."""
    import platform

    import httpx
    import jwt as pyjwt

    from nexus.bricks.auth.daemon.config import DaemonConfig, default_profile_for
    from nexus.bricks.auth.daemon.keystore import load_or_create_keypair

    resolved_profile = profile or default_profile_for(server)
    paths = _profile_paths(resolved_profile)

    if paths["config"].exists():
        raise click.ClickException(
            f"profile {resolved_profile!r} already enrolled at {paths['dir']}; "
            "delete the directory to re-enroll, or pass a different --profile"
        )

    pub_pem = load_or_create_keypair(paths["key"])

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

    paths["server_pubkey"].parent.mkdir(parents=True, exist_ok=True)
    paths["server_pubkey"].write_text(body["server_pubkey_pem"])

    # Decode JWT (without signature check — we trust the just-joined server) to
    # extract tenant_id + principal_id for the config.
    decoded = pyjwt.decode(body["jwt"], options={"verify_signature": False}, algorithms=["ES256"])
    cfg = DaemonConfig(
        profile=resolved_profile,
        server_url=server.rstrip("/"),
        tenant_id=uuid.UUID(decoded["tenant_id"]),
        principal_id=uuid.UUID(decoded["principal_id"]),
        machine_id=uuid.UUID(body["machine_id"]),
        key_path=paths["key"],
        jwt_cache_path=paths["jwt_cache"],
        server_pubkey_path=paths["server_pubkey"],
    )
    cfg.save(paths["config"])

    paths["jwt_cache"].parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(paths["jwt_cache"]), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, body["jwt"].encode())
    finally:
        os.close(fd)
    os.chmod(paths["jwt_cache"], 0o600)

    # Also push the fresh JWT into the per-profile cache (Keychain on macOS /
    # Secret Service on Linux, else file). Prevents a prior enrollment's
    # cached JWT from poisoning the new one when the OS keyring survives a
    # `rm -rf ~/.nexus/daemons/<profile>/` and the profile name collides
    # (e.g., same server URL).
    from nexus.bricks.auth.daemon.jwt_cache import make_jwt_cache

    try:
        cache = make_jwt_cache(paths["jwt_cache"], service=_keyring_service_for(resolved_profile))
        cache.store(body["jwt"])
    except Exception as exc:  # noqa: BLE001  # keyring-backend errors must not fail join
        click.echo(f"warning: could not seed JWT cache ({exc}); daemon will refresh on first run")

    click.echo(
        f"daemon joined: profile={cfg.profile} machine_id={cfg.machine_id} dir={paths['dir']}"
    )


@daemon.command("bootstrap")
@click.option(
    "--server", required=True, help="Local Nexus server URL (e.g. http://localhost:2026)."
)
@click.option(
    "--admin-user",
    default="admin",
    show_default=True,
    help="Admin username for X-Admin-User header (advisory only; not an auth factor).",
)
@click.option(
    "--admin-token",
    default=None,
    help=(
        "Value for X-Admin-Token. Defaults to the NEXUS_ADMIN_BOOTSTRAP_TOKEN env var. "
        "Required — the server rejects requests without a matching token."
    ),
)
@click.option("--tenant-name", default="dev-local", show_default=True)
@click.option("--principal-label", default=None, help="Defaults to machine hostname.")
@click.option(
    "--profile",
    default=None,
    help="Profile name (default: sanitized server host, e.g. 'localhost-2026').",
)
def bootstrap_cmd(
    server: str,
    admin_user: str,
    admin_token: str | None,
    tenant_name: str,
    principal_label: str | None,
    profile: str | None,
) -> None:
    """Dev-loop one-shot: hit the running server's admin-bootstrap endpoint
    to mint tenant+principal+enroll-token, then enroll this laptop.

    Requires the server to have been started with ``NEXUS_ALLOW_ADMIN_BYPASS=true``
    AND ``NEXUS_ADMIN_BOOTSTRAP_TOKEN`` set to a shared secret (the caller
    must present the same value via ``--admin-token`` or the
    ``NEXUS_ADMIN_BOOTSTRAP_TOKEN`` env var). Fails fast with 401/404
    otherwise — production stacks must use ``nexus auth enroll-token`` instead.
    """
    import os
    import platform

    import httpx

    label = principal_label or platform.node() or "dev-laptop"
    token = admin_token or os.environ.get("NEXUS_ADMIN_BOOTSTRAP_TOKEN", "")
    if not token:
        raise click.ClickException(
            "admin bootstrap token required: pass --admin-token or set "
            "NEXUS_ADMIN_BOOTSTRAP_TOKEN (the same value the server was started with)"
        )

    resp = httpx.post(
        f"{server.rstrip('/')}/v1/admin/daemon-bootstrap",
        headers={"X-Admin-User": admin_user, "X-Admin-Token": token},
        json={"tenant_name": tenant_name, "principal_label": label, "ttl_minutes": 15},
        timeout=30.0,
    )
    if resp.status_code == 404:
        raise click.ClickException(
            "bootstrap endpoint not available — ensure the server has "
            "NEXUS_ALLOW_ADMIN_BYPASS=true AND NEXUS_ADMIN_BOOTSTRAP_TOKEN set (dev-only)"
        )
    if resp.status_code != 200:
        raise click.ClickException(f"bootstrap failed: {resp.status_code} {resp.text}")
    body = resp.json()
    click.echo(f"bootstrap ok: tenant_id={body['tenant_id']} principal_id={body['principal_id']}")

    # Re-use the existing join command in-process (preserves all the side
    # effects: keystore, JWT cache, server pubkey, Keychain seed).
    ctx = click.get_current_context()
    ctx.invoke(
        join_cmd,
        server=server,
        enroll_token=body["enroll_token"],
        profile=profile,
    )


@daemon.command("run")
@click.option("--profile", default=None, help="Profile name (auto-selected if only one exists).")
def run_cmd(profile: str | None) -> None:
    """Main daemon loop: watch source files + push changes + renew JWT."""
    from nexus.bricks.auth.daemon.adapters import DEFAULT_SUBPROCESS_SOURCES
    from nexus.bricks.auth.daemon.config import DaemonConfig
    from nexus.bricks.auth.daemon.jwt_client import JwtClient
    from nexus.bricks.auth.daemon.push import Pusher
    from nexus.bricks.auth.daemon.queue import PushQueue
    from nexus.bricks.auth.daemon.runner import DaemonRunner

    resolved_profile = _resolve_profile(profile, required_action="daemon run")
    paths = _profile_paths(resolved_profile)

    cfg = DaemonConfig.load(paths["config"])
    queue = PushQueue(paths["queue"])
    jwt_client = JwtClient(
        server_url=cfg.server_url,
        tenant_id=cfg.tenant_id,
        machine_id=cfg.machine_id,
        key_path=cfg.key_path,
        jwt_cache_path=cfg.jwt_cache_path,
        server_pubkey_path=cfg.server_pubkey_path,
        keyring_service=_keyring_service_for(cfg.profile),
    )
    ep = _build_encryption_provider()
    # Expiry-aware token selection: prefer the cached JWT ONLY when it has
    # more than a 60s safety margin until exp. Otherwise force a refresh
    # before the request leaves the laptop. Combined with the Pusher's
    # 401-reactive retry this keeps sync from stalling on a stale token.
    pusher = Pusher(
        server_url=cfg.server_url,
        tenant_id=cfg.tenant_id,
        principal_id=cfg.principal_id,
        machine_id=cfg.machine_id,
        daemon_version=_daemon_version(),
        encryption_provider=ep,
        queue=queue,
        jwt_provider=lambda: jwt_client.current_valid(margin_s=60) or jwt_client.refresh_now(),
        refresh_jwt=jwt_client.refresh_now,
    )
    watch_target = Path.home() / ".codex" / "auth.json"

    def _refresh_jwt() -> None:
        jwt_client.refresh_now()

    runner = DaemonRunner(
        source_watch_target=watch_target,
        queue=queue,
        pusher=pusher,
        jwt_refresh_every=45 * 60,
        status_path=paths["status"],
        jwt_refresh_callable=_refresh_jwt,
        # Pass the token's own expiry so refresh scheduling is driven by
        # the actual JWT lifetime (with a 60s safety margin) rather than a
        # fixed cadence. Fixes the startup-with-near-expired-token gap.
        jwt_expiry_provider=jwt_client.seconds_until_expiry,
        jwt_refresh_margin_s=60,
        subprocess_sources=DEFAULT_SUBPROCESS_SOURCES,
        subprocess_poll_every=5 * 60,
    )
    runner.run()


@daemon.command("status")
@click.option("--profile", default=None, help="Profile name (auto-selected if only one exists).")
def status_cmd(profile: str | None) -> None:
    """Print daemon status JSON; exit 0=healthy, 1=degraded, 2=stopped."""
    resolved_profile = _resolve_profile(profile, required_action="daemon status")
    paths = _profile_paths(resolved_profile)
    if not paths["status"].exists():
        click.echo("stopped")
        sys.exit(2)
    data = json.loads(paths["status"].read_text())
    click.echo(json.dumps(data, indent=2))
    if data["state"] == "healthy":
        sys.exit(0)
    if data["state"] == "degraded":
        sys.exit(1)
    sys.exit(2)


@daemon.command("list")
def list_cmd() -> None:
    """List enrolled profiles and their server URLs."""
    from nexus.bricks.auth.daemon.config import DaemonConfig, list_profiles

    profiles = list_profiles(_NEXUS_HOME)
    if not profiles:
        click.echo("(no profiles enrolled)")
        return
    rows = []
    for p in profiles:
        cfg_path = _profile_paths(p)["config"]
        try:
            cfg = DaemonConfig.load(cfg_path)
            rows.append((p, cfg.server_url, str(cfg.tenant_id), str(cfg.machine_id)))
        except Exception as exc:
            rows.append((p, f"<load failed: {exc}>", "?", "?"))
    col_widths = [
        max(len(r[i]) for r in rows + [("PROFILE", "SERVER", "TENANT_ID", "MACHINE_ID")])
        for i in range(4)
    ]
    header = ("PROFILE", "SERVER", "TENANT_ID", "MACHINE_ID")
    click.echo("  ".join(h.ljust(col_widths[i]) for i, h in enumerate(header)))
    for r in rows:
        click.echo("  ".join(r[i].ljust(col_widths[i]) for i in range(4)))


@daemon.command("install")
@click.option("--profile", default=None, help="Profile name (auto-selected if only one exists).")
def install_cmd(profile: str | None) -> None:
    """Install launchd plist / systemd user unit for this profile."""
    from nexus.bricks.auth.daemon.installer import install

    resolved_profile = _resolve_profile(profile, required_action="daemon install")
    paths = _profile_paths(resolved_profile)
    unit_path = install(
        executable=sys.executable,
        config_path=paths["config"],
        profile=resolved_profile,
    )
    click.echo(f"installed: {unit_path}")


@daemon.command("uninstall")
@click.option("--profile", default=None, help="Profile name (auto-selected if only one exists).")
def uninstall_cmd(profile: str | None) -> None:
    """Remove launchd plist / systemd unit for this profile."""
    from nexus.bricks.auth.daemon.installer import uninstall

    resolved_profile = _resolve_profile(profile, required_action="daemon uninstall")
    uninstall(profile=resolved_profile)
    click.echo(f"uninstalled: profile={resolved_profile}")


def _daemon_version() -> str:
    from nexus import __version__

    return __version__


class _DaemonEnvelope:
    """Adapter: expose ``encrypt(plaintext, *, tenant_id, aad)`` on top of an
    ``EncryptionProvider`` (KEK wrap/unwrap) + ``AESGCMEnvelope`` (DEK).

    The daemon's ``Pusher`` expects a single ``encrypt(...)`` call that returns
    the full 5-field envelope. The repo's ``EncryptionProvider`` protocol only
    wraps/unwraps a DEK; the DEK is generated per-call and the plaintext is
    encrypted with it via ``AESGCMEnvelope``.
    """

    def __init__(self, provider: Any) -> None:
        from nexus.bricks.auth.envelope import AESGCMEnvelope

        self._provider = provider
        self._aes = AESGCMEnvelope()

    def encrypt(self, plaintext: bytes, *, tenant_id: uuid.UUID, aad: bytes) -> Any:
        import secrets
        from dataclasses import dataclass

        @dataclass(frozen=True)
        class _Envelope:
            ciphertext: bytes
            wrapped_dek: bytes
            nonce: bytes
            aad: bytes
            kek_version: int

        dek = secrets.token_bytes(32)
        wrapped_dek, kek_version = self._provider.wrap_dek(dek, tenant_id=tenant_id, aad=aad)
        nonce, ciphertext = self._aes.encrypt(dek, plaintext, aad=aad)
        return _Envelope(
            ciphertext=ciphertext,
            wrapped_dek=wrapped_dek,
            nonce=nonce,
            aad=aad,
            kek_version=kek_version,
        )


def _build_encryption_provider() -> Any:
    """Build the envelope helper for the Pusher from ``NEXUS_KMS_PROVIDER``.

    MVP only supports ``memory`` (``InMemoryEncryptionProvider``).
    """
    provider_name = os.environ.get("NEXUS_KMS_PROVIDER", "memory")
    if provider_name == "memory":
        from nexus.bricks.auth.envelope_providers.in_memory import (
            InMemoryEncryptionProvider,
        )

        return _DaemonEnvelope(InMemoryEncryptionProvider())
    raise click.ClickException(
        f"unsupported NEXUS_KMS_PROVIDER={provider_name!r}; MVP supports only 'memory'"
    )


__all__ = ["daemon"]
