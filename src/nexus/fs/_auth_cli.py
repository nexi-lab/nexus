"""Standalone unified auth CLI for nexus-fs."""

from __future__ import annotations

import asyncio

import click
from rich.table import Table

from nexus.bricks.auth.unified_service import UnifiedAuthService
from nexus.cli.utils import console
from nexus.contracts.unified_auth import AuthStatus, CredentialKind
from nexus.fs._oauth_support import get_token_manager, run_google_oauth_setup, run_x_oauth_setup

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
            "Use `nexus-fs auth connect s3 native` to tell Nexus to prefer the AWS provider chain.",
            "Run `nexus-fs auth test s3` to verify the provider chain resolves.",
        ),
        "secret_steps": (
            "Gather `access_key_id` and `secret_access_key`.",
            "Run `nexus-fs auth connect s3 secret` and enter the prompts, or pass `--set access_key_id=... --set secret_access_key=...`.",
            "Run `nexus-fs auth test s3` to verify the stored credential shape.",
        ),
    },
    "gcs": {
        "recommended": "native",
        "native_steps": (
            "Run `gcloud auth application-default login` or set `GOOGLE_APPLICATION_CREDENTIALS`.",
            "Use `nexus-fs auth connect gcs native` to tell Nexus to prefer ADC/native credentials.",
            "Run `nexus-fs auth test gcs` to verify the provider chain resolves.",
        ),
        "secret_steps": (
            "Prepare a service-account JSON file or access token.",
            "Run `nexus-fs auth connect gcs secret` and provide `credentials_path` or `access_token`.",
            "Run `nexus-fs auth test gcs` to verify the stored credential shape.",
        ),
    },
    "gws": {
        "recommended": "oauth",
        "oauth_steps": (
            "Set `NEXUS_OAUTH_GOOGLE_CLIENT_ID` and `NEXUS_OAUTH_GOOGLE_CLIENT_SECRET`.",
            "Run `nexus-fs auth connect gws oauth --user-email you@example.com`.",
            "Follow the browser/code flow, then run `nexus-fs auth test gws --user-email you@example.com`.",
        ),
    },
    "google-drive": {
        "recommended": "oauth",
        "oauth_steps": (
            "Set `NEXUS_OAUTH_GOOGLE_CLIENT_ID` and `NEXUS_OAUTH_GOOGLE_CLIENT_SECRET`.",
            "Run `nexus-fs auth connect google-drive oauth --user-email you@example.com`.",
            "Follow the browser/code flow, then run `nexus-fs auth test google-drive --user-email you@example.com`.",
        ),
    },
    "gmail": {
        "recommended": "oauth",
        "oauth_steps": (
            "Set `NEXUS_OAUTH_GOOGLE_CLIENT_ID` and `NEXUS_OAUTH_GOOGLE_CLIENT_SECRET`.",
            "Run `nexus-fs auth connect gmail oauth --user-email you@example.com`.",
            "Follow the browser/code flow, then run `nexus-fs auth test gmail --user-email you@example.com`.",
        ),
    },
    "google-calendar": {
        "recommended": "oauth",
        "oauth_steps": (
            "Set `NEXUS_OAUTH_GOOGLE_CLIENT_ID` and `NEXUS_OAUTH_GOOGLE_CLIENT_SECRET`.",
            "Run `nexus-fs auth connect google-calendar oauth --user-email you@example.com`.",
            "Follow the browser/code flow, then run `nexus-fs auth test google-calendar --user-email you@example.com`.",
        ),
    },
    "x": {
        "recommended": "oauth",
        "oauth_steps": (
            "Set `NEXUS_OAUTH_X_CLIENT_ID` and optionally `NEXUS_OAUTH_X_CLIENT_SECRET`.",
            "Run `nexus-fs auth connect x oauth --user-email you@example.com`.",
            "Follow the browser/code flow, then run `nexus-fs auth test x --user-email you@example.com`.",
        ),
    },
}


def _build_auth_service() -> UnifiedAuthService:
    from nexus.bricks.auth.oauth.credential_service import OAuthCredentialService

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


def _resolve_user_email(user_email: str | None) -> str:
    return user_email or str(click.prompt("user_email"))


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
    console.print(f"[green]ok[/green] {service_name}: {source} {kind.value} auth is configured")
    console.print(f"[dim]Secret store: {store_path}[/dim]")
    if fields:
        console.print(f"[dim]Fields: {', '.join(fields)}[/dim]")
    console.print(f"[dim]Next: nexus-fs auth test {service_name}[/dim]")


@click.group(name="auth")
def auth() -> None:
    """Unified auth commands for nexus-fs."""


@auth.command("list")
def list_auth() -> None:
    """List configured auth across services."""
    service = _build_auth_service()
    summaries = asyncio.run(service.list_summaries())
    table = Table(title="Unified Auth", show_header=True, header_style="bold cyan")
    table.add_column("Service", style="green")
    table.add_column("Kind", style="cyan")
    table.add_column("Status", style="yellow")
    table.add_column("Source", style="blue")
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
    console.print(f"[dim]Secret store: {service.secret_store_path}[/dim]")


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
def test_auth(service_name: str, user_email: str | None, target: str | None) -> None:
    """Validate auth for a service."""
    service = _build_auth_service()
    try:
        result = asyncio.run(
            service.test_service(service_name, user_email=user_email, target=target)
        )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc

    checks = result.get("checks")
    if isinstance(checks, list) and checks:
        table = Table(
            title=f"{service_name} target readiness", show_header=True, header_style="bold cyan"
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

    if result.get("success"):
        console.print(f"[green]ok[/green] {service_name}: {result.get('message')}")
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
        user_email = _resolve_user_email(user_email)
        if service_name in {"gws", "google-drive", "gmail", "google-calendar"}:
            run_google_oauth_setup(user_email=user_email)
            return
        if service_name == "x":
            run_x_oauth_setup(user_email=user_email)
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
    console.print(f"[green]ok[/green] Removed stored auth for {service_name}")


@auth.command("doctor")
def auth_doctor() -> None:
    """Show only auth-related doctor results."""
    service = _build_auth_service()
    summaries = asyncio.run(service.list_summaries())
    failures = [s for s in summaries if s.status not in {AuthStatus.AUTHED, AuthStatus.UNKNOWN}]
    for summary in summaries:
        style = "green" if summary.status == AuthStatus.AUTHED else "yellow"
        console.print(
            f"[{style}]{summary.service}[/{style}] {summary.status.value}: {summary.message}"
        )
    if failures:
        raise click.ClickException("One or more services need auth setup.")
