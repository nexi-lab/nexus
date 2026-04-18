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
from rich.table import Table

from nexus.cli.theme import console
from nexus.contracts.exceptions import AuthenticationError
from nexus.contracts.unified_auth import AuthStatus
from nexus.fs._output import OutputOptions, add_output_options, render_output

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Production factory — tests monkeypatch this at the module level
# ---------------------------------------------------------------------------


def _build_auth_service() -> Any:
    """Build a UnifiedAuthService wired for production use."""
    from pathlib import Path

    oauth_module = importlib.import_module("nexus.bricks.auth.oauth.credential_service")
    unified_module = importlib.import_module("nexus.bricks.auth.unified_service")
    fs_oauth_module = importlib.import_module("nexus.fs._oauth_support")
    profile_store_module = importlib.import_module("nexus.bricks.auth.profile_store")
    oauth_service = oauth_module.OAuthCredentialService(
        token_manager=fs_oauth_module.get_token_manager()
    )
    db_path = Path("~/.nexus/auth_profiles.db").expanduser()
    profile_store = profile_store_module.SqliteAuthProfileStore(db_path)
    return unified_module.UnifiedAuthService(
        oauth_service=oauth_service,
        profile_store=profile_store,
    )


# ---------------------------------------------------------------------------
# Click group
# ---------------------------------------------------------------------------


@click.group(name="auth")
def auth() -> None:
    """Manage authentication for connected services."""


@auth.group(name="pool")
def pool() -> None:
    """Inspect and manage credential pools."""


