"""OAuth CLI commands for token management.

This module provides CLI commands for managing OAuth credentials:
- nexus oauth list: List all stored OAuth credentials
- nexus oauth revoke: Revoke a credential
- nexus oauth test: Test a credential's validity
- nexus oauth refresh: Manually refresh a token
- nexus oauth init: Initialize OAuth flow for a provider

Examples:
    # List all credentials
    nexus oauth list

    # Revoke a credential
    nexus oauth revoke google alice@example.com

    # Test a credential
    nexus oauth test google alice@example.com

    # Initialize OAuth flow for Google Drive
    nexus oauth init google --client-id "..." --client-secret "..." --scopes "https://www.googleapis.com/auth/drive"
"""

from __future__ import annotations

import asyncio
import os
import sys

import click
from rich.console import Console
from rich.table import Table

from nexus.cli.utils import console
from nexus.server.auth import (
    GoogleOAuthProvider,
    MicrosoftOAuthProvider,
    TokenManager,
)
from nexus.server.auth.x_oauth import XOAuthProvider

# Rich console for output
_console = Console()


def get_token_manager(db_path: str | None = None) -> TokenManager:
    """Get TokenManager instance.

    Args:
        db_path: Path to database (defaults to NEXUS_DATABASE_URL or ~/.nexus/nexus.db)

    Returns:
        TokenManager instance
    """
    # Check for database URL in environment (for Postgres/MySQL)
    db_url = os.getenv("NEXUS_DATABASE_URL")

    if db_url:
        # Use database URL (Postgres, MySQL, etc.)
        return TokenManager(db_url=db_url)
    elif db_path is None:
        # Use default SQLite database path
        home = os.path.expanduser("~")
        db_path = os.path.join(home, ".nexus", "nexus.db")
        # Create directory if it doesn't exist
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        return TokenManager(db_path=db_path)
    else:
        # Use provided db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        return TokenManager(db_path=db_path)


@click.group()
def oauth() -> None:
    """OAuth credential management commands.

    Manage OAuth credentials for backend integrations (Google Drive, Microsoft Graph, etc.).

    \b
    Examples:
        # List all credentials
        nexus oauth list

        # Revoke a credential
        nexus oauth revoke google alice@example.com

        # Test a credential
        nexus oauth test google alice@example.com
    """
    pass


@oauth.command("list")
@click.option(
    "--db-path",
    type=str,
    default=None,
    help="Path to database (default: ~/.nexus/nexus.db)",
)
@click.option(
    "--tenant-id",
    type=str,
    default=None,
    help="Filter by tenant ID",
)
def list_credentials(db_path: str | None, tenant_id: str | None) -> None:
    """List all stored OAuth credentials.

    Shows metadata about each credential (provider, user, expiry status).
    Does NOT show the actual tokens (security).

    \b
    Examples:
        nexus oauth list
        nexus oauth list --tenant-id org_acme
    """
    manager = get_token_manager(db_path)

    async def _list() -> None:
        credentials = await manager.list_credentials(tenant_id=tenant_id)

        if not credentials:
            console.print("[yellow]No OAuth credentials found[/yellow]")
            return

        # Create table
        table = Table(title="OAuth Credentials", show_header=True, header_style="bold magenta")
        table.add_column("Provider", style="cyan")
        table.add_column("User Email", style="green")
        table.add_column("Tenant ID", style="blue")
        table.add_column("Status", style="yellow")
        table.add_column("Expires At", style="white")
        table.add_column("Last Used", style="white")

        for cred in credentials:
            status = "🔴 Expired" if cred["is_expired"] else "🟢 Valid"
            expires_at = cred["expires_at"] or "N/A"
            last_used = cred["last_used_at"] or "Never"

            table.add_row(
                cred["provider"],
                cred["user_email"],
                cred["tenant_id"] or "N/A",
                status,
                expires_at,
                last_used,
            )

        console.print(table)
        console.print(f"\n[bold]Total:[/bold] {len(credentials)} credential(s)")

    asyncio.run(_list())
    manager.close()


