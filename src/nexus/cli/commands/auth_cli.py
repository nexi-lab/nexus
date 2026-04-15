"""Unified auth CLI over OAuth, stored secrets, and native providers."""

from __future__ import annotations

import asyncio
import importlib as _il
import os
import sys
from collections.abc import Callable
from typing import cast

import click
from rich.table import Table

from nexus.bricks.auth.unified_service import UnifiedAuthService
from nexus.cli.commands.oauth import get_token_manager, setup_x
from nexus.cli.theme import console
from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.contracts.unified_auth import AuthStatus, CredentialKind

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

_GOOGLE_SERVICE_SCOPES: dict[str, list[str]] = {
    "gws": [
        "https://www.googleapis.com/auth/drive",
        "https://www.googleapis.com/auth/drive.file",
        "https://www.googleapis.com/auth/documents",
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/gmail.modify",
        "https://www.googleapis.com/auth/calendar",
        "https://www.googleapis.com/auth/chat.spaces.readonly",
    ],
    "google-drive": [
        "https://www.googleapis.com/auth/drive",
        "https://www.googleapis.com/auth/drive.file",
    ],
    "gmail": ["https://www.googleapis.com/auth/gmail.modify"],
    "google-calendar": ["https://www.googleapis.com/auth/calendar"],
}

_GOOGLE_SERVICE_PROVIDER_NAMES: dict[str, str] = {
    "gws": "google",
    "google-drive": "google-drive",
    "gmail": "gmail",
    "google-calendar": "google-calendar",
}


def _build_auth_service() -> UnifiedAuthService:
    from nexus.bricks.auth.oauth.credential_service import OAuthCredentialService
    from nexus.cli.commands.oauth import get_token_manager

    oauth_service = OAuthCredentialService(token_manager=get_token_manager())
    return UnifiedAuthService(oauth_service=oauth_service)


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


def _prompt_for_secret_values(
    service: UnifiedAuthService,
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
    kind: CredentialKind,
    store_path: str,
    fields: list[str] | None = None,
    *,
    source: str = "stored",
) -> None:
    console.print(
        f"[nexus.success]ok[/nexus.success] {service_name}: {source} {kind.value} auth is configured"
    )
    console.print(f"[nexus.muted]Secret store: {store_path}[/nexus.muted]")
    if fields:
        console.print(f"[nexus.muted]Fields: {', '.join(fields)}[/nexus.muted]")
    console.print(f"[nexus.muted]Next: nexus auth test {service_name}[/nexus.muted]")


def _ensure_google_oauth_env() -> None:
    if os.getenv("NEXUS_OAUTH_GOOGLE_CLIENT_ID"):
        return
    raise click.ClickException(
        "Google OAuth client ID not provided. Set NEXUS_OAUTH_GOOGLE_CLIENT_ID "
        "and NEXUS_OAUTH_GOOGLE_CLIENT_SECRET first."
    )


def _ensure_x_oauth_env() -> None:
    if os.getenv("NEXUS_OAUTH_X_CLIENT_ID"):
        return
    raise click.ClickException("X OAuth client ID not provided. Set NEXUS_OAUTH_X_CLIENT_ID first.")


def _resolve_user_email(user_email: str | None) -> str:
    if user_email:
        return user_email
    return str(click.prompt("user_email"))


def _run_google_oauth_setup(service_name: str, user_email: str) -> None:
    GoogleOAuthProvider = _il.import_module(
        "nexus.bricks.auth.oauth.providers.google"
    ).GoogleOAuthProvider

    client_id = os.getenv("NEXUS_OAUTH_GOOGLE_CLIENT_ID")
    client_secret = os.getenv("NEXUS_OAUTH_GOOGLE_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise click.ClickException(
            "Google OAuth client ID/secret not provided. Set "
            "NEXUS_OAUTH_GOOGLE_CLIENT_ID and NEXUS_OAUTH_GOOGLE_CLIENT_SECRET first."
        )

    provider = GoogleOAuthProvider(
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri="http://localhost",
        scopes=_GOOGLE_SERVICE_SCOPES.get(service_name, _GOOGLE_SERVICE_SCOPES["gws"]),
        provider_name=_GOOGLE_SERVICE_PROVIDER_NAMES.get(service_name, "google"),
    )
    auth_url = provider.get_authorization_url()

    console.print("\n[bold nexus.success]Google OAuth Setup[/bold nexus.success]")
    console.print(f"\n[bold]Service:[/bold] {service_name}")
    console.print(f"[bold]User:[/bold] {user_email}")
    console.print("\n[bold nexus.warning]Step 1:[/bold nexus.warning] Visit this URL to authorize:")
    console.print(f"\n{auth_url}\n")
    console.print(
        "[bold nexus.warning]Step 2:[/bold nexus.warning] After granting permission, the browser will redirect to localhost."
    )
    console.print(
        "[bold nexus.warning]Step 3:[/bold nexus.warning] Copy the `code` parameter from that URL."
    )
    auth_code = click.prompt("\nEnter authorization code")

    async def _exchange_and_store() -> str:
        credential = await provider.exchange_code(auth_code)
        manager = get_token_manager()
        cred_id = await manager.store_credential(
            provider=_GOOGLE_SERVICE_PROVIDER_NAMES.get(service_name, "google"),
            user_email=user_email,
            credential=credential,
            zone_id=ROOT_ZONE_ID,
            created_by=user_email,
        )
        manager.close()
        return cred_id

    try:
        cred_id = asyncio.run(_exchange_and_store())
        console.print(
            f"\n[nexus.success]ok[/nexus.success] stored Google OAuth credentials for {user_email}"
        )
        console.print(f"[nexus.muted]Credential ID: {cred_id}[/nexus.muted]")
    except Exception as exc:
        console.print(f"\n[nexus.error]OAuth setup failed:[/nexus.error] {exc}")
        sys.exit(1)


