"""Admin CLI commands for user and API key management.

This module provides CLI commands for the Admin API (issue #322, #266).
Admin commands allow remote management of users and API keys without SSH access.

All commands require:
1. A running Nexus server with database-backed authentication
2. An admin API key set via NEXUS_API_KEY or --remote-api-key
3. Server URL set via NEXUS_URL or --remote-url
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from typing import Any

import click
from rich.console import Console
from rich.table import Table

from nexus.cli.utils import (
    REMOTE_API_KEY_OPTION,
    REMOTE_URL_OPTION,
    console,
)
from nexus.remote import RemoteNexusFS

# Rich console for output
_console = Console()


def get_remote_client(url: str | None, api_key: str | None) -> RemoteNexusFS:
    """Get remote Nexus client for admin operations.

    Args:
        url: Server URL (from --remote-url or NEXUS_URL)
        api_key: Admin API key (from --remote-api-key or NEXUS_API_KEY)

    Returns:
        RemoteNexusFS instance

    Raises:
        SystemExit: If URL or API key not provided
    """
    if not url:
        console.print("[red]Error:[/red] Server URL required. Set NEXUS_URL or use --remote-url")
        sys.exit(1)

    if not api_key:
        console.print(
            "[red]Error:[/red] Admin API key required. Set NEXUS_API_KEY or use --remote-api-key"
        )
        sys.exit(1)

    return RemoteNexusFS(server_url=url, api_key=api_key)


@click.group()
def admin() -> None:
    """Admin commands for user and API key management.

    Requires admin privileges and remote server access.

    \b
    Prerequisites:
        - Running Nexus server with database authentication
        - Admin API key (set via NEXUS_API_KEY or --remote-api-key)
        - Server URL (set via NEXUS_URL or --remote-url)

    \b
    Examples:
        export NEXUS_URL=http://localhost:8080
        export NEXUS_API_KEY=<admin_api_key>

        nexus admin create-user alice --name "Alice's Laptop"
        nexus admin list-users
        nexus admin revoke-key <key_id>
    """
    pass


@admin.command("create-user")
@click.argument("user_id")
@click.option("--name", required=True, help="Human-readable name for the API key")
@click.option("--email", help="User email (for documentation purposes)")
@click.option("--is-admin", is_flag=True, help="Grant admin privileges")
@click.option("--expires-days", type=int, help="API key expiry in days")
@click.option("--tenant-id", default="default", help="Tenant ID (default: 'default')")
@click.option("--subject-type", default="user", help="Subject type: user or agent")
@click.option("--json-output", is_flag=True, help="Output as JSON")
@REMOTE_API_KEY_OPTION
@REMOTE_URL_OPTION
def create_user(
    user_id: str,
    name: str,
    email: str | None,
    is_admin: bool,
    expires_days: int | None,
    tenant_id: str,
    subject_type: str,
    json_output: bool,
    remote_url: str | None,
    remote_api_key: str | None,
) -> None:
    """Create a new user and generate API key.

    This creates an API key for a user, effectively creating the user account.

    \b
    Examples:
        # Create regular user with 90-day expiry
        nexus admin create-user alice --name "Alice Smith" --expires-days 90

        # Create admin user
        nexus admin create-user admin --name "Admin Key" --is-admin

        # Create agent key
        nexus admin create-user bot1 --name "Bot Agent" --subject-type agent
    """
    try:
        nx = get_remote_client(remote_url, remote_api_key)

        # Build parameters
        params: dict[str, Any] = {
            "user_id": user_id,
            "name": name,
            "is_admin": is_admin,
            "tenant_id": tenant_id,
            "subject_type": subject_type,
        }

        if expires_days is not None:
            params["expires_days"] = expires_days

        # Call admin API
        result = nx._call_rpc("admin_create_key", params)

        if json_output:
            click.echo(json.dumps(result, indent=2))
        else:
            console.print("[green]✓[/green] User created successfully")
            console.print("\n[yellow]⚠ Save this API key - it will only be shown once![/yellow]\n")
            console.print(f"User ID:     {result['user_id']}")
            console.print(f"Key ID:      {result['key_id']}")
            console.print(f"[bold]API Key:[/bold]     {result['api_key']}")
            console.print(f"Tenant:      {result['tenant_id']}")
            console.print(f"Admin:       {result['is_admin']}")
            if result.get("expires_at"):
                console.print(f"Expires:     {result['expires_at']}")

            if email:
                console.print(f"\n[dim]Email: {email}[/dim]")

    except Exception as e:
        console.print(f"[red]Error creating user:[/red] {e}")
        sys.exit(1)


@admin.command("list-users")
@click.option("--user-id", help="Filter by user ID")
@click.option("--tenant-id", help="Filter by tenant ID")
@click.option("--is-admin", is_flag=True, help="Filter for admin keys only")
@click.option("--include-revoked", is_flag=True, help="Include revoked keys")
@click.option("--include-expired", is_flag=True, help="Include expired keys")
@click.option("--limit", type=int, default=100, help="Maximum number of results")
@click.option("--json-output", is_flag=True, help="Output as JSON")
@REMOTE_API_KEY_OPTION
@REMOTE_URL_OPTION
def list_users(
    user_id: str | None,
    tenant_id: str | None,
    is_admin: bool,
    include_revoked: bool,
    include_expired: bool,
    limit: int,
    json_output: bool,
    remote_url: str | None,
    remote_api_key: str | None,
) -> None:
    """List all users and their API keys.

    \b
    Examples:
        # List all active users
        nexus admin list-users

        # List keys for specific user
        nexus admin list-users --user-id alice

        # List admin keys only
        nexus admin list-users --is-admin

        # Include revoked and expired keys
        nexus admin list-users --include-revoked --include-expired
    """
    try:
        nx = get_remote_client(remote_url, remote_api_key)

        # Build parameters
        params: dict[str, Any] = {
            "limit": limit,
            "include_revoked": include_revoked,
            "include_expired": include_expired,
        }

        if user_id:
            params["user_id"] = user_id
        if tenant_id:
            params["tenant_id"] = tenant_id
        if is_admin:
            params["is_admin"] = True

        # Call admin API
        result = nx._call_rpc("admin_list_keys", params)
        keys = result.get("keys", [])

        if json_output:
            click.echo(json.dumps(keys, indent=2))
        else:
            if not keys:
                console.print("[yellow]No users found.[/yellow]")
                return

            # Create table
            table = Table(title=f"API Keys ({len(keys)} total)")
            table.add_column("User ID", style="cyan")
            table.add_column("Name", style="white")
            table.add_column("Key ID", style="dim")
            table.add_column("Admin", style="green")
            table.add_column("Created", style="blue")
            table.add_column("Expires", style="yellow")
            table.add_column("Status", style="white")

            for key in keys:
                # Determine status
                status = "Active"
                status_style = "green"
                if key.get("revoked"):
                    status = "Revoked"
                    status_style = "red"
                elif key.get("expires_at"):
                    try:
                        # Parse ISO format datetime
                        expires = datetime.fromisoformat(key["expires_at"].replace("Z", "+00:00"))
                        if expires < datetime.now(UTC):
                            status = "Expired"
                            status_style = "yellow"
                    except (ValueError, TypeError):
                        pass

                table.add_row(
                    key.get("user_id", ""),
                    key.get("name", ""),
                    key.get("key_id", "")[:16] + "...",
                    "✓" if key.get("is_admin") else "",
                    key.get("created_at", "")[:10] if key.get("created_at") else "",
                    key.get("expires_at", "")[:10] if key.get("expires_at") else "Never",
                    f"[{status_style}]{status}[/{status_style}]",
                )

            _console.print(table)

    except Exception as e:
        console.print(f"[red]Error listing users:[/red] {e}")
        sys.exit(1)


@admin.command("revoke-key")
@click.argument("key_id")
@click.option("--json-output", is_flag=True, help="Output as JSON")
@REMOTE_API_KEY_OPTION
@REMOTE_URL_OPTION
def revoke_key(
    key_id: str,
    json_output: bool,
    remote_url: str | None,
    remote_api_key: str | None,
) -> None:
    """Revoke an API key.

    \b
    Examples:
        nexus admin revoke-key d6f5e137-5fce-4e06-9432-6e30324dfad1
    """
    try:
        nx = get_remote_client(remote_url, remote_api_key)

        # Call admin API
        result = nx._call_rpc("admin_revoke_key", {"key_id": key_id})

        if json_output:
            click.echo(json.dumps(result, indent=2))
        else:
            console.print("[green]✓[/green] API key revoked successfully")
            console.print(f"Key ID: {key_id}")

    except Exception as e:
        console.print(f"[red]Error revoking key:[/red] {e}")
        sys.exit(1)


@admin.command("create-key")
@click.argument("user_id")
@click.option("--name", required=True, help="Human-readable name for the new key")
@click.option("--expires-days", type=int, help="API key expiry in days")
@click.option("--json-output", is_flag=True, help="Output as JSON")
@REMOTE_API_KEY_OPTION
@REMOTE_URL_OPTION
def create_key(
    user_id: str,
    name: str,
    expires_days: int | None,
    json_output: bool,
    remote_url: str | None,
    remote_api_key: str | None,
) -> None:
    """Create additional API key for existing user.

    \b
    Examples:
        nexus admin create-key alice --name "Alice's new laptop" --expires-days 90
    """
    try:
        nx = get_remote_client(remote_url, remote_api_key)

        # Build parameters
        params: dict[str, Any] = {
            "user_id": user_id,
            "name": name,
        }

        if expires_days is not None:
            params["expires_days"] = expires_days

        # Call admin API
        result = nx._call_rpc("admin_create_key", params)

        if json_output:
            click.echo(json.dumps(result, indent=2))
        else:
            console.print("[green]✓[/green] API key created successfully")
            console.print("\n[yellow]⚠ Save this API key - it will only be shown once![/yellow]\n")
            console.print(f"User ID:     {result['user_id']}")
            console.print(f"Key ID:      {result['key_id']}")
            console.print(f"[bold]API Key:[/bold]     {result['api_key']}")
            if result.get("expires_at"):
                console.print(f"Expires:     {result['expires_at']}")

    except Exception as e:
        console.print(f"[red]Error creating key:[/red] {e}")
        sys.exit(1)


@admin.command("get-user")
@click.option("--user-id", help="User ID to look up")
@click.option("--key-id", help="Key ID to look up")
@click.option("--json-output", is_flag=True, help="Output as JSON")
@REMOTE_API_KEY_OPTION
@REMOTE_URL_OPTION
def get_user(
    user_id: str | None,
    key_id: str | None,
    json_output: bool,
    remote_url: str | None,
    remote_api_key: str | None,
) -> None:
    """Get detailed information about a user or API key.

    Must provide either --user-id or --key-id.

    \b
    Examples:
        nexus admin get-user --user-id alice
        nexus admin get-user --key-id d6f5e137-5fce-4e06-9432-6e30324dfad1
    """
    if not user_id and not key_id:
        console.print("[red]Error:[/red] Must provide either --user-id or --key-id")
        sys.exit(1)

    try:
        nx = get_remote_client(remote_url, remote_api_key)

        # If user_id provided, first get the key_id by listing keys
        if user_id and not key_id:
            list_result = nx._call_rpc("admin_list_keys", {"user_id": user_id, "limit": 1})
            keys = list_result.get("keys", [])
            if not keys:
                console.print(f"[red]Error:[/red] No keys found for user '{user_id}'")
                sys.exit(1)
            key_id = keys[0]["key_id"]

        # Call admin API with key_id
        result = nx._call_rpc("admin_get_key", {"key_id": key_id})

        if json_output:
            click.echo(json.dumps(result, indent=2))
        else:
            console.print("\n[bold]User Information[/bold]\n")
            console.print(f"User ID:      {result['user_id']}")
            console.print(f"Key ID:       {result['key_id']}")
            console.print(f"Name:         {result['name']}")
            console.print(f"Tenant:       {result['tenant_id']}")
            console.print(f"Admin:        {result['is_admin']}")
            console.print(f"Created:      {result['created_at']}")

            if result.get("expires_at"):
                console.print(f"Expires:      {result['expires_at']}")
            else:
                console.print("Expires:      Never")

            if result.get("last_used_at"):
                console.print(f"Last Used:    {result['last_used_at']}")
            else:
                console.print("Last Used:    Never")

            console.print(f"Revoked:      {result.get('revoked', False)}")

            if result.get("subject_type"):
                console.print(f"Subject Type: {result['subject_type']}")
            if result.get("subject_id"):
                console.print(f"Subject ID:   {result['subject_id']}")

    except Exception as e:
        console.print(f"[red]Error getting user:[/red] {e}")
        sys.exit(1)


@admin.command("create-agent-key")
@click.argument("user_id")
@click.argument("agent_id")
@click.option("--name", help="Human-readable name for the API key (default: 'Agent: <agent_id>')")
@click.option("--expires-days", type=int, help="API key expiry in days")
@click.option("--json-output", is_flag=True, help="Output as JSON")
@REMOTE_API_KEY_OPTION
@REMOTE_URL_OPTION
def create_agent_key(
    user_id: str,
    agent_id: str,
    name: str | None,
    expires_days: int | None,
    json_output: bool,
    remote_url: str | None,
    remote_api_key: str | None,
) -> None:
    """Create API key for an existing agent.

    This creates an independent API key for an agent to authenticate without
    using the user's credentials. This is optional - most agents should use
    the user's auth + X-Agent-ID header instead.

    \b
    Examples:
        # Create API key for alice's agent (1 day expiry)
        nexus admin create-agent-key alice alice_agent --expires-days 1

        # Create API key with custom name
        nexus admin create-agent-key alice alice_agent --name "Production Agent" --expires-days 90
    """
    try:
        nx = get_remote_client(remote_url, remote_api_key)

        # Default name if not provided
        if not name:
            name = f"Agent: {agent_id}"

        # Build parameters
        params: dict[str, Any] = {
            "user_id": user_id,
            "name": name,
            "subject_type": "agent",
            "subject_id": agent_id,
        }

        if expires_days is not None:
            params["expires_days"] = expires_days

        # Call admin API
        result = nx._call_rpc("admin_create_key", params)

        if json_output:
            click.echo(json.dumps(result, indent=2))
        else:
            console.print("[green]✓[/green] Agent API key created successfully")
            console.print("\n[yellow]⚠ Save this API key - it will only be shown once![/yellow]\n")
            console.print(f"User ID:     {result['user_id']}")
            console.print(f"Agent ID:    {agent_id}")
            console.print(f"Key ID:      {result['key_id']}")
            console.print(f"[bold]API Key:[/bold]     {result['api_key']}")
            if result.get("expires_at"):
                console.print(f"Expires:     {result['expires_at']}")

            console.print("\n[cyan]ℹ Info:[/cyan] This agent can now authenticate independently.")
            console.print("[cyan]ℹ[/cyan] Recommended: Use user auth + X-Agent-ID header instead.")

    except Exception as e:
        console.print(f"[red]Error creating agent key:[/red] {e}")
        sys.exit(1)


@admin.command("update-key")
@click.argument("key_id")
@click.option("--expires-days", type=int, help="Extend expiry by days from now")
@click.option("--is-admin", type=bool, help="Change admin status (true/false)")
@click.option("--json-output", is_flag=True, help="Output as JSON")
@REMOTE_API_KEY_OPTION
@REMOTE_URL_OPTION
def update_key(
    key_id: str,
    expires_days: int | None,
    is_admin: bool | None,
    json_output: bool,
    remote_url: str | None,
    remote_api_key: str | None,
) -> None:
    """Update API key settings.

    \b
    Examples:
        # Extend expiry by 180 days
        nexus admin update-key <key_id> --expires-days 180

        # Grant admin privileges
        nexus admin update-key <key_id> --is-admin true

        # Revoke admin privileges
        nexus admin update-key <key_id> --is-admin false
    """
    if expires_days is None and is_admin is None:
        console.print("[red]Error:[/red] Must provide --expires-days or --is-admin")
        sys.exit(1)

    try:
        nx = get_remote_client(remote_url, remote_api_key)

        # Build parameters
        params: dict[str, Any] = {"key_id": key_id}

        if expires_days is not None:
            params["expires_days"] = expires_days
        if is_admin is not None:
            params["is_admin"] = is_admin

        # Call admin API
        result = nx._call_rpc("admin_update_key", params)

        if json_output:
            click.echo(json.dumps(result, indent=2))
        else:
            console.print("[green]✓[/green] API key updated successfully")
            console.print(f"Key ID: {key_id}")
            if expires_days is not None:
                console.print(f"New expiry: {result.get('expires_at', 'N/A')}")
            if is_admin is not None:
                console.print(f"Admin: {result.get('is_admin', 'N/A')}")

    except Exception as e:
        console.print(f"[red]Error updating key:[/red] {e}")
        sys.exit(1)


def register_commands(cli: click.Group) -> None:
    """Register admin command group to the main CLI.

    Args:
        cli: The main Click group to register commands to
    """
    # Admin commands are added as a group
    cli.add_command(admin)
