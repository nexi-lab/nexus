"""Release native advisory locks with the selected caller credential."""

import asyncio

import click

from nexus.cli.exit_codes import ExitCode
from nexus.cli.output import OutputOptions, add_output_options, render_error, render_output
from nexus.cli.timing import CommandTiming
from nexus.cli.utils import add_backend_options, open_filesystem
from nexus.remote.client import RemoteNexusFS


@click.group()
def lock() -> None:
    """Manage advisory locks on the native owner."""


@lock.command("release")
@click.argument("path")
@click.option("--lock-id", default=None, help="The lock ID returned by the native owner.")
@click.option("--force", is_flag=True, help="Force release; requires native administration rights.")
@add_output_options
@add_backend_options
def lock_release(
    path: str,
    lock_id: str | None,
    force: bool,
    output_opts: OutputOptions,
    remote_url: str | None,
    remote_api_key: str | None,
) -> None:
    """Release one native lock. Exit 1 if no matching lock was released."""
    if bool(lock_id) == force:
        raise click.UsageError("Pass exactly one of --lock-id or --force")

    async def release() -> bool:
        async with open_filesystem(remote_url, remote_api_key) as nx:
            if not isinstance(nx, RemoteNexusFS):
                raise TypeError("Native lock commands require a remote filesystem connection")
            return await asyncio.to_thread(nx.sys_unlock, path, lock_id=lock_id, force=force)

    timing = CommandTiming()
    try:
        with timing.phase("server"):
            released = asyncio.run(release())
        render_output(
            data={"path": path, "released": released},
            output_opts=output_opts,
            timing=timing,
            message=(
                f"Lock released: {path}" if released else f"No matching lock was released: {path}"
            ),
        )
        if not released:
            raise SystemExit(ExitCode.GENERAL_ERROR)
    except Exception as error:
        render_error(error=error, output_opts=output_opts, timing=timing)