@pool.command("status")
@click.argument("provider", type=str)
def pool_status(provider: str) -> None:
    """Show per-profile pool state for a provider.

    Displays each configured profile, its current status (ok / cooldown /
    disabled), failure count, and cooldown expiry if applicable.

    Note: runtime cooldown state (failure counts, cooldown timers) reflects
    the current process's in-memory pool. Persistent pool state across restarts
    requires Issue #3722 (SqliteAuthProfileStore) to land.

    Example:
        nexus auth pool status openai
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
            f"Run 'nexus auth list' to see all providers."
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


# ---------------------------------------------------------------------------
# auth list — profile store is the sole authoritative read path (#3741)
# ---------------------------------------------------------------------------


@auth.command("list")
@add_output_options
def list_auth(output_opts: OutputOptions) -> None:
    """List configured auth across services.

    Reads from UnifiedAuthService (OAuth, secret, and native discovery).
    The profile-store dual-read fallback was removed in Phase 4 (#3741).
    """
    data: list[dict[str, str]] = []

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
        logger.debug("Auth service unavailable: %s", exc)
        data.append(
            {
                "provider": "(error)",
                "account": "",
                "source": "error",
                "status": "degraded",
                "last_used": f"Auth check failed: {exc}",
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


# ---------------------------------------------------------------------------
# auth doctor
# ---------------------------------------------------------------------------


@auth.command(name="doctor")
def auth_doctor() -> None:
    """Show auth-related health across all configured services."""
    from nexus.bricks.auth.doctor import run_doctor

    exit_code = run_doctor(_build_auth_service())
    raise click.exceptions.Exit(exit_code)


# ---------------------------------------------------------------------------
# auth migrate (Phase 1 flow + Phase 4 finalization)
# ---------------------------------------------------------------------------


def _run_migrate_finalize() -> None:
    """Execute the --finalize branch: verify parity then delete legacy rows."""
    from nexus.bricks.auth.migrate import finalize_migration

    service = _build_auth_service()
    legacy, profile_store, backend = service.migration_components()
    result = finalize_migration(
        legacy_store=legacy,
        profile_store=profile_store,
        backend=backend,
    )
    if result.ok:
        console.print(
            f"[nexus.success]finalized: deleted {len(result.deleted)} legacy row(s)[/nexus.success]"
        )
        raise click.exceptions.Exit(0)
    for f in result.failures:
        console.print(f"[nexus.error]{f.profile_id}: {f.detail}[/nexus.error]")
    raise click.exceptions.Exit(1)


@auth.command("migrate")
@click.option("--apply", is_flag=True, default=False, help="Actually copy rows (default: dry-run)")
@click.option(
    "--finalize",
    is_flag=True,
    default=False,
    help="Verify parity with legacy store and delete legacy rows (Phase 4 finalization).",
)
def auth_migrate(apply: bool, finalize: bool) -> None:
    """Migrate OAuth credentials to the unified auth-profile store.

    Dry-run by default — prints what would be copied without writing.
    Pass --apply to actually copy rows into the new store.
    Pass --finalize to verify parity and delete legacy rows (Phase 4).

    This is Phase 1 of the auth unification (#3722). Migration is copy-only:
    the old store is never modified or deleted until --finalize is passed.
    """
    if apply and finalize:
        raise click.UsageError("--apply and --finalize are mutually exclusive.")

    if finalize:
        _run_migrate_finalize()
        return  # _run_migrate_finalize raises Exit internally
    from pathlib import Path

    from nexus.bricks.auth.migrate import build_migration_plan, execute_migration
    from nexus.bricks.auth.profile_store import SqliteAuthProfileStore
    from nexus.fs._oauth_support import get_token_manager

    # Guard: refuse to run if the source store is a shared/remote DB.
    # Check all env vars that get_token_manager() / get_database_url() consult.
    for env_var in ("NEXUS_DATABASE_URL", "POSTGRES_URL", "DATABASE_URL"):
        db_url = os.environ.get(env_var, "")
        if db_url and not db_url.startswith("sqlite"):
            raise click.ClickException(
                "auth migrate only supports local SQLite deployments. "
                f"Detected {env_var}={db_url!r}. "
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


# ---------------------------------------------------------------------------
# auth migrate-to-postgres (epic #3788 Phase F)
# ---------------------------------------------------------------------------


@auth.command("migrate-to-postgres")
@click.option(
    "--db-url",
    required=True,
    help="PostgreSQL URL (e.g. postgresql+psycopg2://user:pw@host:5432/db).",
)
@click.option(
    "--tenant",
    required=True,
    help="Tenant name; created if it does not exist.",
)
@click.option(
    "--principal",
    required=True,
    help="Principal external_sub (OIDC sub / keypair fingerprint); created if absent.",
)
@click.option(
    "--principal-kind",
    default="human",
    type=click.Choice(["human", "agent", "machine"]),
    help="Principal kind used when creating a new principal row.",
)
@click.option(
    "--auth-method",
    default="bootstrap",
    help="Auth method recorded on the principal_alias row.",
)
@click.option(
    "--source-db",
    type=click.Path(),
    default=None,
    help="Path to SQLite auth_profiles.db (default: ~/.nexus/auth_profiles.db).",
)
@click.option(
    "--apply",
    is_flag=True,
    default=False,
    help="Actually copy rows (default: dry-run).",
)
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help="Overwrite rows that already exist in the target store.",
)
def auth_migrate_to_postgres(
    db_url: str,
    tenant: str,
    principal: str,
    principal_kind: str,
    auth_method: str,
    source_db: str | None,
    apply: bool,
    force: bool,
) -> None:
    """Migrate local SQLite auth profiles into a multi-tenant Postgres store.

    Part of epic #3788 (Phase F). Copy-only by default — the SQLite source is
    never modified. Safe to rerun: rows already in the target are skipped
    unless --force is passed.

    Example (dry-run, then apply)::

        nexus auth migrate-to-postgres \\
            --db-url postgresql+psycopg2://postgres:pw@host/nexus \\
            --tenant acme --principal alice@acme.com

        nexus auth migrate-to-postgres ... --apply
    """
    from pathlib import Path

    from sqlalchemy import create_engine

    from nexus.bricks.auth.postgres_migrate import (
        build_migration_plan,
        execute_migration,
    )
    from nexus.bricks.auth.postgres_profile_store import (
        PostgresAuthProfileStore,
        ensure_principal,
        ensure_schema,
        ensure_tenant,
    )
    from nexus.bricks.auth.profile_store import SqliteAuthProfileStore

    sqlite_path = (
        Path(source_db).expanduser()
        if source_db
        else Path("~/.nexus/auth_profiles.db").expanduser()
    )
    if not sqlite_path.exists():
        raise click.ClickException(f"Source SQLite DB not found: {sqlite_path}")

    engine = create_engine(db_url, future=True)
    try:
        ensure_schema(engine)
        tenant_id = ensure_tenant(engine, tenant)
        principal_id = ensure_principal(
            engine,
            tenant_id=tenant_id,
            kind=principal_kind,
            external_sub=principal,
            auth_method=auth_method,
        )

        source = SqliteAuthProfileStore(sqlite_path)
        target = PostgresAuthProfileStore(
            db_url,
            tenant_id=tenant_id,
            principal_id=principal_id,
            engine=engine,
        )
        try:
            plan = build_migration_plan(source, target, force=force)
            result = execute_migration(plan, source, target, apply=apply)

            if not apply:
                console.print("[bold]Dry-run[/bold] (pass --apply to write):\n")

            for entry in result.entries:
                if entry.action in ("copy", "overwrite"):
                    style = "nexus.success" if apply else "nexus.info"
                    verb = {
                        ("copy", True): "Copied",
                        ("copy", False): "Would copy",
                        ("overwrite", True): "Overwrote",
                        ("overwrite", False): "Would overwrite",
                    }[(entry.action, apply)]
                    console.print(f"  [{style}]{verb}[/{style}] {entry.profile_id}")
                elif entry.action == "skip_exists":
                    console.print(f"  [nexus.muted]Skip (exists)[/nexus.muted] {entry.profile_id}")
                elif entry.action == "error":
                    console.print(
                        f"  [nexus.error]Error[/nexus.error] {entry.profile_id}: {entry.reason}"
                    )

            console.print(
                f"\n{'Dry-run' if result.dry_run else 'Result'}: "
                f"tenant={tenant} principal={principal} "
                f"{result.copied} copied, {result.skipped} skipped, "
                f"{result.errors} errors"
            )

            if result.errors > 0:
                raise click.exceptions.Exit(1)
        finally:
            source.close()
            target.close()
    finally:
        engine.dispose()


# Order preserves registration for parity testing.


__all__ = ["auth", "pool"]
