"""Admin CLI commands for user and API key management.

This module provides CLI commands for the Admin API (issue #322, #266).
Admin commands allow remote management of users and API keys without SSH access.

All commands require:
1. A running Nexus server with database-backed authentication
2. An admin API key set via NEXUS_API_KEY or --remote-api-key
3. Server URL set via NEXUS_URL or --remote-url
"""

import contextlib
import os
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import click
from rich.table import Table

from nexus.cli.output import OutputOptions, add_output_options, render_output
from nexus.cli.theme import console
from nexus.cli.timing import CommandTiming
from nexus.cli.utils import (
    REMOTE_API_KEY_OPTION,
    REMOTE_URL_OPTION,
)
from nexus.contracts.constants import ROOT_ZONE_ID

# Type alias for the RPC transport callable returned by get_admin_rpc().
AdminRPC = Callable[[str, dict[str, Any] | None], Any]


def get_admin_rpc(url: str | None, api_key: str | None) -> AdminRPC:
    """Get RPC transport callable for admin management-plane operations.

    Admin commands are management-plane RPCs (like ioctl), not data-plane
    VFS operations (read/write/mkdir). They only need the HTTP transport
    to call server-side admin endpoints -- no full NexusFS instance needed.

    Args:
        url: Server URL (from --remote-url or NEXUS_URL)
        api_key: Admin API key (from --remote-api-key or NEXUS_API_KEY)

    Returns:
        RPC callable: ``(method: str, params: dict | None) -> Any``

    Raises:
        SystemExit: If URL or API key not provided
    """
    if not url:
        console.print(
            "[nexus.error]Error:[/nexus.error] Server URL required. Set NEXUS_URL or use --remote-url"
        )
        sys.exit(1)

    if not api_key:
        console.print(
            "[nexus.error]Error:[/nexus.error] Admin API key required. Set NEXUS_API_KEY or use --remote-api-key"
        )
        sys.exit(1)

    from urllib.parse import urlparse

    from nexus.remote.rpc_transport import RPCTransport
    from nexus.security.tls.config import ZoneTlsConfig

    parsed = urlparse(url)

    # Load runtime state unconditionally for both port and TLS discovery.
    # This avoids the bug where TLS fallback only ran when NEXUS_GRPC_PORT
    # was absent — the two are independent concerns.
    from nexus.cli.state import load_project_config_optional, load_runtime_state

    cfg = load_project_config_optional()
    # data_dir: nexus.yaml > NEXUS_DATA_DIR env (Docker containers) > default
    data_dir = cfg.get("data_dir", os.environ.get("NEXUS_DATA_DIR", "./nexus-data"))
    state = load_runtime_state(data_dir)

    # gRPC port: env var > state.json > config > default
    grpc_port = int(os.environ.get("NEXUS_GRPC_PORT", "0"))
    if not grpc_port:
        grpc_port = state.get("ports", {}).get("grpc", cfg.get("ports", {}).get("grpc", 2028))

    # TLS: trust explicit runtime state / env, not blind auto-detection from
    # NEXUS_DATA_DIR. Standalone demo/shared stacks may still have a tls/
    # directory for internal services while exposing insecure host gRPC.
    tls = state.get("tls", {})
    if tls.get("cert") and not os.environ.get("NEXUS_TLS_CERT"):
        os.environ["NEXUS_TLS_CERT"] = tls["cert"]
        os.environ["NEXUS_TLS_KEY"] = tls.get("key", "")
        os.environ["NEXUS_TLS_CA"] = tls.get("ca", "")

    grpc_address = f"{parsed.hostname}:{grpc_port}"

    tls_config = None
    _grpc_tls_env = os.environ.get("NEXUS_GRPC_TLS", "").lower()
    _grpc_tls_off = _grpc_tls_env in ("false", "0", "no")
    _grpc_tls_on = _grpc_tls_env in ("true", "1", "yes")

    # NEXUS_GRPC_TLS=false is an unconditional disable — matches server
    # semantics. Operator explicitly wants insecure, even if stale certs
    # exist in state.json or env.
    if _grpc_tls_off:
        pass
    elif tls.get("cert") or os.environ.get("NEXUS_TLS_CERT"):
        tls_config = ZoneTlsConfig.from_env()
    elif data_dir:
        with contextlib.suppress(Exception):
            tls_config = ZoneTlsConfig.from_data_dir(data_dir)

    # Fail closed: explicit true but no certs resolved
    if _grpc_tls_on and tls_config is None:
        raise click.ClickException(
            "NEXUS_GRPC_TLS=true but no TLS certificates found. "
            "Provide certs via NEXUS_TLS_CERT/KEY/CA, "
            "in {data_dir}/tls/, or configure TLS in state.json."
        )
    transport = RPCTransport(server_address=grpc_address, auth_token=api_key, tls_config=tls_config)
    return transport.call_rpc


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
        export NEXUS_URL=http://localhost:2026
        export NEXUS_API_KEY=<admin_api_key>

        nexus admin create-user alice --name "Alice's Laptop"
        nexus admin list-users
        nexus admin revoke-key <key_id>
    """
    pass


def _parse_grants(grant_strings: tuple[str, ...]) -> list[dict[str, str]] | None:
    """Parse --grant PATH:ROLE options into a list of grant dicts.

    Returns None if no grants provided, or a list of {"path": ..., "role": ...}.
    """
    if not grant_strings:
        return None
    grants: list[dict[str, str]] = []
    for g in grant_strings:
        if ":" not in g:
            raise click.BadParameter(
                f"Invalid grant format: {g!r}. Expected PATH:ROLE (e.g. /workspace/*:editor)",
                param_hint="--grant",
            )
        path, role = g.rsplit(":", 1)
        if role not in ("viewer", "editor", "owner"):
            raise click.BadParameter(
                f"Invalid role: {role!r}. Must be viewer, editor, or owner",
                param_hint="--grant",
            )
        grants.append({"path": path, "role": role})
    return grants


def _print_grants(grants: list[dict[str, str]]) -> None:
    """Print created grants to console."""
    console.print(f"\nGrants ({len(grants)}):")
    for g in grants:
        console.print(f"  {g['role']:8s} {g['path']}")


@admin.command("create-user")
@click.argument("user_id")
@click.option("--name", required=True, help="Human-readable name for the API key")
@click.option("--email", help="User email (for documentation purposes)")
@click.option("--is-admin", is_flag=True, help="Grant admin privileges")
@click.option("--expires-days", type=int, help="API key expiry in days")
@click.option("--zone-id", default=ROOT_ZONE_ID, help="Zone ID (default: root)")
@click.option("--subject-type", default="user", help="Subject type: user or agent")
@click.option("--grant", "grants", multiple=True, help="Path grant as PATH:ROLE (repeatable)")
@add_output_options
@REMOTE_API_KEY_OPTION
@REMOTE_URL_OPTION
def create_user(
    user_id: str,
    name: str,
    email: str | None,
    is_admin: bool,
    expires_days: int | None,
    zone_id: str,
    subject_type: str,
    grants: tuple[str, ...],
    output_opts: OutputOptions,
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

        # Create scoped user with per-path grants
        nexus admin create-user alice --name "Alice" \\
            --grant "/workspace/project-a/*:editor" \\
            --grant "/workspace/shared/*:viewer"
    """
    timing = CommandTiming()
    try:
        call_rpc = get_admin_rpc(remote_url, remote_api_key)

        # Build parameters
        params: dict[str, Any] = {
            "user_id": user_id,
            "name": name,
            "is_admin": is_admin,
            "zone_id": zone_id,
            "subject_type": subject_type,
        }

        if email is not None:
            params["email"] = email

        if expires_days is not None:
            params["expires_days"] = expires_days

        parsed_grants = _parse_grants(grants)
        if parsed_grants:
            params["grants"] = parsed_grants

        # Call admin API
        with timing.phase("server"):
            result = call_rpc("admin_create_key", params)

        def _render(data: dict[str, Any]) -> None:
            console.print("[nexus.success]\u2713[/nexus.success] User created successfully")
            console.print(
                "\n[nexus.warning]\u26a0 Save this API key - it will only be shown once![/nexus.warning]\n"
            )
            console.print(f"User ID:     {data['user_id']}")
            console.print(f"Key ID:      {data['key_id']}")
            console.print(f"[bold]API Key:[/bold]     {data['api_key']}")
            console.print(f"Zone:        {data['zone_id']}")
            console.print(f"Admin:       {data['is_admin']}")
            if data.get("expires_at"):
                console.print(f"Expires:     {data['expires_at']}")
            if data.get("grants"):
                _print_grants(data["grants"])

            if email:
                console.print(f"\n[nexus.muted]Email: {email}[/nexus.muted]")

        render_output(
            data=result,
            output_opts=output_opts,
            timing=timing,
            human_formatter=_render,
        )

    except Exception as e:
        console.print(f"[nexus.error]Error creating user:[/nexus.error] {e}")
        sys.exit(1)


