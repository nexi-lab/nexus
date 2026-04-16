"""Shared Click `auth` CLI command handlers.

Single source of truth for `nexus auth` and `nexus-fs auth`. Both of those
entry points import the `auth` group from this module.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
from typing import Any

import click
from rich.console import Console
from rich.table import Table

from nexus.fs._output import OutputOptions, add_output_options, render_output

logger = logging.getLogger(__name__)
console = Console()


# ---------------------------------------------------------------------------
# Production factory — tests monkeypatch this at the module level
# ---------------------------------------------------------------------------


def _build_auth_service() -> Any:
    """Build a UnifiedAuthService wired for production use."""
    oauth_module = importlib.import_module("nexus.bricks.auth.oauth.credential_service")
    unified_module = importlib.import_module("nexus.bricks.auth.unified_service")
    fs_oauth_module = importlib.import_module("nexus.fs._oauth_support")
    oauth_service = oauth_module.OAuthCredentialService(
        token_manager=fs_oauth_module.get_token_manager()
    )
    return unified_module.UnifiedAuthService(oauth_service=oauth_service)


# ---------------------------------------------------------------------------
# Click group
# ---------------------------------------------------------------------------


@click.group(name="auth")
def auth() -> None:
    """Manage authentication for connected services."""


@auth.group(name="pool")
def pool() -> None:
    """Inspect and manage credential pools."""


# ---------------------------------------------------------------------------
# Helpers for the dual-read auth list
# (ported verbatim from nexus.fs._auth_cli — fs version is the superset)
# ---------------------------------------------------------------------------


def _try_profile_store_list() -> list[Any] | None:
    """Try reading from the unified profile store.

    Returns the list of AuthProfile objects on success, or None if the store
    is unavailable, empty, or raises any exception.
    """
    from nexus.fs._external_sync_boot import ensure_external_sync, list_profiles

    ensure_external_sync()
    return list_profiles()


def _format_status(profile: Any) -> str:
    """Format status string for a profile row."""
    import datetime as _dt

    now = _dt.datetime.now(_dt.UTC)
    stats = profile.usage_stats

    if stats.disabled_until is not None:
        disabled_until = stats.disabled_until
        if disabled_until.tzinfo is None:
            disabled_until = disabled_until.replace(tzinfo=_dt.UTC)
        if disabled_until > now:
            return "disabled"

    if stats.cooldown_until is not None:
        cooldown_until = stats.cooldown_until
        if cooldown_until.tzinfo is None:
            cooldown_until = cooldown_until.replace(tzinfo=_dt.UTC)
        if cooldown_until > now:
            remaining = cooldown_until - now
            total_minutes = int(remaining.total_seconds() / 60)
            reason = stats.cooldown_reason.value if stats.cooldown_reason else "unknown"
            if total_minutes > 60:
                hours = total_minutes / 60
                return f"cooldown  {reason} · {hours:.1f}h left"
            return f"cooldown  {reason} · {total_minutes}m left"

    if profile.last_synced_at is None:
        return "not yet synced"

    # Check if sync is stale (last_synced_at + sync_ttl_seconds < now)
    last_synced = profile.last_synced_at
    if last_synced.tzinfo is None:
        last_synced = last_synced.replace(tzinfo=_dt.UTC)
    stale_after = last_synced + _dt.timedelta(seconds=profile.sync_ttl_seconds)
    if stale_after < now:
        return "stale"

    return "ok"


def _format_relative_time(dt: Any) -> str:
    """Format a datetime as a human-readable relative time string."""
    import datetime as _dt

    if dt is None:
        return "never"

    now = _dt.datetime.now(_dt.UTC)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_dt.UTC)

    delta = now - dt
    seconds = int(delta.total_seconds())

    if seconds < 60:
        return "just now"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"
    days = hours // 24
    return f"{days}d ago"


def _source_display(backend: str) -> str:
    """Map backend name to a short display string."""
    if backend == "external-cli":
        return "external"
    if backend == "nexus-token-manager":
        return "nexus"
    return backend


# ---------------------------------------------------------------------------
# auth list (dual-read: profile store primary, legacy fallback)
# ---------------------------------------------------------------------------


@auth.command("list")
@add_output_options
def list_auth(output_opts: OutputOptions) -> None:
    """List configured auth across services.

    Merges two sources:
      1. Profile store (external-CLI synced profiles + migrated OAuth profiles)
      2. Legacy UnifiedAuthService summaries (OAuth, secret, native discovery)

    Profile store entries take precedence when both sources have the same
    provider. Legacy entries fill in services not yet in the profile store.
    """
    data: list[dict[str, str]] = []

    # 1. Profile store entries (external-cli synced + migrated)
    profiles = _try_profile_store_list()
    if profiles is not None:
        for p in profiles:
            data.append(
                {
                    "provider": p.provider,
                    "account": p.account_identifier,
                    "source": _source_display(p.backend),
                    "status": _format_status(p),
                    "last_used": _format_relative_time(p.usage_stats.last_used_at),
                }
            )

    # 2. Legacy summaries — always included alongside profile store entries.
    # The external-cli adapter (Phase 2) only discovers inline-key AWS profiles,
    # so legacy summaries still provide valuable info for SSO/role/credential_process
    # setups and non-AWS services. No provider dedup — both sources complement.
    try:
        service = _build_auth_service()
        summaries = asyncio.run(service.list_summaries())
        for s in summaries:
            data.append(
                {
                    "provider": s.service,
                    "account": "",
                    "source": s.source,
                    "status": s.status.value,
                    "last_used": s.message,
                    # Backward-compatible keys for JSON consumers
                    "service": s.service,
                    "kind": s.kind.value,
                    "message": s.message,
                }
            )
    except Exception as exc:
        logger.debug("Legacy auth service unavailable: %s", exc)
        data.append(
            {
                "provider": "(legacy)",
                "account": "",
                "source": "error",
                "status": "degraded",
                "last_used": f"Legacy auth check failed: {exc}",
            }
        )

    if not data:
        render_output(
            data=[],
            output_opts=output_opts,
            human_formatter=lambda _: console.print(
                "[dim]No auth configured. Run `auth connect` to set up.[/dim]"
            ),
        )
        return

    def _human_display(_data: object) -> None:
        table = Table(title="Unified Auth", show_header=True, header_style="bold cyan")
        table.add_column("Provider", style="green")
        table.add_column("Account", style="cyan")
        table.add_column("Source", style="blue")
        table.add_column("Status", style="yellow")
        table.add_column("Last used")
        for row in data:
            table.add_row(
                row["provider"],
                row["account"],
                row["source"],
                row["status"],
                row["last_used"],
            )
        console.print(table)

    render_output(data=data, output_opts=output_opts, human_formatter=_human_display)


# Subcommands wired in later Phase-4 tasks (test, connect, disconnect,
# doctor, migrate). Order preserves registration for parity testing.


__all__ = ["auth", "pool"]
