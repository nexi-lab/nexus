"""Shared Click `auth` CLI command handlers.

Single source of truth for `nexus auth` and `nexus-fs auth`. Both of those
entry points import the `auth` group from this module.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import os
import re
from typing import Any

import click
from rich.console import Console
from rich.table import Table

from nexus.contracts.exceptions import AuthenticationError
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


# ---------------------------------------------------------------------------
# Helpers for auth test output
# (ported verbatim from nexus.fs._auth_cli — fs version is the superset)
# ---------------------------------------------------------------------------


def _raise_authentication_error(
    exc: AuthenticationError, output_opts: OutputOptions | None = None
) -> None:
    """Render an AuthenticationError and exit non-zero."""
    if output_opts is not None and output_opts.json_output:
        import json

        payload: dict[str, object] = {"error": "AuthenticationError", "detail": str(exc)}
        if exc.provider:
            payload["provider"] = exc.provider
        if exc.user_email:
            payload["user_email"] = exc.user_email
        if exc.auth_url:
            payload["auth_url"] = exc.auth_url
        console.print(json.dumps(payload))
        raise SystemExit(1)

    console.print(f"[red]Auth error:[/red] {exc}")
    if exc.provider or exc.user_email:
        account = (
            f"{exc.provider}:{exc.user_email}"
            if exc.provider and exc.user_email
            else (exc.provider or exc.user_email)
        )
        console.print(f"[dim]Account: {account}[/dim]")
    if exc.auth_url:
        console.print(f"[yellow]Re-authenticate:[/yellow] {exc.auth_url}")
    raise SystemExit(1)


def _print_target_readiness_summary(
    service_name: str,
    checks: list[dict[str, object]],
    *,
    user_email: str | None = None,
) -> None:
    ready = [str(check.get("target", "")) for check in checks if check.get("success")]
    failed = [check for check in checks if not check.get("success")]

    if ready:
        console.print(f"[green]Ready:[/green] {', '.join(ready)}")

    if not failed:
        console.print(f"[green]ok[/green] {service_name}: all checked targets are ready.")
        return

    console.print(
        f"[yellow]{service_name} is partially ready.[/yellow] "
        f"{len(failed)} target(s) still need action."
    )
    for check in failed:
        target = str(check.get("target", ""))
        message = str(check.get("message", ""))
        console.print(f"[red]Needs action:[/red] {target}: {message}")

    if service_name == "gws":
        email = user_email or os.environ.get("NEXUS_FS_USER_EMAIL")
        if not email:
            joined_messages = "\n".join(str(check.get("message", "")) for check in checks)
            match = re.search(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", joined_messages)
            email = match.group(0) if match else "you@example.com"
        console.print(
            "[bold]Next steps[/bold]\n"
            f"1. Run `nexus auth connect gws oauth --user-email {email}`\n"
            "2. Approve the requested Google scopes for the failing target(s)\n"
            "3. Re-run `nexus auth test gws`\n"
            "4. Use `nexus auth test gws --target <target>` to verify one target at a time"
        )


# ---------------------------------------------------------------------------
# auth test
# ---------------------------------------------------------------------------


@auth.command("test")
@click.argument("service_name", type=str)
@click.option("--user-email", type=str, default=None, help="OAuth account email override.")
@click.option(
    "--target",
    type=click.Choice(
        ["drive", "docs", "sheets", "gmail", "calendar", "chat"], case_sensitive=False
    ),
    default=None,
    help="Google Workspace target readiness check.",
)
@add_output_options
def test_auth(
    service_name: str,
    user_email: str | None,
    target: str | None,
    output_opts: OutputOptions,
) -> None:
    """Validate auth for a service."""
    service = _build_auth_service()
    try:
        result = asyncio.run(
            service.test_service(service_name, user_email=user_email, target=target)
        )
    except AuthenticationError as exc:
        _raise_authentication_error(exc, output_opts)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc

    data = {"service": service_name, **result}

    def _human_display(_data: object) -> None:
        checks = result.get("checks")
        if isinstance(checks, list) and checks:
            table = Table(
                title=f"{service_name} target readiness",
                show_header=True,
                header_style="bold cyan",
            )
            table.add_column("Target", style="green")
            table.add_column("Status", style="yellow")
            table.add_column("Source", style="blue")
            table.add_column("Message")
            for check in checks:
                table.add_row(
                    str(check.get("target", "")),
                    "ok" if check.get("success") else "error",
                    str(check.get("source", result.get("source", ""))),
                    str(check.get("message", "")),
                )
            console.print(table)
            _print_target_readiness_summary(service_name, checks, user_email=user_email)
            if not result.get("success"):
                raise SystemExit(1)
            return

        if result.get("success"):
            console.print(f"[green]ok[/green] {service_name}: {result.get('message')}")
            return
        raise click.ClickException(f"{service_name}: {result.get('message')}")

    render_output(
        data=data,
        output_opts=output_opts,
        human_formatter=_human_display,
    )

    if output_opts.json_output and not result.get("success"):
        raise SystemExit(1)


# ---------------------------------------------------------------------------
# Helpers for auth connect
# (ported from nexus.fs._auth_cli — fs version is the superset)
# ---------------------------------------------------------------------------

_SERVICE_AUTH_TYPES: dict[str, tuple[str, ...]] = {
    "s3": ("native", "secret"),
    "gcs": ("native", "secret"),
    "gws": ("oauth",),
    "google-drive": ("oauth",),
    "gmail": ("oauth",),
    "google-calendar": ("oauth",),
    "slack": ("oauth",),
    "x": ("oauth",),
}

_SERVICE_HELP: dict[str, dict[str, tuple[str, ...] | str]] = {
    "s3": {
        "recommended": "native",
        "native_steps": (
            "Run `aws configure`, set `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY`, or ensure your AWS profile works.",
            "Use `nexus auth connect s3 native` to tell Nexus to prefer the AWS provider chain.",
            "Run `nexus auth test s3` to verify the provider chain resolves.",
        ),
        "secret_steps": (
            "Gather `access_key_id` and `secret_access_key`.",
            "Run `nexus auth connect s3 secret` and enter the prompts, or pass `--set access_key_id=... --set secret_access_key=...`.",
            "Run `nexus auth test s3` to verify the stored credential shape.",
        ),
    },
    "gcs": {
        "recommended": "native",
        "native_steps": (
            "Run `gcloud auth application-default login` or set `GOOGLE_APPLICATION_CREDENTIALS`.",
            "Use `nexus auth connect gcs native` to tell Nexus to prefer ADC/native credentials.",
            "Run `nexus auth test gcs` to verify the provider chain resolves.",
        ),
        "secret_steps": (
            "Prepare a service-account JSON file or access token.",
            "Run `nexus auth connect gcs secret` and provide `credentials_path` or `access_token`.",
            "Run `nexus auth test gcs` to verify the stored credential shape.",
        ),
    },
    "gws": {
        "recommended": "oauth",
        "oauth_steps": (
            "Set `NEXUS_OAUTH_GOOGLE_CLIENT_ID` and `NEXUS_OAUTH_GOOGLE_CLIENT_SECRET`.",
            "Run `nexus auth connect gws oauth --user-email you@example.com`.",
            "Follow the browser/code flow, then run `nexus auth test gws --user-email you@example.com`.",
        ),
    },
    "google-drive": {
        "recommended": "oauth",
        "oauth_steps": (
            "Set `NEXUS_OAUTH_GOOGLE_CLIENT_ID` and `NEXUS_OAUTH_GOOGLE_CLIENT_SECRET`.",
            "Run `nexus auth connect google-drive oauth --user-email you@example.com`.",
            "Follow the browser/code flow, then run `nexus auth test google-drive --user-email you@example.com`.",
        ),
    },
    "gmail": {
        "recommended": "oauth",
        "oauth_steps": (
            "Set `NEXUS_OAUTH_GOOGLE_CLIENT_ID` and `NEXUS_OAUTH_GOOGLE_CLIENT_SECRET`.",
            "Run `nexus auth connect gmail oauth --user-email you@example.com`.",
            "Follow the browser/code flow, then run `nexus auth test gmail --user-email you@example.com`.",
        ),
    },
    "google-calendar": {
        "recommended": "oauth",
        "oauth_steps": (
            "Set `NEXUS_OAUTH_GOOGLE_CLIENT_ID` and `NEXUS_OAUTH_GOOGLE_CLIENT_SECRET`.",
            "Run `nexus auth connect google-calendar oauth --user-email you@example.com`.",
            "Follow the browser/code flow, then run `nexus auth test google-calendar --user-email you@example.com`.",
        ),
    },
    "x": {
        "recommended": "oauth",
        "oauth_steps": (
            "Set `NEXUS_OAUTH_X_CLIENT_ID` and optionally `NEXUS_OAUTH_X_CLIENT_SECRET`.",
            "Run `nexus auth connect x oauth --user-email you@example.com`.",
            "Follow the browser/code flow, then run `nexus auth test x --user-email you@example.com`.",
        ),
    },
}


def _parse_key_values(items: tuple[str, ...]) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for item in items:
        if "=" not in item:
            raise click.ClickException(f"Expected KEY=VALUE, got: {item}")
        key, value = item.split("=", 1)
        parsed[key.strip()] = value
    return parsed


def _choose_auth_type(service_name: str) -> str:
    supported = _SERVICE_AUTH_TYPES.get(service_name)
    if not supported:
        raise click.ClickException(f"Unknown auth service '{service_name}'.")
    if len(supported) == 1:
        return supported[0]
    default = str(_SERVICE_HELP.get(service_name, {}).get("recommended", supported[0]))
    console.print(f"[bold]Choose auth mode for {service_name}[/bold]")
    for mode in supported:
        suffix = " (recommended)" if mode == default else ""
        console.print(f"  - {mode}{suffix}")
    choice = click.prompt(
        "Auth type",
        type=click.Choice(list(supported), case_sensitive=False),
        default=default,
        show_choices=False,
    )
    return str(choice).lower()


def _print_steps(service_name: str, auth_type: str) -> None:
    guide = _SERVICE_HELP.get(service_name, {})
    steps = guide.get(f"{auth_type}_steps")
    if not steps:
        return
    console.print(f"[bold]Setup steps for {service_name} ({auth_type})[/bold]")
    for idx, step in enumerate(steps, start=1):
        console.print(f"{idx}. {step}")
    console.print("")


def _resolve_user_email(user_email: str | None) -> str:
    return user_email or str(click.prompt("user_email"))


def _prompt_for_secret_values(
    service: Any,
    service_name: str,
    pairs: tuple[str, ...],
) -> dict[str, str]:
    values = _parse_key_values(pairs)
    if values:
        return values
    spec = service.store_help_fields(service_name)
    prompt_fields = list(spec["required_fields"])
    if service_name == "gcs" and not prompt_fields:
        prompt_fields = ["credentials_path"]
    values = {}
    for field in prompt_fields:
        hide = "secret" in field or "token" in field or "key" in field
        values[field] = click.prompt(field, hide_input=hide)
    return values


def _print_connect_success(
    service_name: str,
    kind: Any,
    store_path: str,
    fields: list[str] | None = None,
    *,
    source: str = "stored",
) -> None:
    console.print(f"[green]ok[/green] {service_name}: {source} {kind.value} auth is configured")
    console.print(f"[dim]Secret store: {store_path}[/dim]")
    if fields:
        console.print(f"[dim]Fields: {', '.join(fields)}[/dim]")
    console.print(f"[dim]Next: nexus auth test {service_name}[/dim]")


# ---------------------------------------------------------------------------
# auth connect
# ---------------------------------------------------------------------------


@auth.command("connect")
@click.argument("service_name", type=str)
@click.argument(
    "auth_type",
    required=False,
    type=click.Choice(["oauth", "secret", "native"], case_sensitive=False),
)
@click.option("--set", "pairs", multiple=True, help="Secret field as KEY=VALUE. Repeat as needed.")
@click.option("--user-email", type=str, default=None, help="OAuth account email.")
def connect_auth(
    service_name: str,
    auth_type: str | None,
    pairs: tuple[str, ...],
    user_email: str | None,
) -> None:
    """Connect a service using OAuth, stored secrets, or native fallback."""
    auth_type = auth_type.lower() if auth_type else _choose_auth_type(service_name)
    service = _build_auth_service()
    _print_steps(service_name, auth_type)

    try:
        if auth_type == "oauth":
            user_email = _resolve_user_email(user_email)
            if service_name in {"gws", "google-drive", "gmail", "google-calendar"}:
                _oauth_support = importlib.import_module("nexus.fs._oauth_support")
                _oauth_support.run_google_oauth_setup(
                    user_email=user_email, service_name=service_name
                )
                return
            if service_name == "x":
                _oauth_support = importlib.import_module("nexus.fs._oauth_support")
                _oauth_support.run_x_oauth_setup(user_email=user_email)
                return
            raise click.ClickException(f"OAuth connect is not implemented for '{service_name}'.")

        if auth_type == "native":
            record = service.connect_native(service_name)
            _print_connect_success(
                service_name,
                record.kind,
                str(service.secret_store_path),
                source="native fallback",
            )
            return

        values = _prompt_for_secret_values(service, service_name, pairs)
        record = service.connect_secret(service_name, values)
        _print_connect_success(
            service_name,
            record.kind,
            str(service.secret_store_path),
            sorted(record.data),
        )
    except AuthenticationError as exc:
        _raise_authentication_error(exc)


# ---------------------------------------------------------------------------
# auth disconnect
# ---------------------------------------------------------------------------


@auth.command("disconnect")
@click.argument("service_name", type=str)
def disconnect_auth(service_name: str) -> None:
    """Remove stored secret/native auth for a service."""
    service = _build_auth_service()
    removed = service.disconnect(service_name)
    if not removed:
        raise click.ClickException(f"No stored auth found for '{service_name}'.")
    console.print(f"[green]ok[/green] Removed stored auth for {service_name}")


# Subcommands wired in later Phase-4 tasks (doctor, migrate).
# Order preserves registration for parity testing.


__all__ = ["auth", "pool"]