@admin.command("list-users")
@click.option("--user-id", help="Filter by user ID")
@click.option("--zone-id", help="Filter by zone ID")
@click.option("--is-admin", is_flag=True, help="Filter for admin keys only")
@click.option("--include-revoked", is_flag=True, help="Include revoked keys")
@click.option("--include-expired", is_flag=True, help="Include expired keys")
@click.option("--limit", type=int, default=100, help="Maximum number of results")
@add_output_options
@REMOTE_API_KEY_OPTION
@REMOTE_URL_OPTION
def list_users(
    user_id: str | None,
    zone_id: str | None,
    is_admin: bool,
    include_revoked: bool,
    include_expired: bool,
    limit: int,
    output_opts: OutputOptions,
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
    timing = CommandTiming()
    try:
        call_rpc = get_admin_rpc(remote_url, remote_api_key)

        # Build parameters
        params: dict[str, Any] = {
            "limit": limit,
            "include_revoked": include_revoked,
            "include_expired": include_expired,
        }

        if user_id:
            params["user_id"] = user_id
        if zone_id:
            params["zone_id"] = zone_id
        if is_admin:
            params["is_admin"] = True

        # Call admin API
        with timing.phase("server"):
            result = call_rpc("admin_list_keys", params)
            keys = result.get("keys", [])

        if not keys:
            console.print("[nexus.warning]No users found.[/nexus.warning]")
            return

        def _render(data: list[dict[str, Any]]) -> None:
            # Create table
            table = Table(title=f"API Keys ({len(data)} total)")
            table.add_column("User ID", style="nexus.value")
            table.add_column("Name")
            table.add_column("Key ID", style="nexus.muted")
            table.add_column("Admin", style="nexus.success")
            table.add_column("Created", style="nexus.reference")
            table.add_column("Expires", style="nexus.warning")
            table.add_column("Status")

            for key in data:
                # Determine status
                status = "Active"
                status_style = "nexus.success"
                if key.get("revoked"):
                    status = "Revoked"
                    status_style = "nexus.error"
                elif key.get("expires_at"):
                    try:
                        # Parse ISO format datetime
                        expires = datetime.fromisoformat(key["expires_at"].replace("Z", "+00:00"))
                        if expires < datetime.now(UTC):
                            status = "Expired"
                            status_style = "nexus.warning"
                    except (ValueError, TypeError):
                        pass

                table.add_row(
                    key.get("user_id", ""),
                    key.get("name", ""),
                    key.get("key_id", "")[:16] + "...",
                    "\u2713" if key.get("is_admin") else "",
                    key.get("created_at", "")[:10] if key.get("created_at") else "",
                    key.get("expires_at", "")[:10] if key.get("expires_at") else "Never",
                    f"[{status_style}]{status}[/{status_style}]",
                )

            console.print(table)

        render_output(
            data=keys,
            output_opts=output_opts,
            timing=timing,
            human_formatter=_render,
        )

    except Exception as e:
        console.print(f"[nexus.error]Error listing users:[/nexus.error] {e}")
        sys.exit(1)


@admin.command("revoke-key")
@click.argument("key_id")
@add_output_options
@REMOTE_API_KEY_OPTION
@REMOTE_URL_OPTION
def revoke_key(
    key_id: str,
    output_opts: OutputOptions,
    remote_url: str | None,
    remote_api_key: str | None,
) -> None:
    """Revoke an API key.

    \b
    Examples:
        nexus admin revoke-key d6f5e137-5fce-4e06-9432-6e30324dfad1
    """
    timing = CommandTiming()
    try:
        call_rpc = get_admin_rpc(remote_url, remote_api_key)

        # Call admin API
        with timing.phase("server"):
            result = call_rpc("admin_revoke_key", {"key_id": key_id})

        def _render(data: dict[str, Any]) -> None:  # noqa: ARG001
            console.print("[nexus.success]\u2713[/nexus.success] API key revoked successfully")
            console.print(f"Key ID: {key_id}")

        render_output(
            data=result,
            output_opts=output_opts,
            timing=timing,
            human_formatter=_render,
        )

    except Exception as e:
        console.print(f"[nexus.error]Error revoking key:[/nexus.error] {e}")
        sys.exit(1)


@admin.command("create-key")
@click.argument("user_id")
@click.option("--name", required=True, help="Human-readable name for the new key")
@click.option("--expires-days", type=int, help="API key expiry in days")
@click.option("--grant", "grants", multiple=True, help="Path grant as PATH:ROLE (repeatable)")
@add_output_options
@REMOTE_API_KEY_OPTION
@REMOTE_URL_OPTION
def create_key(
    user_id: str,
    name: str,
    expires_days: int | None,
    grants: tuple[str, ...],
    output_opts: OutputOptions,
    remote_url: str | None,
    remote_api_key: str | None,
) -> None:
    """Create additional API key for existing user.

    \b
    Examples:
        nexus admin create-key alice --name "Alice's new laptop" --expires-days 90

        # Create scoped key with grants
        nexus admin create-key alice --name "Project key" \\
            --grant "/workspace/project-a/*:editor"
    """
    timing = CommandTiming()
    try:
        call_rpc = get_admin_rpc(remote_url, remote_api_key)

        # Build parameters
        params: dict[str, Any] = {
            "user_id": user_id,
            "name": name,
        }

        if expires_days is not None:
            params["expires_days"] = expires_days

        parsed_grants = _parse_grants(grants)
        if parsed_grants:
            params["grants"] = parsed_grants

        # Call admin API
        with timing.phase("server"):
            result = call_rpc("admin_create_key", params)

        def _render(data: dict[str, Any]) -> None:
            console.print("[nexus.success]\u2713[/nexus.success] API key created successfully")
            console.print(
                "\n[nexus.warning]\u26a0 Save this API key - it will only be shown once![/nexus.warning]\n"
            )
            console.print(f"User ID:     {data['user_id']}")
            console.print(f"Key ID:      {data['key_id']}")
            console.print(f"[bold]API Key:[/bold]     {data['api_key']}")
            if data.get("expires_at"):
                console.print(f"Expires:     {data['expires_at']}")
            if data.get("grants"):
                _print_grants(data["grants"])

        render_output(
            data=result,
            output_opts=output_opts,
            timing=timing,
            human_formatter=_render,
        )

    except Exception as e:
        console.print(f"[nexus.error]Error creating key:[/nexus.error] {e}")
        sys.exit(1)


@admin.command("get-user")
@click.option("--user-id", help="User ID to look up")
@click.option("--key-id", help="Key ID to look up")
@add_output_options
@REMOTE_API_KEY_OPTION
@REMOTE_URL_OPTION
def get_user(
    user_id: str | None,
    key_id: str | None,
    output_opts: OutputOptions,
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
        console.print("[nexus.error]Error:[/nexus.error] Must provide either --user-id or --key-id")
        sys.exit(1)

    timing = CommandTiming()
    try:
        call_rpc = get_admin_rpc(remote_url, remote_api_key)

        with timing.phase("server"):
            # If user_id provided, first get the key_id by listing keys
            if user_id and not key_id:
                list_result = call_rpc("admin_list_keys", {"user_id": user_id, "limit": 1})
                keys = list_result.get("keys", [])
                if not keys:
                    console.print(
                        f"[nexus.error]Error:[/nexus.error] No keys found for user '{user_id}'"
                    )
                    sys.exit(1)
                key_id = keys[0]["key_id"]

            # Call admin API with key_id
            result = call_rpc("admin_get_key", {"key_id": key_id})

        def _render(data: dict[str, Any]) -> None:
            console.print("\n[bold]User Information[/bold]\n")
            console.print(f"User ID:      {data['user_id']}")
            console.print(f"Key ID:       {data['key_id']}")
            console.print(f"Name:         {data['name']}")
            console.print(f"Zone:         {data['zone_id']}")
            console.print(f"Admin:        {data['is_admin']}")
            console.print(f"Created:      {data['created_at']}")

            if data.get("expires_at"):
                console.print(f"Expires:      {data['expires_at']}")
            else:
                console.print("Expires:      Never")

            if data.get("last_used_at"):
                console.print(f"Last Used:    {data['last_used_at']}")
            else:
                console.print("Last Used:    Never")

            console.print(f"Revoked:      {data.get('revoked', False)}")

            if data.get("subject_type"):
                console.print(f"Subject Type: {data['subject_type']}")
            if data.get("subject_id"):
                console.print(f"Subject ID:   {data['subject_id']}")

        render_output(
            data=result,
            output_opts=output_opts,
            timing=timing,
            human_formatter=_render,
        )

    except Exception as e:
        console.print(f"[nexus.error]Error getting user:[/nexus.error] {e}")
        sys.exit(1)


@admin.command("create-agent-key")
@click.argument("user_id")
@click.argument("agent_id")
@click.option("--name", help="Human-readable name for the API key (default: 'Agent: <agent_id>')")
@click.option("--expires-days", type=int, help="API key expiry in days")
@click.option("--grant", "grants", multiple=True, help="Path grant as PATH:ROLE (repeatable)")
@add_output_options
@REMOTE_API_KEY_OPTION
@REMOTE_URL_OPTION
def create_agent_key(
    user_id: str,
    agent_id: str,
    name: str | None,
    expires_days: int | None,
    grants: tuple[str, ...],
    output_opts: OutputOptions,
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

        # Create scoped agent key with grants
        nexus admin create-agent-key alice alice_agent \\
            --grant "/workspace/tools/*:editor" --expires-days 1
    """
    timing = CommandTiming()
    try:
        call_rpc = get_admin_rpc(remote_url, remote_api_key)

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

        parsed_grants = _parse_grants(grants)
        if parsed_grants:
            params["grants"] = parsed_grants

        # Call admin API
        with timing.phase("server"):
            result = call_rpc("admin_create_key", params)

        def _render(data: dict[str, Any]) -> None:
            console.print(
                "[nexus.success]\u2713[/nexus.success] Agent API key created successfully"
            )
            console.print(
                "\n[nexus.warning]\u26a0 Save this API key - it will only be shown once![/nexus.warning]\n"
            )
            console.print(f"User ID:     {data['user_id']}")
            console.print(f"Agent ID:    {agent_id}")
            console.print(f"Key ID:      {data['key_id']}")
            console.print(f"[bold]API Key:[/bold]     {data['api_key']}")
            if data.get("expires_at"):
                console.print(f"Expires:     {data['expires_at']}")
            if data.get("grants"):
                _print_grants(data["grants"])

            console.print(
                "\n[nexus.value]\u2139 Info:[/nexus.value] This agent can now authenticate independently."
            )
            console.print(
                "[nexus.value]\u2139[/nexus.value] Recommended: Use user auth + X-Agent-ID header instead."
            )

        render_output(
            data=result,
            output_opts=output_opts,
            timing=timing,
            human_formatter=_render,
        )

    except Exception as e:
        console.print(f"[nexus.error]Error creating agent key:[/nexus.error] {e}")
        sys.exit(1)


@admin.command("update-key")
@click.argument("key_id")
@click.option("--expires-days", type=int, help="Extend expiry by days from now")
@click.option("--is-admin", type=bool, help="Change admin status (true/false)")
@add_output_options
@REMOTE_API_KEY_OPTION
@REMOTE_URL_OPTION
def update_key(
    key_id: str,
    expires_days: int | None,
    is_admin: bool | None,
    output_opts: OutputOptions,
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
        console.print("[nexus.error]Error:[/nexus.error] Must provide --expires-days or --is-admin")
        sys.exit(1)

    timing = CommandTiming()
    try:
        call_rpc = get_admin_rpc(remote_url, remote_api_key)

        # Build parameters
        params: dict[str, Any] = {"key_id": key_id}

        if expires_days is not None:
            params["expires_days"] = expires_days
        if is_admin is not None:
            params["is_admin"] = is_admin

        # Call admin API
        with timing.phase("server"):
            result = call_rpc("admin_update_key", params)

        def _render(data: dict[str, Any]) -> None:
            console.print("[nexus.success]\u2713[/nexus.success] API key updated successfully")
            console.print(f"Key ID: {key_id}")
            if expires_days is not None:
                console.print(f"New expiry: {data.get('expires_at', 'N/A')}")
            if is_admin is not None:
                console.print(f"Admin: {data.get('is_admin', 'N/A')}")

        render_output(
            data=result,
            output_opts=output_opts,
            timing=timing,
            human_formatter=_render,
        )

    except Exception as e:
        console.print(f"[nexus.error]Error updating key:[/nexus.error] {e}")
        sys.exit(1)


@admin.command("gc-versions")
@click.option("--dry-run/--execute", default=True, help="Dry run (default) or execute")
@click.option("--retention-days", type=int, help="Override retention days")
@click.option("--max-versions", type=int, help="Override max versions per resource")
@add_output_options
@REMOTE_API_KEY_OPTION
@REMOTE_URL_OPTION
def gc_versions(
    dry_run: bool,
    retention_days: int | None,
    max_versions: int | None,
    output_opts: OutputOptions,
    remote_url: str | None,
    remote_api_key: str | None,
) -> None:
    """Trigger version history garbage collection (Issue #974).

    Cleans up old version_history entries based on retention policy.
    By default runs in dry-run mode (shows what would be deleted).

    \b
    Examples:
        # Dry run - see what would be deleted
        nexus admin gc-versions

        # Execute with default settings
        nexus admin gc-versions --execute

        # Custom retention (keep 7 days)
        nexus admin gc-versions --execute --retention-days 7

        # Keep only 50 versions per file
        nexus admin gc-versions --execute --max-versions 50
    """
    timing = CommandTiming()
    try:
        call_rpc = get_admin_rpc(remote_url, remote_api_key)

        # Build parameters
        params: dict[str, Any] = {"dry_run": dry_run}

        if retention_days is not None:
            params["retention_days"] = retention_days
        if max_versions is not None:
            params["max_versions"] = max_versions

        # Call admin API
        with timing.phase("server"):
            result = call_rpc("admin_gc_versions", params)

        def _render(data: dict[str, Any]) -> None:
            mode = (
                "[nexus.warning]DRY RUN[/nexus.warning]"
                if dry_run
                else "[nexus.success]EXECUTED[/nexus.success]"
            )
            console.print(f"\n{mode} Version History Garbage Collection\n")

            table = Table(show_header=False, box=None)
            table.add_column("Metric", style="nexus.value")
            table.add_column("Value")

            table.add_row("Deleted by age:", str(data.get("deleted_by_age", 0)))
            table.add_row("Deleted by count:", str(data.get("deleted_by_count", 0)))
            table.add_row("Total deleted:", f"[bold]{data.get('total_deleted', 0)}[/bold]")

            bytes_reclaimed = data.get("bytes_reclaimed", 0)
            if bytes_reclaimed > 1024 * 1024:
                size_str = f"{bytes_reclaimed / 1024 / 1024:.2f} MB"
            elif bytes_reclaimed > 1024:
                size_str = f"{bytes_reclaimed / 1024:.2f} KB"
            else:
                size_str = f"{bytes_reclaimed} bytes"
            table.add_row("Space reclaimed:", size_str)
            table.add_row("Duration:", f"{data.get('duration_seconds', 0):.2f}s")

            console.print(table)

            if dry_run:
                console.print(
                    "\n[nexus.muted]Run with --execute to perform actual deletion[/nexus.muted]"
                )

        render_output(
            data=result,
            output_opts=output_opts,
            timing=timing,
            human_formatter=_render,
        )

    except Exception as e:
        console.print(f"[nexus.error]Error running GC:[/nexus.error] {e}")
        sys.exit(1)


@admin.command("gc-versions-stats")
@add_output_options
@REMOTE_API_KEY_OPTION
@REMOTE_URL_OPTION
def gc_versions_stats(
    output_opts: OutputOptions,
    remote_url: str | None,
    remote_api_key: str | None,
) -> None:
    """Show version history table statistics (Issue #974).

    Displays current size and configuration of the version_history table.

    \b
    Examples:
        nexus admin gc-versions-stats
    """
    timing = CommandTiming()
    try:
        call_rpc = get_admin_rpc(remote_url, remote_api_key)

        # Call admin API
        with timing.phase("server"):
            result = call_rpc("admin_gc_versions_stats", {})

        def _render(data: dict[str, Any]) -> None:
            console.print("\n[bold]Version History Statistics[/bold]\n")

            table = Table(show_header=False, box=None)
            table.add_column("Metric", style="nexus.value")
            table.add_column("Value")

            table.add_row("Total versions:", f"{data.get('total_versions', 0):,}")
            table.add_row("Unique resources:", f"{data.get('unique_resources', 0):,}")

            total_bytes = data.get("total_bytes", 0)
            if total_bytes > 1024 * 1024 * 1024:
                size_str = f"{total_bytes / 1024 / 1024 / 1024:.2f} GB"
            elif total_bytes > 1024 * 1024:
                size_str = f"{total_bytes / 1024 / 1024:.2f} MB"
            elif total_bytes > 1024:
                size_str = f"{total_bytes / 1024:.2f} KB"
            else:
                size_str = f"{total_bytes} bytes"
            table.add_row("Total size:", size_str)

            table.add_row(
                "Oldest version:",
                data.get("oldest_version", "N/A")[:19] if data.get("oldest_version") else "N/A",
            )
            table.add_row(
                "Newest version:",
                data.get("newest_version", "N/A")[:19] if data.get("newest_version") else "N/A",
            )

            console.print(table)

            # Show GC config
            gc_config = data.get("gc_config", {})
            if gc_config:
                console.print("\n[bold]GC Configuration[/bold]\n")

                config_table = Table(show_header=False, box=None)
                config_table.add_column("Setting", style="nexus.value")
                config_table.add_column("Value")

                status = (
                    "[nexus.success]Enabled[/nexus.success]"
                    if gc_config.get("enabled")
                    else "[nexus.error]Disabled[/nexus.error]"
                )
                config_table.add_row("Status:", status)
                config_table.add_row("Retention:", f"{gc_config.get('retention_days', 30)} days")
                config_table.add_row(
                    "Max versions/resource:", str(gc_config.get("max_versions_per_resource", 100))
                )
                config_table.add_row(
                    "Run interval:", f"{gc_config.get('run_interval_hours', 24)} hours"
                )

                console.print(config_table)

        render_output(
            data=result,
            output_opts=output_opts,
            timing=timing,
            human_formatter=_render,
        )

    except Exception as e:
        console.print(f"[nexus.error]Error getting stats:[/nexus.error] {e}")
        sys.exit(1)


def register_commands(cli: click.Group) -> None:
    """Register admin command group to the main CLI.

    Args:
        cli: The main Click group to register commands to
    """
    # Admin commands are added as a group
    cli.add_command(admin)
