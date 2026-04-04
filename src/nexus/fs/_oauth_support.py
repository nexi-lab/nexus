"""Standalone OAuth helpers for the nexus-fs CLI surface."""

from __future__ import annotations

import asyncio
import importlib as _il
import os
import sys
from pathlib import Path
from typing import Any

import click
from cryptography.fernet import Fernet
from rich.console import Console

from nexus.fs._paths import oauth_key_path as _oauth_key_path_fn
from nexus.fs._paths import token_manager_db as _token_manager_db_fn

# Resolve once at import time for backwards compatibility
_DEFAULT_DB_PATH = _token_manager_db_fn()
_DEFAULT_OAUTH_KEY_PATH = _oauth_key_path_fn()
console = Console()

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
    "gmail": [
        "https://www.googleapis.com/auth/gmail.modify",
    ],
    "google-calendar": [
        "https://www.googleapis.com/auth/calendar",
    ],
}

_GOOGLE_SERVICE_PROVIDER_NAMES: dict[str, str] = {
    "gws": "google",
    "google-drive": "google-drive",
    "gmail": "gmail",
    "google-calendar": "google-calendar",
}

_X_SCOPES: list[str] = [
    "tweet.read",
    "tweet.write",
    "tweet.moderate.write",
    "users.read",
    "follows.read",
    "offline.access",
    "bookmark.read",
    "bookmark.write",
    "list.read",
    "like.read",
    "like.write",
]


def _root_zone_id() -> str:
    try:
        constants = _il.import_module("nexus.contracts.constants")
        value = getattr(constants, "ROOT_ZONE_ID", None)
        if value:
            return str(value)
    except Exception:
        pass
    return "root"


