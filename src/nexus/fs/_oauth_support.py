"""Standalone OAuth helpers for the nexus-fs CLI surface."""

from __future__ import annotations

import asyncio
import importlib as _il
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import click
from cryptography.fernet import Fernet

from nexus.cli.utils import console
from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.security.secret_file import write_secret_file

if TYPE_CHECKING:
    from nexus.bricks.auth.oauth.providers.x import XOAuthProvider
    from nexus.bricks.auth.oauth.token_manager import TokenManager
else:
    XOAuthProvider = _il.import_module("nexus.bricks.auth.oauth.providers.x").XOAuthProvider
    TokenManager = _il.import_module("nexus.bricks.auth.oauth.token_manager").TokenManager

_DEFAULT_DB_PATH = Path("~/.nexus/nexus.db").expanduser()
_DEFAULT_OAUTH_KEY_PATH = Path("~/.nexus/auth/oauth.key").expanduser()

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
    write_secret_file(_DEFAULT_OAUTH_KEY_PATH, key + "\n")
    console.print(f"[dim]Created local OAuth encryption key: {_DEFAULT_OAUTH_KEY_PATH}[/dim]")
    return key


def get_token_manager(db_path: str | None = None) -> TokenManager:
    """Create the OAuth token manager for nexus-fs."""
    db_url = get_fs_database_url()
    encryption_key = get_oauth_encryption_key()
    if db_url:
        return TokenManager(db_url=db_url, encryption_key=encryption_key)
    if db_path is None:
        db_path = str(_DEFAULT_DB_PATH)
    parent_dir = os.path.dirname(db_path)
    if parent_dir:
        os.makedirs(parent_dir, exist_ok=True)
    return TokenManager(db_path=db_path, encryption_key=encryption_key)


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
    GoogleOAuthProvider = _il.import_module(
        "nexus.bricks.auth.oauth.providers.google"
    ).GoogleOAuthProvider

    client_id = client_id or os.getenv("NEXUS_OAUTH_GOOGLE_CLIENT_ID")
    client_secret = client_secret or os.getenv("NEXUS_OAUTH_GOOGLE_CLIENT_SECRET")
    if not client_id:
        raise click.ClickException(
            "Google OAuth client ID not provided. Set NEXUS_OAUTH_GOOGLE_CLIENT_ID first."
        )
    if not client_secret:
        raise click.ClickException(
            "Google OAuth client secret not provided. Set NEXUS_OAUTH_GOOGLE_CLIENT_SECRET first."
        )

    provider = GoogleOAuthProvider(
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri="http://localhost",
        scopes=_GOOGLE_SERVICE_SCOPES.get(service_name, _GOOGLE_SERVICE_SCOPES["gws"]),
        provider_name=service_name,
    )
    auth_url = provider.get_authorization_url()

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
        credential = await provider.exchange_code(auth_code)
        manager = get_token_manager(db_path)
        cred_id = await manager.store_credential(
            provider="google",
            user_email=user_email,
            credential=credential,
            zone_id=zone_id or ROOT_ZONE_ID,
            created_by=user_email,
        )
        manager.close()
        return cred_id

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
    client_id = client_id or os.getenv("NEXUS_OAUTH_X_CLIENT_ID")
    client_secret = client_secret or os.getenv("NEXUS_OAUTH_X_CLIENT_SECRET")
    if not client_id:
        raise click.ClickException("X OAuth client ID not provided. Set NEXUS_OAUTH_X_CLIENT_ID.")

    provider = XOAuthProvider(
        client_id=client_id,
        redirect_uri="http://localhost",
        scopes=[
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
        ],
        provider_name="x",
        client_secret=client_secret,
    )
    auth_url, pkce_data = provider.get_authorization_url_with_pkce()
    code_verifier = pkce_data["code_verifier"]

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
        credential = await provider.exchange_code_pkce(auth_code, code_verifier)
        manager = get_token_manager(db_path)
        cred_id = await manager.store_credential(
            provider="twitter",
            user_email=user_email,
            credential=credential,
            zone_id=zone_id or ROOT_ZONE_ID,
            created_by=user_email,
        )
        manager.close()
        return cred_id

    try:
        cred_id = asyncio.run(_exchange_and_store())
        console.print(f"\n[green]ok[/green] stored X OAuth credentials for {user_email}")
        console.print(f"[dim]Credential ID: {cred_id}[/dim]")
    except Exception as exc:
        console.print(f"\n[red]OAuth setup failed:[/red] {exc}")
        sys.exit(1)
