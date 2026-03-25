"""Share link CLI commands — create and manage share links.

Maps to share_link_* RPC methods via rpc_call().
Issue #2812.
"""

from __future__ import annotations

import click

from nexus.cli.output import OutputOptions, add_output_options, render_output
from nexus.cli.timing import CommandTiming
from nexus.cli.utils import REMOTE_API_KEY_OPTION, REMOTE_URL_OPTION, console, rpc_call


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
def share_create(
    path: str,
    expires_in_hours: int | None,
    permission_level: str,
    password: str | None,
    output_opts: OutputOptions,
    remote_url: str | None,
    remote_api_key: str | None,
) -> None:
    """Create a share link for a file or directory.

    \b
    Examples:
        nexus share create /data/report.pdf
        nexus share create /data/report.pdf --expires 24 --password secret
        nexus share create /workspace --permission editor --json
    """
    timing = CommandTiming()
    try:
        with timing.phase("server"):
            data = rpc_call(
                remote_url,
                remote_api_key,
                "share_link_create",
                path=path,
                permission_level=permission_level,
                expires_in_hours=expires_in_hours,
                password=password,
            )

        def _render(d: dict) -> None:
            console.print("[green]Share link created[/green]")
            console.print(f"  URL:        {d.get('url', d.get('token', 'N/A'))}")
            console.print(f"  Path:       {d.get('path', path)}")
            console.print(f"  Permission: {d.get('permission_level', permission_level)}")
            if d.get("expires_at"):
                console.print(f"  Expires:    {d['expires_at'][:19]}")

        render_output(
            data=data,
            output_opts=output_opts,
            timing=timing,
            human_formatter=_render,
        )
    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        raise SystemExit(1) from None


@share.command("list")
@click.option("--path", default=None, help="Filter by path")
@add_output_options
@REMOTE_API_KEY_OPTION
@REMOTE_URL_OPTION
def share_list(
    path: str | None,
    output_opts: OutputOptions,
    remote_url: str | None,
    remote_api_key: str | None,
) -> None:
    """List active share links.

    \b
    Examples:
        nexus share list
        nexus share list --path /data --json
    """
    timing = CommandTiming()
    try:
        with timing.phase("server"):
            data = rpc_call(remote_url, remote_api_key, "share_link_list", path=path)

        def _render(d: dict) -> None:
            from rich.table import Table

            links = d.get("links", d.get("share_links", []))
            if not links:
                console.print("[yellow]No active share links[/yellow]")
                return

            table = Table(title=f"Share Links ({len(links)})")
            table.add_column("Token", style="dim")
            table.add_column("Path")
            table.add_column("Permission")
            table.add_column("Expires", style="dim")

            for link in links:
                table.add_row(
                    link.get("token", "")[:12],
                    link.get("path", ""),
                    link.get("permission_level", ""),
                    link.get("expires_at", "never")[:19],
                )
            console.print(table)

        render_output(
            data=data,
            output_opts=output_opts,
            timing=timing,
            human_formatter=_render,
        )
    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        raise SystemExit(1) from None


@share.command("show")
@click.argument("token")
@add_output_options
@REMOTE_API_KEY_OPTION
@REMOTE_URL_OPTION
def share_show(
    token: str,
    output_opts: OutputOptions,
    remote_url: str | None,
    remote_api_key: str | None,
) -> None:
    """Show share link details.

    \b
    Examples:
        nexus share show abc123 --json
    """
    timing = CommandTiming()
    try:
        with timing.phase("server"):
            data = rpc_call(remote_url, remote_api_key, "share_link_get", token=token)

        def _render(d: dict) -> None:
            console.print(f"[bold cyan]Share Link: {token}[/bold cyan]")
            console.print(f"  Path:        {d.get('path', 'N/A')}")
            console.print(f"  Permission:  {d.get('permission_level', 'N/A')}")
            console.print(f"  Created:     {d.get('created_at', 'N/A')[:19]}")
            console.print(f"  Expires:     {d.get('expires_at', 'never')[:19]}")
            console.print(f"  Access Count: {d.get('access_count', 0)}")

        render_output(
            data=data,
            output_opts=output_opts,
            timing=timing,
            human_formatter=_render,
        )
    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        raise SystemExit(1) from None


@share.command("revoke")
@click.argument("token")
@add_output_options
@REMOTE_API_KEY_OPTION
@REMOTE_URL_OPTION
def share_revoke(
    token: str,
    output_opts: OutputOptions,
    remote_url: str | None,
    remote_api_key: str | None,
) -> None:
    """Revoke a share link.

    \b
    Examples:
        nexus share revoke abc123
    """
    timing = CommandTiming()
    try:
        with timing.phase("server"):
            data = rpc_call(remote_url, remote_api_key, "share_link_revoke", token=token)

        render_output(
            data=data,
            output_opts=output_opts,
            timing=timing,
            message=f"Share link {token} revoked",
        )
    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        raise SystemExit(1) from None