def _run_x_oauth_setup(user_email: str) -> None:
    callback = cast(
        Callable[[str | None, str | None, str, str | None, str | None], None],
        setup_x.callback,
    )
    callback(
        None,
        None,
        user_email,
        None,
        None,
    )


@click.group(name="auth")
def auth() -> None:
    """Unified auth commands for OAuth and cloud backends."""


@auth.command("list")
def list_auth() -> None:
    """List configured auth across services."""
    service = _build_auth_service()
    summaries = asyncio.run(service.list_summaries())

    table = Table(title="Unified Auth", show_header=True, header_style="nexus.table_header")
    table.add_column("Service", style="nexus.success")
    table.add_column("Kind", style="nexus.value")
    table.add_column("Status", style="nexus.warning")
    table.add_column("Source", style="nexus.reference")
    table.add_column("Message")

    for summary in summaries:
        table.add_row(
            summary.service,
            summary.kind.value,
            summary.status.value,
            summary.source,
            summary.message,
        )

    console.print(table)
    console.print(f"[nexus.muted]Secret store: {service.secret_store_path}[/nexus.muted]")


@auth.command("test")
@click.argument("service_name", type=str)
@click.option("--user-email", type=str, default=None, help="OAuth account email override.")
def test_auth(service_name: str, user_email: str | None) -> None:
    """Validate auth for a service."""
    service = _build_auth_service()
    result = asyncio.run(service.test_service(service_name, user_email=user_email))
    if result.get("success"):
        console.print(f"[nexus.success]ok[/nexus.success] {service_name}: {result.get('message')}")
        return
    raise click.ClickException(f"{service_name}: {result.get('message')}")


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

    if auth_type == "oauth":
        if service_name in {"gws", "google-drive", "gmail", "google-calendar"}:
            user_email = _resolve_user_email(user_email)
            _ensure_google_oauth_env()
            _run_google_oauth_setup(service_name, user_email)
            return
        if service_name == "x":
            user_email = _resolve_user_email(user_email)
            _ensure_x_oauth_env()
            _run_x_oauth_setup(user_email)
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


@auth.command("disconnect")
@click.argument("service_name", type=str)
def disconnect_auth(service_name: str) -> None:
    """Remove stored secret/native auth for a service."""
    service = _build_auth_service()
    removed = service.disconnect(service_name)
    if not removed:
        raise click.ClickException(f"No stored auth found for '{service_name}'.")
    console.print(f"[nexus.success]ok[/nexus.success] Removed stored auth for {service_name}")


@auth.group("pool")
def auth_pool() -> None:
    """Manage credential pool state (multi-account failover, cooldowns)."""


