"""CLI entry point for nexus-fs.

Provides the `nexus-fs` console command with subcommands:
- nexus-fs mount       — register backends for later use
- nexus-fs doctor      — diagnostic checks (environment, backends, mounts)
- nexus-fs playground  — interactive TUI file browser
- nexus-fs cp          — copy files between mounted backends
- nexus-fs auth        — credential management

This module is referenced by pyproject.toml [project.scripts].
It stays thin: imports are deferred to keep startup fast and to avoid
pulling in optional dependencies (e.g., Textual) when they aren't needed.
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import asdict

import click

from nexus.fs._auth_cli import auth as auth_group
from nexus.fs._output import OutputOptions, add_output_options, render_output


@click.group(invoke_without_command=True)
@click.version_option(package_name="nexus-fs")
@click.pass_context
def main(ctx: click.Context) -> None:
    """nexus-fs: unified filesystem for cloud storage."""
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


@main.command("mount")
@click.argument("uris", nargs=-1, required=True)
@click.option(
    "--at",
    default=None,
    help="Custom mount point (only valid with a single URI).",
)
@add_output_options
def mount_cmd(uris: tuple[str, ...], at: str | None, output_opts: OutputOptions) -> None:
    """Register backends for later use by other commands.

    Mounts the given backend URIs and persists them so that subsequent
    commands (cp, playground) can auto-discover them without needing
    URIs again.

    \b
    Examples:

    \b
      nexus-fs mount s3://my-bucket
      nexus-fs mount s3://my-bucket gcs://project/bucket local://./data
      nexus-fs mount s3://my-bucket --at /custom/path
      nexus-fs mount s3://my-bucket --json
    """
    from nexus.fs._sync import run_sync

    async def _run() -> dict:
        from nexus.fs import mount

        fs = await mount(*uris, at=at)
        mounts = fs.list_mounts()
        return {"mounts": mounts, "uris": list(uris)}

    try:
        data = run_sync(_run())
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)

    def _human_display(d: dict) -> None:
        for mp in d["mounts"]:
            click.echo(f"  {mp}")
        click.echo(f"Mounted {len(d['mounts'])} backend(s).")

    render_output(
        data=data,
        output_opts=output_opts,
        human_formatter=_human_display,
    )


@main.command()
@click.option(
    "--mount",
    "-m",
    "mount_uris",
    multiple=True,
    help="URI to mount for connectivity checks (e.g., s3://bucket).",
)
@add_output_options
def doctor(mount_uris: tuple[str, ...], output_opts: OutputOptions) -> None:
    """Check environment, backends, and connectivity.

    Runs diagnostic checks across three sections:

    \b
    - Environment: Python version, nexus-fs version, Rust accelerator
    - Backends:    installed packages + credential validation
    - Mounts:      connectivity + latency (when --mount is provided)

    Examples:

    \b
      nexus-fs doctor
      nexus-fs doctor --json
      nexus-fs doctor --mount s3://my-bucket --mount gcs://project/bucket
    """
    from nexus.fs._doctor import DoctorStatus, render_doctor, run_all_checks

    async def _run() -> dict:
        fs = None
        if mount_uris:
            try:
                from nexus.fs import mount

                fs = await mount(*mount_uris)
            except Exception as exc:
                click.echo(f"Warning: unable to mount for connectivity checks: {exc}", err=True)

        return await run_all_checks(fs=fs)

    # Credential validation runs in threads (asyncio.to_thread). If a check
    # hangs (slow DNS, unresponsive metadata service), the thread can't be
    # cancelled. We use shutdown(wait=False, cancel_futures=True) to abandon
    # hung threads so the process exits cleanly after rendering.
    import concurrent.futures

    executor = concurrent.futures.ThreadPoolExecutor(max_workers=8, thread_name_prefix="doctor")
    loop = asyncio.new_event_loop()
    loop.set_default_executor(executor)
    try:
        results = loop.run_until_complete(_run())
    finally:
        executor.shutdown(wait=False, cancel_futures=True)
        loop.close()

    # Serialize DoctorCheckResult dataclasses for JSON output
    serializable = {
        section: [{**asdict(r), "status": r.status.value} for r in checks]
        for section, checks in results.items()
    }

    has_failure = any(
        r.status == DoctorStatus.FAIL for section in results.values() for r in section
    )

    def _human_display(_data: object) -> None:
        render_doctor(results)
        if has_failure:
            sys.exit(1)

    render_output(
        data=serializable,
        output_opts=output_opts,
        human_formatter=_human_display,
    )

    if output_opts.json_output and has_failure:
        sys.exit(1)


@main.command()
@click.argument("uris", nargs=-1)
def playground(uris: tuple[str, ...]) -> None:
    """Interactive TUI file browser.

    Browse files across mounted backends with keyboard navigation,
    file preview, and search.

    Pass backend URIs as arguments, or run without arguments to
    auto-discover existing mounts from the state directory.

    \b
    Keyboard shortcuts:
      arrows  Navigate          Enter  Open/preview
      b       Go back           /      Search
      c       Copy path         p      Preview file
      m       Toggle mounts     q      Quit

    Examples:

    \b
      nexus-fs playground s3://my-bucket local://./data
      nexus-fs playground                              # auto-discover
    """
    try:
        from nexus.fs._tui import PlaygroundApp
    except ImportError:
        click.echo(
            "TUI requires the textual package.\nInstall with: pip install nexus-fs[tui]",
            err=True,
        )
        sys.exit(1)

    app = PlaygroundApp(uris=uris)
    app.run()


@main.command()
@click.argument("source", type=str)
@click.argument("dest", type=str)
@click.argument("mount_uris", nargs=-1)
@add_output_options
def cp(source: str, dest: str, mount_uris: tuple[str, ...], output_opts: OutputOptions) -> None:
    """Copy a file between any mounted backends.

    Uses backend-native server-side copy when source and destination are
    on the same backend (S3 CopyObject, GCS rewrite).  For cross-backend
    copies (e.g., S3 → GCS), streams data in 8 MB chunks without
    buffering the entire file in memory.

    Pass additional URIs to mount backends that aren't auto-discovered.

    \b
    Examples:

    \b
      nexus-fs cp /s3/bucket/data.csv /s3/bucket/backup.csv
      nexus-fs cp /s3/bucket/data.csv /s3/bucket/backup.csv --json
      nexus-fs cp /s3/bucket/file.parquet /gcs/bucket/file.parquet
      nexus-fs cp /local/data/report.pdf /s3/archive/report.pdf s3://archive
    """
    from nexus.fs._sync import run_sync

    async def _run() -> dict:
        import json as json_mod
        import os
        import tempfile

        from nexus.fs import mount

        # Load previously persisted mount URIs from mounts.json
        # (written by mount() on every invocation).
        state_dir = os.environ.get("NEXUS_FS_STATE_DIR") or os.path.join(
            tempfile.gettempdir(), "nexus-fs"
        )
        persisted: list[str] = []
        mounts_file = os.path.join(state_dir, "mounts.json")
        try:
            with open(mounts_file) as f:
                persisted = json_mod.load(f)
        except (OSError, json_mod.JSONDecodeError):
            pass

        # Merge persisted + any extra URIs the user passed explicitly.
        all_uris = list(dict.fromkeys(persisted + list(mount_uris)))
        if not all_uris:
            raise click.UsageError(
                "No mounts found. Run 'nexus-fs mount <uri>' first or "
                "pass backend URIs as trailing arguments:\n"
                "  nexus-fs cp /src /dst s3://bucket gcs://project/bucket"
            )
        fs = await mount(*all_uris)

        result = await fs.copy(source, dest)
        return {"source": source, "dest": dest, **result}

    try:
        data = run_sync(_run())
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)

    def _human_display(d: dict) -> None:
        size = d.get("size", 0)
        if size >= 1024 * 1024:
            size_str = f"{size / (1024 * 1024):.1f} MB"
        elif size >= 1024:
            size_str = f"{size / 1024:.1f} KB"
        else:
            size_str = f"{size} B"
        click.echo(f"Copied {d['source']} → {d['dest']} ({size_str})")

    render_output(
        data=data,
        output_opts=output_opts,
        human_formatter=_human_display,
    )


main.add_command(auth_group)


if __name__ == "__main__":
    main()