@oauth.command("revoke")
@click.argument("provider", type=str)
@click.argument("user_email", type=str)
@click.option(
    "--db-path",
    type=str,
    default=None,
    help="Path to database (default: ~/.nexus/nexus.db)",
)
@click.option(
    "--tenant-id",
    type=str,
    default=None,
    help="Tenant ID (optional)",
)
def revoke_credential(
    provider: str, user_email: str, db_path: str | None, tenant_id: str | None
) -> None:
    """Revoke an OAuth credential.

    This will:
    1. Revoke the token via the provider's API (if available)
    2. Mark the credential as revoked in the database

    \b
    Examples:
        nexus oauth revoke google alice@example.com
        nexus oauth revoke microsoft bob@company.com --tenant-id org_acme
    """
    manager = get_token_manager(db_path)

    async def _revoke() -> None:
        success = await manager.revoke_credential(provider, user_email, tenant_id or "default")

        if success:
            console.print(f"[green]✓[/green] Revoked credential: {provider}:{user_email}")
        else:
            console.print(f"[red]✗[/red] Credential not found: {provider}:{user_email}")
            sys.exit(1)

    asyncio.run(_revoke())
    manager.close()


@oauth.command("test")
@click.argument("provider", type=str)
@click.argument("user_email", type=str)
@click.option(
    "--db-path",
    type=str,
    default=None,
    help="Path to database (default: ~/.nexus/nexus.db)",
)
@click.option(
    "--tenant-id",
    type=str,
    default=None,
    help="Tenant ID (optional)",
)
def test_credential(
    provider: str, user_email: str, db_path: str | None, tenant_id: str | None
) -> None:
    """Test an OAuth credential's validity.

    This will:
    1. Retrieve the credential from database
    2. Decrypt and check expiry
    3. Attempt to refresh if expired
    4. Validate via provider's API

    \b
    Examples:
        nexus oauth test google alice@example.com
        nexus oauth test microsoft bob@company.com
    """
    manager = get_token_manager(db_path)

    # Register provider (needed for validation)
    # Note: This is a simplified version - in production, you'd load client credentials from config
    console.print(f"[yellow]⚠ Testing credential for {provider}:{user_email}...[/yellow]")

    async def _test() -> None:
        try:
            # Try to get a valid token (will auto-refresh if needed)
            token = await manager.get_valid_token(provider, user_email, tenant_id or "default")

            console.print("[green]✓[/green] Credential is valid")
            console.print(f"[dim]Token length: {len(token)} chars[/dim]")

        except Exception as e:
            console.print(f"[red]✗[/red] Credential test failed: {e}")
            sys.exit(1)

    asyncio.run(_test())
    manager.close()