def _write_secret_file(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    os.chmod(path, 0o600)


def _get_token_manager_cls() -> Any:
    return _il.import_module("nexus.bricks.auth.oauth.token_manager").TokenManager


def _get_x_oauth_provider_cls() -> Any:
    return _il.import_module("nexus.bricks.auth.oauth.providers.x").XOAuthProvider


def get_fs_database_url() -> str | None:
    """Resolve the standalone nexus-fs database URL.

    Unlike the full Nexus CLI, nexus-fs defaults to local SQLite. It only
    uses a network/shared database when explicitly pointed there.
    """
    return os.getenv("NEXUS_FS_DATABASE_URL")


def get_oauth_encryption_key() -> str:
    """Load or create the local persisted OAuth encryption key for nexus-fs."""
    env_key = os.getenv("NEXUS_OAUTH_ENCRYPTION_KEY", "").strip()
    if env_key:
        return env_key

    if _DEFAULT_OAUTH_KEY_PATH.exists():
        return _DEFAULT_OAUTH_KEY_PATH.read_text().strip()

    key = Fernet.generate_key().decode("utf-8")
    _write_secret_file(_DEFAULT_OAUTH_KEY_PATH, key + "\n")
    console.print(f"[dim]Created local OAuth encryption key: {_DEFAULT_OAUTH_KEY_PATH}[/dim]")
    return key


def get_token_manager(db_path: str | None = None) -> Any:
    """Create the OAuth token manager for nexus-fs."""
    db_url = get_fs_database_url()
    encryption_key = get_oauth_encryption_key()
    if db_url:
        return _get_token_manager_cls()(db_url=db_url, encryption_key=encryption_key)
    if db_path is None:
        db_path = str(_DEFAULT_DB_PATH)
    parent_dir = os.path.dirname(db_path)
    if parent_dir:
        os.makedirs(parent_dir, exist_ok=True)
    return _get_token_manager_cls()(db_path=db_path, encryption_key=encryption_key)


def get_google_auth_url(
    *,
    service_name: str = "gws",
    client_id: str | None = None,
    client_secret: str | None = None,
    redirect_uri: str = "http://localhost",
) -> str:
    """Generate a Google OAuth authorization URL programmatically.

    Returns the URL that the user (or agent) should visit to authorize.
    No CLI interaction — suitable for embedding in web flows, agent
    orchestration, or headless automation.

    Args:
        service_name: Google service scope set (gws, google-drive, gmail, google-calendar).
        client_id: OAuth client ID. Falls back to NEXUS_OAUTH_GOOGLE_CLIENT_ID env var.
        client_secret: OAuth client secret. Falls back to NEXUS_OAUTH_GOOGLE_CLIENT_SECRET env var.
        redirect_uri: OAuth redirect URI. Defaults to http://localhost.

    Returns:
        The authorization URL string.

    Raises:
        ValueError: If client_id or client_secret is missing.
    """
    GoogleOAuthProvider = _il.import_module(
        "nexus.bricks.auth.oauth.providers.google"
    ).GoogleOAuthProvider

    client_id = client_id or os.getenv("NEXUS_OAUTH_GOOGLE_CLIENT_ID")
    client_secret = client_secret or os.getenv("NEXUS_OAUTH_GOOGLE_CLIENT_SECRET")
    if not client_id:
        raise ValueError("Google OAuth client ID not provided. Set NEXUS_OAUTH_GOOGLE_CLIENT_ID.")
    if not client_secret:
        raise ValueError(
            "Google OAuth client secret not provided. Set NEXUS_OAUTH_GOOGLE_CLIENT_SECRET."
        )

    provider = GoogleOAuthProvider(
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=redirect_uri,
        scopes=_GOOGLE_SERVICE_SCOPES.get(service_name, _GOOGLE_SERVICE_SCOPES["gws"]),
        provider_name=_GOOGLE_SERVICE_PROVIDER_NAMES.get(service_name, "google"),
    )
    result: str = provider.get_authorization_url()
    return result


def get_x_auth_url(
    *,
    client_id: str | None = None,
    client_secret: str | None = None,
    redirect_uri: str = "http://localhost",
) -> tuple[str, dict[str, str]]:
    """Generate an X (Twitter) OAuth authorization URL programmatically.

    Returns the URL and PKCE data needed for the code exchange step.
    No CLI interaction — suitable for embedding in web flows, agent
    orchestration, or headless automation.

    Args:
        client_id: OAuth client ID. Falls back to NEXUS_OAUTH_X_CLIENT_ID env var.
        client_secret: Optional OAuth client secret. Falls back to NEXUS_OAUTH_X_CLIENT_SECRET env var.
        redirect_uri: OAuth redirect URI. Defaults to http://localhost.

    Returns:
        Tuple of (auth_url, pkce_data) where pkce_data contains 'code_verifier'.

    Raises:
        ValueError: If client_id is missing.
    """
    client_id = client_id or os.getenv("NEXUS_OAUTH_X_CLIENT_ID")
    client_secret = client_secret or os.getenv("NEXUS_OAUTH_X_CLIENT_SECRET")
    if not client_id:
        raise ValueError("X OAuth client ID not provided. Set NEXUS_OAUTH_X_CLIENT_ID.")

    provider = _get_x_oauth_provider_cls()(
        client_id=client_id,
        redirect_uri=redirect_uri,
        scopes=_X_SCOPES,
        provider_name="x",
        client_secret=client_secret,
    )
    result: tuple[str, dict[str, str]] = provider.get_authorization_url_with_pkce()
    return result


def run_google_oauth_setup(
    *,
    user_email: str,
    service_name: str = "gws",
    client_id: str | None = None,
    client_secret: str | None = None,
    db_path: str | None = None,
    zone_id: str | None = None,
) -> None:
    """Run the Google OAuth browser/code flow for nexus-fs."""
    try:
        auth_url = get_google_auth_url(
            service_name=service_name,
            client_id=client_id,
            client_secret=client_secret,
        )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc

    # Resolve actual client_id for display (may have come from env var)
    client_id = client_id or os.getenv("NEXUS_OAUTH_GOOGLE_CLIENT_ID", "")

    console.print("\n[bold green]Google OAuth Setup[/bold green]")
    console.print(f"\n[bold]User:[/bold] {user_email}")
    console.print(f"[bold]Client ID:[/bold] {client_id}")
    console.print("\n[bold yellow]Step 1:[/bold yellow] Visit this URL to authorize:")
    console.print(f"\n{auth_url}\n")
    console.print(
        "[bold yellow]Step 2:[/bold yellow] After granting permission, the browser will redirect to localhost."
    )
    console.print("[bold yellow]Step 3:[/bold yellow] Copy the `code` parameter from that URL.")
    auth_code = click.prompt("\nEnter authorization code")

    async def _exchange_and_store() -> str:
        GoogleOAuthProvider = _il.import_module(
            "nexus.bricks.auth.oauth.providers.google"
        ).GoogleOAuthProvider
        client_secret_resolved = client_secret or os.getenv("NEXUS_OAUTH_GOOGLE_CLIENT_SECRET", "")
        provider = GoogleOAuthProvider(
            client_id=client_id or "",
            client_secret=client_secret_resolved,
            redirect_uri="http://localhost",
            scopes=_GOOGLE_SERVICE_SCOPES.get(service_name, _GOOGLE_SERVICE_SCOPES["gws"]),
            provider_name=_GOOGLE_SERVICE_PROVIDER_NAMES.get(service_name, "google"),
        )
        credential = await provider.exchange_code(auth_code)
        manager = get_token_manager(db_path)
        cred_id = await manager.store_credential(
            provider=_GOOGLE_SERVICE_PROVIDER_NAMES.get(service_name, "google"),
            user_email=user_email,
            credential=credential,
            zone_id=zone_id or _root_zone_id(),
            created_by=user_email,
        )
        manager.close()
        return str(cred_id)

    try:
        cred_id = asyncio.run(_exchange_and_store())
        console.print(f"\n[green]ok[/green] stored Google OAuth credentials for {user_email}")
        console.print(f"[dim]Credential ID: {cred_id}[/dim]")
    except Exception as exc:
        console.print(f"\n[red]OAuth setup failed:[/red] {exc}")
        sys.exit(1)


def run_x_oauth_setup(
    *,
    user_email: str,
    client_id: str | None = None,
    client_secret: str | None = None,
    db_path: str | None = None,
    zone_id: str | None = None,
) -> None:
    """Run the X OAuth browser/code flow for nexus-fs."""
    try:
        auth_url, pkce_data = get_x_auth_url(
            client_id=client_id,
            client_secret=client_secret,
        )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc

    code_verifier = pkce_data["code_verifier"]
    client_id = client_id or os.getenv("NEXUS_OAUTH_X_CLIENT_ID", "")

    console.print("\n[bold green]X OAuth Setup[/bold green]")
    console.print(f"\n[bold]User:[/bold] {user_email}")
    console.print(f"[bold]Client ID:[/bold] {client_id}")
    console.print("\n[bold yellow]Step 1:[/bold yellow] Visit this URL to authorize:")
    console.print(f"\n{auth_url}\n")
    console.print(
        "[bold yellow]Step 2:[/bold yellow] Copy the `code` parameter from the redirect URL."
    )
    auth_code = click.prompt("\nEnter authorization code")

    async def _exchange_and_store() -> str:
        client_secret_resolved = client_secret or os.getenv("NEXUS_OAUTH_X_CLIENT_SECRET")
        provider = _get_x_oauth_provider_cls()(
            client_id=client_id or "",
            redirect_uri="http://localhost",
            scopes=_X_SCOPES,
            provider_name="x",
            client_secret=client_secret_resolved,
        )
        credential = await provider.exchange_code_pkce(auth_code, code_verifier)
        manager = get_token_manager(db_path)
        cred_id = await manager.store_credential(
            provider="twitter",
            user_email=user_email,
            credential=credential,
            zone_id=zone_id or _root_zone_id(),
            created_by=user_email,
        )
        manager.close()
        return str(cred_id)

    try:
        cred_id = asyncio.run(_exchange_and_store())
        console.print(f"\n[green]ok[/green] stored X OAuth credentials for {user_email}")
        console.print(f"[dim]Credential ID: {cred_id}[/dim]")
    except Exception as exc:
        console.print(f"\n[red]OAuth setup failed:[/red] {exc}")
        sys.exit(1)


# =============================================================================
# Programmatic OAuth API — for bridges, agents, and non-CLI callers
# =============================================================================


def generate_auth_url(
    provider: str,
    redirect_uri: str,
) -> tuple[str, str | None]:
    """Generate an OAuth authorization URL for the given provider.

    No CLI interaction. Suitable for web flows, agent orchestration, or any
    caller that owns the redirect URI (e.g. a localhost callback server).

    Args:
        provider: Provider name. One of: "google-drive", "gws", "gmail",
            "google-calendar", "x".
        redirect_uri: The redirect URI that OAuth will send the ``?code=``
            parameter to after the user authorizes. Must match what you pass
            to :func:`exchange_auth_code`.

    Returns:
        ``(auth_url, code_verifier)`` tuple.
        - ``auth_url``: URL the user should visit to authorize.
        - ``code_verifier``: PKCE verifier string for X OAuth; ``None`` for
          Google providers. Pass this value to :func:`exchange_auth_code`.

    Raises:
        ValueError: If required credentials are missing or ``provider`` is
            not recognized.

    Examples:
        # Google
        url, _ = nexus.fs.generate_auth_url("google-drive", "http://localhost:4567/callback")

        # X (Twitter) — store the verifier; you'll need it in exchange_auth_code
        url, code_verifier = nexus.fs.generate_auth_url("x", "http://localhost:4567/callback")
    """
    if provider in _GOOGLE_SERVICE_SCOPES:
        url = get_google_auth_url(service_name=provider, redirect_uri=redirect_uri)
        return url, None
    if provider == "x":
        url, pkce_data = get_x_auth_url(redirect_uri=redirect_uri)
        return url, pkce_data["code_verifier"]
    supported = list(_GOOGLE_SERVICE_SCOPES) + ["x"]
    raise ValueError(f"Unsupported provider {provider!r}. Supported: {supported}")


async def exchange_auth_code(
    provider: str,
    user_email: str,
    code: str,
    redirect_uri: str,
    code_verifier: str | None = None,
    *,
    db_path: str | None = None,
    zone_id: str | None = None,
) -> None:
    """Exchange an OAuth authorization code for a token and persist it.

    Call this after the user has authorized and your redirect URI received
    ``?code=...``. The credential is stored in the nexus-fs token store and
    will be used automatically by subsequent filesystem operations.

    Args:
        provider: Provider name. One of: "google-drive", "gws", "gmail",
            "google-calendar", "x".
        user_email: Email address to associate with this credential.
        code: The authorization code from the ``?code=`` query parameter.
        redirect_uri: Must be the same URI used in :func:`generate_auth_url`.
        code_verifier: PKCE verifier returned by :func:`generate_auth_url`.
            Required for X OAuth; ignored for Google providers.
        db_path: Override the token database path. Uses the default nexus-fs
            path if not provided.
        zone_id: Zone to associate the credential with. Defaults to root.

    Raises:
        ValueError: If required credentials or arguments are missing.
        OAuthError: If the code exchange with the provider fails.
    """
    if provider in _GOOGLE_SERVICE_SCOPES:
        GoogleOAuthProvider = _il.import_module(
            "nexus.bricks.auth.oauth.providers.google"
        ).GoogleOAuthProvider
        client_id = os.getenv("NEXUS_OAUTH_GOOGLE_CLIENT_ID", "")
        client_secret = os.getenv("NEXUS_OAUTH_GOOGLE_CLIENT_SECRET", "")
        if not client_id:
            raise ValueError(
                "Google OAuth client ID not provided. Set NEXUS_OAUTH_GOOGLE_CLIENT_ID."
            )
        if not client_secret:
            raise ValueError(
                "Google OAuth client secret not provided. Set NEXUS_OAUTH_GOOGLE_CLIENT_SECRET."
            )
        oauth_provider = GoogleOAuthProvider(
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri=redirect_uri,
            scopes=_GOOGLE_SERVICE_SCOPES[provider],
            provider_name=_GOOGLE_SERVICE_PROVIDER_NAMES[provider],
        )
        credential = await oauth_provider.exchange_code(code)
        provider_name = _GOOGLE_SERVICE_PROVIDER_NAMES[provider]
    elif provider == "x":
        if not code_verifier:
            raise ValueError("code_verifier is required for X OAuth (PKCE).")
        client_id = os.getenv("NEXUS_OAUTH_X_CLIENT_ID", "")
        x_client_secret: str | None = os.getenv("NEXUS_OAUTH_X_CLIENT_SECRET")
        if not client_id:
            raise ValueError("X OAuth client ID not provided. Set NEXUS_OAUTH_X_CLIENT_ID.")
        oauth_provider = _get_x_oauth_provider_cls()(
            client_id=client_id,
            redirect_uri=redirect_uri,
            scopes=_X_SCOPES,
            provider_name="x",
            client_secret=x_client_secret,
        )
        credential = await oauth_provider.exchange_code_pkce(code, code_verifier)
        provider_name = "twitter"
    else:
        supported = list(_GOOGLE_SERVICE_SCOPES) + ["x"]
        raise ValueError(f"Unsupported provider {provider!r}. Supported: {supported}")

    manager = get_token_manager(db_path)
    try:
        await manager.store_credential(
            provider=provider_name,
            user_email=user_email,
            credential=credential,
            zone_id=zone_id or _root_zone_id(),
            created_by=user_email,
        )
    finally:
        manager.close()
