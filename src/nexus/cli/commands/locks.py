"""Lock CLI commands — list, info, and release distributed locks."""

import click

from nexus.cli.output import OutputOptions, add_output_options, render_output
from nexus.cli.timing import CommandTiming
from nexus.cli.utils import (
    REMOTE_API_KEY_OPTION,
    REMOTE_URL_OPTION,
    console,
    rpc_call,
)


@click.group()
def lock() -> None:
    """Distributed lock management.

    \b
    Prerequisites:
        - Running Nexus server with Redis/Dragonfly
        - Server URL (set via NEXUS_URL or --remote-url)
        - API key (set via NEXUS_API_KEY or --remote-api-key)

    \b
    Examples:
        nexus lock list
        nexus lock info /data/shared.db
        nexus lock release /data/shared.db --force
    """


@lock.command("list")
@click.option("--zone-id", default=None, help="Filter by zone ID")
@add_output_options
@REMOTE_API_KEY_OPTION
@REMOTE_URL_OPTION
def lock_list(
    zone_id: str | None,  # noqa: ARG001 — zone filtering via __sys__ TBD
    output_opts: OutputOptions,
    remote_url: str | None,
    remote_api_key: str | None,
) -> None:
    """List active locks.

    \b
    Examples:
        nexus lock list
        nexus lock list --zone-id org_acme --json
    """
    timing = CommandTiming()
    try:
        with timing.phase("server"):
            data = rpc_call(
                remote_url, remote_api_key, "sys_readdir", path="/__sys__/locks/", details=True
            )

        def _render(d: dict) -> None:
            from rich.table import Table

            locks = d.get("locks", [])
            if not locks:
                console.print("[nexus.warning]No active locks[/nexus.warning]")
                return

            table = Table(title=f"Active Locks ({d.get('count', len(locks))})")
            table.add_column("Path")
            table.add_column("Mode")
            table.add_column("Holders", justify="right")
            table.add_column("Expires At", style="nexus.muted")

            for lk in locks:
                table.add_row(
                    lk.get("path", ""),
                    lk.get("mode", ""),
                    str(lk.get("current_holders", 1)),
                    lk.get("expires_at", "")[:19],
                )
            console.print(table)

        render_output(
            data=data,
            output_opts=output_opts,
            timing=timing,
            human_formatter=_render,
        )
    except Exception as e:
        console.print(f"[nexus.error]Error:[/nexus.error] {e}")
        raise SystemExit(1) from None


@lock.command("info")
@click.argument("path")
@add_output_options
@REMOTE_API_KEY_OPTION
@REMOTE_URL_OPTION
def lock_info(
    path: str,
    output_opts: OutputOptions,
    remote_url: str | None,
    remote_api_key: str | None,
) -> None:
    """Show lock status for a path.

    \b
    Examples:
        nexus lock info /data/shared.db
        nexus lock info /workspace/file.txt --json
    """
    timing = CommandTiming()
    try:
        with timing.phase("server"):
            data = rpc_call(remote_url, remote_api_key, "sys_stat", path=path, include_lock=True)

        def _render(d: dict) -> None:
            console.print(f"[bold nexus.value]Lock Status: {path}[/bold nexus.value]")
            console.print(
                f"  Locked:  {'[nexus.error]Yes[/nexus.error]' if d.get('locked') else '[nexus.success]No[/nexus.success]'}"
            )
            info = d.get("lock_info")
            if info:
                console.print(f"  Mode:    {info.get('mode', 'N/A')}")
                console.print(f"  Lock ID: {info.get('lock_id', 'N/A')}")
                console.print(f"  Fence:   {info.get('fence_token', 'N/A')}")
                if info.get("expires_at"):
                    console.print(f"  Expires: {info['expires_at'][:19]}")

        render_output(
            data=data,
            output_opts=output_opts,
            timing=timing,
            human_formatter=_render,
        )
    except Exception as e:
        console.print(f"[nexus.error]Error:[/nexus.error] {e}")
        raise SystemExit(1) from None


@lock.command("release")
@click.argument("path")
@click.option("--lock-id", default=None, help="Lock ID (required for non-force release)")
@click.option("--force", is_flag=True, help="Force-release (admin only)")
@REMOTE_API_KEY_OPTION
@REMOTE_URL_OPTION
def lock_release(
    path: str,
    lock_id: str | None,
    force: bool,
    remote_url: str | None,
    remote_api_key: str | None,
) -> None:
    """Release a lock.

    \b
    Examples:
        nexus lock release /data/shared.db --lock-id abc123
        nexus lock release /data/shared.db --force
    """
    try:
        if force:
            rpc_call(remote_url, remote_api_key, "sys_unlock", path=path, force=True)
        else:
            if not lock_id:
                console.print(
                    "[nexus.error]--lock-id required (use --force for admin release)[/nexus.error]"
                )
                raise SystemExit(1)
            rpc_call(remote_url, remote_api_key, "sys_unlock", path=path, lock_id=lock_id)
        console.print(f"[nexus.success]Lock released:[/nexus.success] {path}")
    except Exception as e:
        console.print(f"[nexus.error]Error:[/nexus.error] {e}")
        raise SystemExit(1) from None
