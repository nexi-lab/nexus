"""CLI entry point for nexus-fs.

Provides the `nexus-fs` console command with subcommands:
- nexus-fs mount       — register backends for later use
- nexus-fs mount list  — show persisted mounts
- nexus-fs mount test  — test backend connectivity without persisting
- nexus-fs unmount     — remove persisted mounts
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
import contextlib
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


def _mount_list(output_opts: OutputOptions) -> None:
    """List persisted mounts from mounts.json."""
    from nexus.fs._paths import load_persisted_mounts

    entries = load_persisted_mounts()
    data = {
        "mounts": [
            {"uri": entry["uri"], "at": entry.get("at"), "status": "persisted"} for entry in entries
        ]
    }

    def _human_display(d: dict) -> None:
        mounts = d["mounts"]
        if not mounts:
            click.echo("No persisted mounts.")
            return
        for mount in mounts:
            mount_point = mount["at"] or "(default)"
            click.echo(f"{mount['uri']} -> {mount_point} [{mount['status']}]")
        click.echo(f"Listed {len(mounts)} persisted mount(s).")

    render_output(
        data=data,
        output_opts=output_opts,
        human_formatter=_human_display,
    )


def _mount_test(uris: tuple[str, ...], output_opts: OutputOptions) -> None:
    """Test backend connectivity without persisting mount state."""
    from nexus.fs._doctor import DoctorStatus, render_doctor, run_all_checks
    from nexus.fs._paths import mounts_file
    from nexus.fs._sync import run_sync

    async def _run() -> dict[str, list[dict[str, str | float | None]]]:
        from nexus.fs import mount
        from nexus.fs._paths import load_persisted_mounts, save_persisted_mounts

        mf = mounts_file()
        had_mounts_file = mf.exists()
        previous_entries = load_persisted_mounts() if had_mounts_file else []
        try:
            fs = await mount(*uris)
            results = await run_all_checks(fs=fs)
        finally:
            if had_mounts_file:
                save_persisted_mounts(previous_entries, merge=False)
            else:
                with contextlib.suppress(OSError):
                    mf.unlink()

        return {
            section: [{**asdict(r), "status": r.status.value} for r in checks]
            for section, checks in results.items()
        }

    try:
        serializable = run_sync(_run())
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)

    has_failure = any(
        result["status"] == DoctorStatus.FAIL.value for result in serializable.get("Mounts", [])
    )

    def _human_display(data: dict[str, list[dict[str, str | float | None]]]) -> None:
        from nexus.fs._doctor import DoctorCheckResult

        def _string_field(item: dict[str, str | float | None], key: str) -> str | None:
            value = item.get(key)
            return value if isinstance(value, str) or value is None else str(value)

        def _latency_field(item: dict[str, str | float | None]) -> float | None:
            value = item.get("latency_ms")
            return float(value) if isinstance(value, (str, float, int)) else None

        results = {
            section: [
                DoctorCheckResult(
                    name=str(item["name"]),
                    status=DoctorStatus(str(item["status"])),
                    message=str(item["message"]),
                    fix_hint=_string_field(item, "fix_hint"),
                    latency_ms=_latency_field(item),
                    install_cmd=_string_field(item, "install_cmd"),
                )
                for item in checks
            ]
            for section, checks in data.items()
        }
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


@main.command("mount")
@click.argument("uris", nargs=-1, required=True)
@click.option(
    "--at",
    default=None,
    help="Custom mount point (only valid with a single URI).",
)
@add_output_options
def mount_cmd(uris: tuple[str, ...], at: str | None, output_opts: OutputOptions) -> None:
    """Manage persisted mounts and test backend connectivity.

    The default form mounts the given backend URIs and persists them so that
    subsequent commands (cp, playground) can auto-discover them without
    needing URIs again. ``list`` and ``test`` are accepted as the first
    positional token to provide a subcommand-style workflow:

    \b
    Examples:

    \b
      nexus-fs mount s3://my-bucket
      nexus-fs mount s3://my-bucket gcs://project/bucket local://./data
      nexus-fs mount s3://my-bucket --at /custom/path
      nexus-fs mount list
      nexus-fs mount test s3://my-bucket
      nexus-fs mount s3://my-bucket --json
    """
    if uris[0] == "list":
        if len(uris) != 1 or at is not None:
            raise click.UsageError("Usage: nexus-fs mount list")
        _mount_list(output_opts)
        return

    if uris[0] == "test":
        if at is not None:
            raise click.UsageError("'--at' is not supported with 'nexus-fs mount test'")
        if len(uris) == 1:
            raise click.UsageError("Usage: nexus-fs mount test <uri> [<uri> ...]")
        _mount_test(uris[1:], output_opts)
        return

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


@main.command("unmount")
@click.argument("uri", type=str)
@add_output_options
def unmount_cmd(uri: str, output_opts: OutputOptions) -> None:
    """Remove a persisted mount entry from mounts.json."""
    from nexus.fs._paths import load_persisted_mounts, save_persisted_mounts

    entries = load_persisted_mounts()
    remaining = [entry for entry in entries if entry["uri"] != uri]
    removed = len(entries) - len(remaining)

    if removed == 0:
        click.echo(f"Error: mount not found: {uri}", err=True)
        sys.exit(1)

    save_persisted_mounts(remaining, merge=False)
    data = {"uri": uri, "removed": removed}

    def _human_display(d: dict) -> None:
        click.echo(f"Removed persisted mount: {d['uri']}")

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
        from nexus.fs import mount
        from nexus.fs._paths import build_mount_args, load_persisted_mounts

        # Load previously persisted mount entries from mounts.json.
        persisted = load_persisted_mounts()
        uris, overrides = build_mount_args(persisted)

        # Append extra URIs from the command line.
        for uri in mount_uris:
            if uri not in uris:
                uris.append(uri)

        if not uris:
            raise click.UsageError(
                "No mounts found. Run 'nexus-fs mount <uri>' first or "
                "pass backend URIs as trailing arguments:\n"
                "  nexus-fs cp /src /dst s3://bucket gcs://project/bucket"
            )

        fs = await mount(*uris, mount_overrides=overrides or None)

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