@oauth.command("setup-gdrive")
@click.option(
    "--client-id",
    type=str,
    default=lambda: os.getenv("NEXUS_OAUTH_GOOGLE_CLIENT_ID"),
    help="Google OAuth client ID (default: $NEXUS_OAUTH_GOOGLE_CLIENT_ID)",
)
@click.option(
    "--client-secret",
    type=str,
    default=lambda: os.getenv("NEXUS_OAUTH_GOOGLE_CLIENT_SECRET"),
    help="Google OAuth client secret (default: $NEXUS_OAUTH_GOOGLE_CLIENT_SECRET)",
)
@click.option(
    "--user-email",
    type=str,
    required=True,
    help="User email address (for storing credentials)",
)
@click.option(
    "--db-path",
    type=str,
    default=None,
    help="Path to database (default: ~/.nexus/nexus.db)",
)
@click.option(
    "--tenant-id",
    type=str,
    default=None,
    help="Tenant ID (optional)",
)
def setup_gdrive(
    client_id: str | None,
    client_secret: str | None,
    user_email: str,
    db_path: str | None,
    tenant_id: str | None,
) -> None:
    """Setup Google Drive OAuth credentials for backend integration.

    This command guides you through the OAuth flow to authorize Nexus
    to access your Google Drive. The credentials are stored encrypted
    in the database and used by the Google Drive connector backend.

    OAuth credentials can be provided via:
    - Command-line options: --client-id and --client-secret
    - Environment variables: NEXUS_OAUTH_GOOGLE_CLIENT_ID and NEXUS_OAUTH_GOOGLE_CLIENT_SECRET
    - Users only need to provide --user-email if credentials are in environment

    \b
    Steps:
    1. Creates OAuth authorization URL
    2. Opens browser for user consent
    3. User grants permission
    4. Exchanges authorization code for tokens
    5. Stores encrypted tokens in database

    \b
    Example (with credentials in command):
        nexus oauth setup-gdrive \\
            --client-id "123.apps.googleusercontent.com" \\
            --client-secret "GOCSPX-..." \\
            --user-email "alice@example.com"

    \b
    Example (with credentials in environment):
        export NEXUS_OAUTH_GOOGLE_CLIENT_ID="123.apps.googleusercontent.com"
        export NEXUS_OAUTH_GOOGLE_CLIENT_SECRET="GOCSPX-..."
        nexus oauth setup-gdrive --user-email "alice@example.com"
    """

    from nexus.server.auth import GoogleOAuthProvider

    # Validate that credentials are provided (either via options or environment)
    if not client_id:
        console.print("[red]Error:[/red] Google OAuth client ID not provided")
        console.print("[yellow]Provide via:[/yellow]")
        console.print("  --client-id option, OR")
        console.print("  NEXUS_OAUTH_GOOGLE_CLIENT_ID environment variable")
        sys.exit(1)

    if not client_secret:
        console.print("[red]Error:[/red] Google OAuth client secret not provided")
        console.print("[yellow]Provide via:[/yellow]")
        console.print("  --client-secret option, OR")
        console.print("  NEXUS_OAUTH_GOOGLE_CLIENT_SECRET environment variable")
        sys.exit(1)

    # Create provider
    provider = GoogleOAuthProvider(
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri="http://localhost",  # Desktop app redirect URI
        scopes=[
            "https://www.googleapis.com/auth/drive",
            "https://www.googleapis.com/auth/drive.file",
        ],
        provider_name="google-drive",
    )

    # Generate authorization URL
    auth_url = provider.get_authorization_url()

    console.print("\n[bold green]Google Drive OAuth Setup[/bold green]")
    console.print(f"\n[bold]User:[/bold] {user_email}")
    console.print(f"[bold]Client ID:[/bold] {client_id}")
    console.print("\n[bold yellow]Step 1:[/bold yellow] Visit this URL to authorize:")
    console.print(f"\n{auth_url}\n")
    console.print(
        "[bold yellow]Step 2:[/bold yellow] After granting permission, the browser will redirect to localhost (which will fail)."
    )
    console.print(
        "[bold yellow]Step 3:[/bold yellow] Copy the 'code' parameter from the failed URL:"
    )
    console.print("[dim]Example: http://localhost/?code=4/0AdLI...[/dim]")
    console.print("[dim]Copy everything after 'code=' (the authorization code)[/dim]")

    # Get authorization code from user
    auth_code = click.prompt("\nEnter authorization code")

    # Exchange code for tokens
    console.print("\n[dim]Exchanging code for tokens...[/dim]")

    async def _exchange_and_store() -> None:
        # Exchange code
        credential = await provider.exchange_code(auth_code)

        # Store in database
        manager = get_token_manager(db_path)
        cred_id = await manager.store_credential(
            provider="google",
            user_email=user_email,
            credential=credential,
            tenant_id=tenant_id or "default",
            created_by=user_email,
        )
        manager.close()

        console.print(f"[green]✓[/green] Stored credential with ID: {cred_id}")

    import asyncio

    try:
        cred_id = asyncio.run(_exchange_and_store())
        console.print(f"\n[green]✓[/green] Successfully stored credentials for {user_email}")
        console.print(f"[dim]Credential ID: {cred_id}[/dim]")
        console.print("\n[bold]Next steps:[/bold]")
        console.print("1. Configure Nexus to use Google Drive backend")
        console.print("2. Use [cyan]nexus oauth test google {user_email}[/cyan] to verify")
    except Exception as e:
        console.print(f"\n[red]✗[/red] Failed to setup Google Drive: {e}")
        sys.exit(1)


