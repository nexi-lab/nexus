"""Share link CLI commands — create and manage share links.

Maps to /api/v2/share-links/* endpoints via ShareClient.
Issue #2812.
"""

from __future__ import annotations

import click

from nexus.cli.clients.share import ShareClient
from nexus.cli.output import add_output_options
from nexus.cli.service_command import ServiceResult, service_command
from nexus.cli.utils import REMOTE_API_KEY_OPTION, REMOTE_URL_OPTION


@click.group()
def share() -> None:
    """Share link management.

    \b
    Create, list, and revoke capability-based share links.

    \b
    Examples:
        nexus share create /data/report.pdf --expires 24
        nexus share list --json
        nexus share revoke <token>
    """


@share.command("create")
@click.argument("path")
@click.option("--expires", "expires_in_hours", type=int, default=None, help="Expiration in hours")
@click.option(
    "--permission",
    "permission_level",
    default="viewer",
    show_default=True,
    help="Permission level (viewer, editor)",
)
@click.option("--password", default=None, help="Optional password protection")
@add_output_options
@REMOTE_API_KEY_OPTION
@REMOTE_URL_OPTION
@service_command(client_class=ShareClient)
def share_create(
    client: ShareClient,
    path: str,
    expires_in_hours: int | None,
    permission_level: str,
    password: str | None,
) -> ServiceResult:
    """Create a share link for a file or directory.

    \b
    Examples:
        nexus share create /data/report.pdf
        nexus share create /data/report.pdf --expires 24 --password secret
        nexus share create /workspace --permission editor --json
    """
    data = client.create(
        path,
        permission_level=permission_level,
        expires_in_hours=expires_in_hours,
        password=password,
    )

    def _render(d: dict) -> None:
        from nexus.cli.theme import console

        console.print("[nexus.success]Share link created[/nexus.success]")
        console.print(f"  URL:        {d.get('url', d.get('token', 'N/A'))}")
        console.print(f"  Path:       {d.get('path', path)}")
        console.print(f"  Permission: {d.get('permission_level', permission_level)}")
        if d.get("expires_at"):
            console.print(f"  Expires:    {d['expires_at'][:19]}")

    return ServiceResult(data=data, human_formatter=_render)


@share.command("list")
@click.option("--path", default=None, help="Filter by path")
@add_output_options
@REMOTE_API_KEY_OPTION
@REMOTE_URL_OPTION
@service_command(client_class=ShareClient)
def share_list(client: ShareClient, path: str | None) -> ServiceResult:
    """List active share links.

    \b
    Examples:
        nexus share list
        nexus share list --path /data --json
    """
    data = client.list(path=path)

    def _render(d: dict) -> None:
        from rich.table import Table

        from nexus.cli.theme import console

        links = d.get("links", d.get("share_links", []))
        if not links:
            console.print("[nexus.warning]No active share links[/nexus.warning]")
            return

        table = Table(title=f"Share Links ({len(links)})")
        table.add_column("Token", style="nexus.muted")
        table.add_column("Path")
        table.add_column("Permission")
        table.add_column("Expires", style="nexus.muted")

        for link in links:
            table.add_row(
                link.get("token", "")[:12],
                link.get("path", ""),
                link.get("permission_level", ""),
                link.get("expires_at", "never")[:19],
            )
        console.print(table)

    return ServiceResult(data=data, human_formatter=_render)


@share.command("show")
@click.argument("token")
@add_output_options
@REMOTE_API_KEY_OPTION
@REMOTE_URL_OPTION
@service_command(client_class=ShareClient)
def share_show(client: ShareClient, token: str) -> ServiceResult:
    """Show share link details.

    \b
    Examples:
        nexus share show abc123 --json
    """
    data = client.show(token)

    def _render(d: dict) -> None:
        from nexus.cli.theme import console

        console.print(f"[bold cyan]Share Link: {token}[/bold cyan]")
        console.print(f"  Path:        {d.get('path', 'N/A')}")
        console.print(f"  Permission:  {d.get('permission_level', 'N/A')}")
        console.print(f"  Created:     {d.get('created_at', 'N/A')[:19]}")
        console.print(f"  Expires:     {d.get('expires_at', 'never')[:19]}")
        console.print(f"  Access Count: {d.get('access_count', 0)}")

    return ServiceResult(data=data, human_formatter=_render)


@share.command("revoke")
@click.argument("token")
@add_output_options
@REMOTE_API_KEY_OPTION
@REMOTE_URL_OPTION
@service_command(client_class=ShareClient)
def share_revoke(client: ShareClient, token: str) -> ServiceResult:
    """Revoke a share link.

    \b
    Examples:
        nexus share revoke abc123
    """
    data = client.revoke(token)
    return ServiceResult(data=data, message=f"Share link {token} revoked")
