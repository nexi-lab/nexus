"""Standalone OAuth helpers for the nexus-fs CLI surface."""

from __future__ import annotations

import asyncio
import importlib as _il
import os
import sys
from typing import TYPE_CHECKING

import click

from nexus.cli.utils import console
from nexus.contracts.constants import ROOT_ZONE_ID

if TYPE_CHECKING:
    from nexus.bricks.auth.oauth.providers.x import XOAuthProvider
    from nexus.bricks.auth.oauth.token_manager import TokenManager
else:
    XOAuthProvider = _il.import_module("nexus.bricks.auth.oauth.providers.x").XOAuthProvider
    TokenManager = _il.import_module("nexus.bricks.auth.oauth.token_manager").TokenManager


def get_token_manager(db_path: str | None = None) -> TokenManager:
    """Create the OAuth token manager for nexus-fs."""
    from nexus.lib.env import get_database_url

    db_url = get_database_url()
    if db_url:
        return TokenManager(db_url=db_url)
    if db_path is None:
        home = os.path.expanduser("~")
        db_path = os.path.join(home, ".nexus", "nexus.db")
    parent_dir = os.path.dirname(db_path)
    if parent_dir:
        os.makedirs(parent_dir, exist_ok=True)
    return TokenManager(db_path=db_path)


def run_google_oauth_setup(
    *,
    user_email: str,
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
        scopes=[
            "https://www.googleapis.com/auth/drive",
            "https://www.googleapis.com/auth/drive.file",
        ],
        provider_name="google-drive",
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