@oauth.command("setup-x")
@click.option(
    "--client-id",
    type=str,
    default=lambda: os.getenv("NEXUS_OAUTH_X_CLIENT_ID"),
    help="X (Twitter) OAuth client ID (default: $NEXUS_OAUTH_X_CLIENT_ID)",
)
@click.option(
    "--client-secret",
    type=str,
    default=lambda: os.getenv("NEXUS_OAUTH_X_CLIENT_SECRET"),
    help="X OAuth client secret (optional for PKCE, default: $NEXUS_OAUTH_X_CLIENT_SECRET)",
)
@click.option(
    "--user-email",
    type=str,
    required=True,
    help="User email address (for storing credentials)",
)
@click.option(
    "--db-path",
    type=str,
    default=None,
    help="Path to database (default: ~/.nexus/nexus.db)",
)
@click.option(
    "--tenant-id",
    type=str,
    default=None,
    help="Tenant ID (optional)",
)
def setup_x(
    client_id: str | None,
    client_secret: str | None,
    user_email: str,
    db_path: str | None,
    tenant_id: str | None,
) -> None:
    """Setup X (Twitter) OAuth credentials for backend integration.

    This command guides you through the OAuth 2.0 PKCE flow to authorize
    Nexus to access your X (Twitter) account. The credentials are stored
    encrypted in the database and used by the X connector backend.

    OAuth credentials can be provided via:
    - Command-line options: --client-id and --client-secret (optional)
    - Environment variables: NEXUS_OAUTH_X_CLIENT_ID and NEXUS_OAUTH_X_CLIENT_SECRET
    - Note: client_secret is optional for PKCE (public clients)

    \b
    Steps:
    1. Creates OAuth authorization URL with PKCE challenge
    2. Opens browser for user consent
    3. User grants permission
    4. Exchanges authorization code + PKCE verifier for tokens
    5. Stores encrypted tokens in database

    \b
    Example (with client ID in command):
        nexus oauth setup-x \\
            --client-id "your-client-id" \\
            --user-email "you@example.com"

    \b
    Example (with credentials in environment):
        export NEXUS_OAUTH_X_CLIENT_ID="your-client-id"
        nexus oauth setup-x --user-email "you@example.com"

    \b
    Get X API credentials:
    1. Visit https://developer.twitter.com/
    2. Create a new app (or use existing)
    3. Setup OAuth 2.0 with redirect URI: http://localhost
    4. Copy Client ID (and optionally Client Secret)
    """

    # Validate that client_id is provided
    if not client_id:
        console.print("[red]Error:[/red] X OAuth client ID not provided")
        console.print("[yellow]Provide via:[/yellow]")
        console.print("  --client-id option, OR")
        console.print("  NEXUS_OAUTH_X_CLIENT_ID environment variable")
        console.print("\n[bold]Get credentials at:[/bold] https://developer.twitter.com/")
        sys.exit(1)

    # Create provider with PKCE
    provider = XOAuthProvider(
        client_id=client_id,
        redirect_uri="http://localhost",  # Desktop app redirect URI
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
        client_secret=client_secret,  # Optional for PKCE
    )

    # Generate authorization URL with PKCE
    auth_url, pkce_data = provider.get_authorization_url_with_pkce()
    code_verifier = pkce_data["code_verifier"]

    console.print("\n[bold green]X (Twitter) OAuth Setup[/bold green]")
    console.print(f"\n[bold]User:[/bold] {user_email}")
    console.print(f"[bold]Client ID:[/bold] {client_id}")
    console.print("[bold]Using PKCE:[/bold] Yes (enhanced security)")
    console.print("\n[bold yellow]Step 1:[/bold yellow] Visit this URL to authorize:")
    console.print(f"\n{auth_url}\n")
    console.print(
        "[bold yellow]Step 2:[/bold yellow] After granting permission, the browser will redirect to localhost (which will fail)."
    )
    console.print(
        "[bold yellow]Step 3:[/bold yellow] Copy the 'code' parameter from the failed URL:"
    )
    console.print("[dim]Example: http://localhost/?code=ABCD...[/dim]")
    console.print("[dim]Copy everything after 'code=' (the authorization code)[/dim]")

    # Get authorization code from user
    auth_code = click.prompt("\nEnter authorization code")

    # Exchange code for tokens using PKCE
    console.print("\n[dim]Exchanging code for tokens (using PKCE verifier)...[/dim]")

    async def _exchange_and_store() -> str:
        # Exchange code with PKCE verifier
        credential = await provider.exchange_code_pkce(auth_code, code_verifier)

        # Store in database
        manager = get_token_manager(db_path)
        cred_id = await manager.store_credential(
            provider="twitter",
            user_email=user_email,
            credential=credential,
            tenant_id=tenant_id or "default",
            created_by=user_email,
        )
        manager.close()

        console.print(f"[green]✓[/green] Stored credential with ID: {cred_id}")
        return cred_id

    try:
        cred_id = asyncio.run(_exchange_and_store())
        console.print(f"\n[green]✓[/green] Successfully stored credentials for {user_email}")
        console.print(f"[dim]Credential ID: {cred_id}[/dim]")
        console.print("\n[bold]Next steps:[/bold]")
        console.print("1. Configure Nexus to use X connector backend")
        console.print(f"2. Use [cyan]nexus oauth test twitter {user_email}[/cyan] to verify")
        console.print("3. Try the example: [cyan]python examples/x_connector_example.py[/cyan]")
    except Exception as e:
        console.print(f"\n[red]✗[/red] Failed to setup X OAuth: {e}")
        import traceback

        console.print(f"[dim]{traceback.format_exc()}[/dim]")
        sys.exit(1)