@auth_pool.command("status")
@click.argument("provider", type=str)
def pool_status(provider: str) -> None:
    """Show per-profile pool state for a provider.

    Displays each configured profile, its current status (ok / cooldown /
    disabled), failure count, and cooldown expiry if applicable.

    Note: runtime cooldown state (failure counts, cooldown timers) reflects
    the current process's in-memory pool. Persistent pool state across restarts
    requires Issue #3722 (SqliteAuthProfileStore) to land.

    Example:
        nexus-fs auth pool status openai
    """

    # Build a minimal profile list from the existing credential records.
    # Until #3722 lands, we read from UnifiedAuthService and populate an
    # InMemoryAuthProfileStore — this gives correct static data but no
    # runtime cooldown history from previous processes.
    service = _build_auth_service()
    summaries = asyncio.run(service.list_summaries())

    provider_summaries = [s for s in summaries if s.service == provider]
    if not provider_summaries:
        # Try prefix match (e.g. "google" matches "gmail", "google-drive", etc.)
        provider_summaries = [s for s in summaries if s.service.startswith(provider)]

    if not provider_summaries:
        raise click.ClickException(
            f"No configured profiles found for provider '{provider}'. "
            f"Run 'nexus-fs auth list' to see all providers."
        )

    table = Table(
        title=f"Pool: {provider}",
        show_header=True,
        header_style="nexus.table_header",
    )
    table.add_column("Status", style="nexus.success", width=10)
    table.add_column("Account", style="nexus.value")
    table.add_column("Source", style="nexus.reference")
    table.add_column("Failures", justify="right")
    table.add_column("Cooldown")

    for summary in provider_summaries:
        from nexus.contracts.unified_auth import AuthStatus

        is_ok = summary.status == AuthStatus.AUTHED
        status_str = (
            "[nexus.success]ok[/nexus.success]"
            if is_ok
            else f"[nexus.warning]{summary.status.value}[/nexus.warning]"
        )
        account = summary.details.get("email") or summary.details.get("user") or "default"
        table.add_row(
            status_str,
            str(account),
            summary.source,
            "0",  # runtime failure counts require #3722 for persistence
            "—",  # runtime cooldown requires #3722 for persistence
        )

    console.print(table)
    console.print(
        "[nexus.muted]Runtime cooldown state (failures, timers) requires "
        "Issue #3722 (persistent AuthProfileStore) to persist across restarts.[/nexus.muted]"
    )


@auth.command("doctor")
def auth_doctor() -> None:
    """Show only auth-related doctor results."""
    service = _build_auth_service()
    summaries = asyncio.run(service.list_summaries())
    failures = [s for s in summaries if s.status not in {AuthStatus.AUTHED, AuthStatus.UNKNOWN}]
    for summary in summaries:
        style = "nexus.success" if summary.status == AuthStatus.AUTHED else "nexus.warning"
        console.print(
            f"[{style}]{summary.service}[/{style}] {summary.status.value}: {summary.message}"
        )
    if failures:
        raise click.ClickException("One or more services need auth setup.")


@auth.command("migrate")
@click.option("--apply", is_flag=True, default=False, help="Actually copy rows (default: dry-run)")
def auth_migrate(apply: bool) -> None:
    """Migrate OAuth credentials to the unified auth-profile store.

    Dry-run by default — prints what would be copied without writing.
    Pass --apply to actually copy rows into the new store.

    This is Phase 1 of the auth unification (#3722). Migration is copy-only:
    the old store is never modified or deleted.
    """
    # Guard: refuse to run if the source store is a shared/remote DB,
    # since the destination is always host-local ~/.nexus/auth_profiles.db.
    import os
    from pathlib import Path

    from nexus.bricks.auth.migrate import build_migration_plan, execute_migration
    from nexus.bricks.auth.profile_store import SqliteAuthProfileStore

    db_url = os.environ.get("NEXUS_DATABASE_URL", "")
    if db_url and not db_url.startswith("sqlite"):
        raise click.ClickException(
            "auth migrate only supports local SQLite deployments. "
            f"Detected NEXUS_DATABASE_URL={db_url!r}. "
            "Shared-DB migration will be supported in Phase 4 (#3741)."
        )

    # Collect old credentials across ALL zones — pass zone_id=None to
    # TokenManager.list_credentials() to avoid filtering to root only.
    token_manager = get_token_manager()
    old_creds = asyncio.run(token_manager.list_credentials(zone_id=None))

    if not old_creds:
        console.print("[nexus.muted]No OAuth credentials found to migrate.[/nexus.muted]")
        return

    # Open (or create) the new profile store
    db_path = Path("~/.nexus/auth_profiles.db").expanduser()
    store = SqliteAuthProfileStore(db_path)
    try:
        plan = build_migration_plan(old_creds, store)
        result = execute_migration(plan, old_creds, store, apply=apply)

        if not apply:
            console.print("[bold]Dry-run[/bold] (pass --apply to write):\n")

        for entry in result.entries:
            if entry.action == "copy":
                style = "nexus.success" if apply else "nexus.info"
                verb = "Copied" if apply else "Would copy"
                console.print(f"  [{style}]{verb}[/{style}] {entry.profile_id}")
            elif entry.action == "skip_exists":
                console.print(f"  [nexus.muted]Skip (exists)[/nexus.muted] {entry.profile_id}")
            elif entry.action == "skip_unmappable":
                console.print(
                    f"  [nexus.warning]Skip (unmappable)[/nexus.warning] "
                    f"{entry.provider}/{entry.user_email}: {entry.reason}"
                )
            elif entry.action == "error":
                console.print(
                    f"  [nexus.error]Error[/nexus.error] {entry.profile_id}: {entry.reason}"
                )

        console.print(
            f"\n{'Dry-run' if result.dry_run else 'Result'}: "
            f"{result.copied} copied, {result.skipped} skipped, {result.errors} errors"
        )
    finally:
        store.close()