@oauth.command("init")
@click.argument("provider", type=click.Choice(["google", "microsoft", "microsoft-onedrive"]))
@click.option("--client-id", type=str, required=True, help="OAuth client ID")
@click.option("--client-secret", type=str, required=True, help="OAuth client secret")
@click.option(
    "--redirect-uri",
    type=str,
    default="http://localhost:8080/oauth/callback",
    help="OAuth redirect URI",
)
@click.option(
    "--scopes",
    type=str,
    multiple=True,
    help="OAuth scopes (can be specified multiple times)",
)
def init_oauth_flow(
    provider: str,
    client_id: str,
    client_secret: str,
    redirect_uri: str,
    scopes: tuple[str, ...],
) -> None:
    """Initialize OAuth flow and get authorization URL.

    This generates an authorization URL for the user to visit and grant permissions.
    After granting permissions, the user will be redirected with an authorization code.

    \b
    Examples:
        # Google Drive
        nexus oauth init google \\
            --client-id "123.apps.googleusercontent.com" \\
            --client-secret "secret" \\
            --scopes "https://www.googleapis.com/auth/drive"

        # Microsoft OneDrive
        nexus oauth init microsoft-onedrive \\
            --client-id "12345678-1234-1234-1234-123456789012" \\
            --client-secret "secret~..." \\
            --scopes "Files.ReadWrite.All" \\
            --scopes "offline_access"

        # Note: 'microsoft' is also accepted as an alias for 'microsoft-onedrive'
    """
    scopes_list = list(scopes)

    oauth_provider: GoogleOAuthProvider | MicrosoftOAuthProvider
    if provider == "google":
        oauth_provider = GoogleOAuthProvider(
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri=redirect_uri,
            scopes=scopes_list,
            provider_name="google-drive",
        )
    elif provider in ("microsoft", "microsoft-onedrive"):
        # Note: tenant_id is hardcoded to "common" in MicrosoftOAuthProvider
        oauth_provider = MicrosoftOAuthProvider(
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri=redirect_uri,
            scopes=scopes_list,
            provider_name="microsoft-onedrive",
        )
    else:
        console.print(f"[red]Unsupported provider: {provider}[/red]")
        sys.exit(1)

    # Generate authorization URL
    auth_url = oauth_provider.get_authorization_url(state="nexus_cli")

    console.print("\n[bold green]OAuth Authorization Flow[/bold green]")
    console.print(f"\n[bold]Provider:[/bold] {provider}")
    console.print(f"[bold]Client ID:[/bold] {client_id}")
    console.print(f"[bold]Scopes:[/bold] {', '.join(scopes_list)}")
    console.print("\n[bold yellow]Step 1:[/bold yellow] Visit this URL to authorize:")
    console.print(f"\n{auth_url}\n")
    console.print(
        "[bold yellow]Step 2:[/bold yellow] After authorization, you'll be redirected with a code."
    )
    console.print(
        "[bold yellow]Step 3:[/bold yellow] Use that code with the Nexus API or server to complete the flow."
    )
    console.print(
        "\n[dim]Note: This CLI command only generates the URL. To complete the flow, "
        "use the Nexus server's OAuth endpoints.[/dim]"
    )
