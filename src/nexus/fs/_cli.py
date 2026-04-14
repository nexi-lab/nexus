"""CLI entry point for nexus-fs.

Provides the `nexus-fs` console command with subcommands:
- nexus-fs mount       — register backends for later use
- nexus-fs mount list  — show persisted mounts
- nexus-fs mount test  — test backend connectivity without persisting
- nexus-fs unmount     — remove persisted mounts
- nexus-fs ls          — list directory contents
- nexus-fs cat         — read file contents to stdout
- nexus-fs write       — write content to a file
- nexus-fs edit        — surgical search/replace edit
- nexus-fs rm          — delete a file
- nexus-fs mkdir       — create a directory
- nexus-fs stat        — show file/directory metadata
- nexus-fs cp          — copy files between mounted backends
- nexus-fs grep        — search file contents for a regex pattern
- nexus-fs glob        — find files matching a glob pattern
- nexus-fs doctor      — diagnostic checks (environment, backends, mounts)
- nexus-fs playground  — interactive TUI file browser
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
from typing import Any

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

        fs = await mount(
            *uris,
            mount_overrides=overrides or None,
            skip_unavailable=True,
        )

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


def _boot_fs() -> Any:
    """Boot a SlimNexusFS from persisted mounts only.

    Shared by ls, cat, write, edit, rm, mkdir, stat.
    Uses only previously persisted mounts — does not accept ad-hoc URIs
    to avoid mutating global mount state as a side effect of one-shot
    commands.  Users should run ``nexus-fs mount <uri>`` first.
    """

    async def _run() -> Any:
        from nexus.fs import mount
        from nexus.fs._paths import build_mount_args, load_persisted_mounts

        persisted = load_persisted_mounts()
        uris, overrides = build_mount_args(persisted)
        if not uris:
            raise click.UsageError("No mounts found. Run 'nexus-fs mount <uri>' first.")
        return await mount(
            *uris,
            mount_overrides=overrides or None,
            skip_unavailable=True,
        )

    return _run()


# ── Basic filesystem primitives ──────────────────────────────────────────────


@main.command()
@click.argument("path", default="/")
@click.option("-l", "--long", "detail", is_flag=True, help="Show detailed metadata.")
@click.option("-r", "--recursive", is_flag=True, help="List recursively.")
@add_output_options
def ls(
    path: str,
    detail: bool,
    recursive: bool,
    output_opts: OutputOptions,
) -> None:
    """List directory contents.

    \b
    Examples:
      nexus-fs ls /s3/my-bucket/
      nexus-fs ls /local/data/ -l
      nexus-fs ls /s3/my-bucket/ -r
    """
    from nexus.fs._sync import run_sync

    async def _run() -> dict:
        fs = await _boot_fs()
        entries = await fs.ls(path, detail=detail, recursive=recursive)
        await fs.close()
        return {"path": path, "entries": entries}

    try:
        data = run_sync(_run())
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)

    def _human_display(d: dict) -> None:
        for entry in d["entries"]:
            if isinstance(entry, dict):
                size = entry.get("size", 0)
                is_dir = entry.get("is_directory", False)
                kind = "d" if is_dir else "-"
                name = entry.get("path", entry.get("name", "?"))
                click.echo(f"{kind} {size:>10}  {name}")
            else:
                click.echo(entry)

    render_output(data=data, output_opts=output_opts, human_formatter=_human_display)


@main.command()
@click.argument("path", type=str)
def cat(path: str) -> None:
    """Read file contents to stdout.

    \b
    Examples:
      nexus-fs cat /s3/my-bucket/README.md
      nexus-fs cat /local/data/config.json
    """
    from nexus.fs._sync import run_sync

    async def _run() -> bytes:
        fs = await _boot_fs()
        content: bytes = fs.read(path)
        await fs.close()
        return content

    try:
        content = run_sync(_run())
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)

    # Write raw bytes to stdout (handles binary gracefully)
    sys.stdout.buffer.write(content)


@main.command()
@click.argument("path", type=str)
@click.option(
    "--data",
    "-d",
    type=str,
    default=None,
    help="Content string to write. If omitted, reads from stdin.",
)
@click.option(
    "--allow-empty",
    is_flag=True,
    default=False,
    help="Allow writing empty content (e.g., truncating a file to zero bytes).",
)
@add_output_options
def write(
    path: str,
    data: str | None,
    allow_empty: bool,
    output_opts: OutputOptions,
) -> None:
    """Write content to a file (creates or overwrites).

    Content can come from --data or stdin (piped).

    \b
    Examples:
      nexus-fs write /local/data/hello.txt -d "Hello, world!"
      echo "piped content" | nexus-fs write /s3/bucket/file.txt
      nexus-fs write /local/data/config.json -d '{"key": "value"}'
    """
    from nexus.fs._sync import run_sync

    if data is not None:
        content = data.encode("utf-8")
    elif not sys.stdin.isatty():
        content = sys.stdin.buffer.read()
    else:
        click.echo("Error: provide --data or pipe content via stdin.", err=True)
        sys.exit(1)

    if not content and not allow_empty:
        click.echo(
            "Error: refusing to write empty content (would truncate file). "
            "Use --allow-empty to override.",
            err=True,
        )
        sys.exit(1)

    async def _run() -> dict:
        fs = await _boot_fs()
        result = fs.write(path, content)
        await fs.close()
        return {"path": path, **result}

    try:
        result_data = run_sync(_run())
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)

    def _human_display(d: dict) -> None:
        size = d.get("size", len(content))
        click.echo(f"Wrote {size} bytes to {d['path']}")

    render_output(data=result_data, output_opts=output_opts, human_formatter=_human_display)


@main.command()
@click.argument("path", type=str)
@click.option(
    "-e",
    "--edit-spec",
    "edits",
    multiple=True,
    required=True,
    help="Edit spec as 'old_str>>>new_str'. Repeat for multiple edits.",
)
@click.option("--preview", is_flag=True, help="Show diff without writing.")
@click.option(
    "--fuzzy",
    type=float,
    default=1.0,
    help="Fuzzy match threshold (0.0-1.0). Default 1.0 (exact only). Use 0.85 for typo tolerance.",
)
@add_output_options
def edit(
    path: str,
    edits: tuple[str, ...],
    preview: bool,
    fuzzy: float,
    output_opts: OutputOptions,
) -> None:
    """Surgical search/replace edit on a file.

    Each -e flag takes 'old_str>>>new_str' (triple angle bracket separator).

    \b
    Examples:
      nexus-fs edit /local/src/main.py -e 'def foo():>>>def bar():'
      nexus-fs edit /local/src/main.py -e 'old>>>new' --preview
      nexus-fs edit /local/src/main.py -e 'typo>>>fix' --fuzzy 0.8
    """
    from nexus.fs._sync import run_sync

    parsed_edits = []
    for spec in edits:
        if ">>>" not in spec:
            click.echo(f"Error: invalid edit spec (missing '>>>'): {spec}", err=True)
            sys.exit(1)
        old, new = spec.split(">>>", 1)
        parsed_edits.append({"old_str": old, "new_str": new})

    async def _run() -> dict:
        fs = await _boot_fs()
        result = fs.edit(
            path,
            parsed_edits,
            preview=preview,
            fuzzy_threshold=fuzzy,
        )
        await fs.close()
        # Strip new_content from result to prevent leaking full file body
        # into JSON output (auto-JSON in piped/CI contexts).
        result.pop("new_content", None)
        return {"path": path, **result}

    try:
        result_data = run_sync(_run())
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)

    # Check success before rendering — ensures non-zero exit in ALL output
    # modes (human, JSON, auto-JSON) so CI/agents don't treat failures as ok.
    edit_failed = not result_data.get("success", True)

    def _human_display(d: dict) -> None:
        if d.get("success"):
            status = "preview" if preview else "applied"
            click.echo(f"Edit {status}: {d.get('applied_count', 0)} replacement(s)")
            if d.get("diff"):
                click.echo(d["diff"])
        else:
            click.echo("Edit failed:", err=True)
            for err in d.get("errors", []):
                click.echo(f"  {err}", err=True)

    render_output(data=result_data, output_opts=output_opts, human_formatter=_human_display)

    if edit_failed:
        sys.exit(1)


@main.command()
@click.argument("path", type=str)
@add_output_options
def rm(path: str, output_opts: OutputOptions) -> None:
    """Delete a file.

    \b
    Examples:
      nexus-fs rm /s3/my-bucket/old-file.txt
      nexus-fs rm /local/data/temp.csv
    """
    from nexus.fs._sync import run_sync

    async def _run() -> dict:
        fs = await _boot_fs()
        await fs.delete(path)
        await fs.close()
        return {"path": path, "deleted": True}

    try:
        data = run_sync(_run())
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)

    def _human_display(d: dict) -> None:
        click.echo(f"Deleted {d['path']}")

    render_output(data=data, output_opts=output_opts, human_formatter=_human_display)


@main.command()
@click.argument("path", type=str)
@click.option("-p", "--parents", is_flag=True, default=True, help="Create parent dirs.")
@add_output_options
def mkdir(
    path: str,
    parents: bool,
    output_opts: OutputOptions,
) -> None:
    """Create a directory.

    \b
    Examples:
      nexus-fs mkdir /local/data/new-dir
      nexus-fs mkdir /s3/bucket/path/to/dir
    """
    from nexus.fs._sync import run_sync

    async def _run() -> dict:
        fs = await _boot_fs()
        fs.mkdir(path, parents=parents)
        await fs.close()
        return {"path": path, "created": True}

    try:
        data = run_sync(_run())
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)

    def _human_display(d: dict) -> None:
        click.echo(f"Created {d['path']}")

    render_output(data=data, output_opts=output_opts, human_formatter=_human_display)


@main.command()
@click.argument("path", type=str)
@add_output_options
def stat(path: str, output_opts: OutputOptions) -> None:
    """Show file or directory metadata.

    \b
    Examples:
      nexus-fs stat /s3/my-bucket/file.txt
      nexus-fs stat /local/data/ --json
    """
    from nexus.fs._sync import run_sync

    async def _run() -> dict[str, Any]:
        fs = await _boot_fs()
        info: dict[str, Any] | None = await fs.stat(path)
        await fs.close()
        if info is None:
            raise FileNotFoundError(f"Not found: {path}")
        return info

    try:
        data = run_sync(_run())
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)

    def _human_display(d: dict) -> None:
        click.echo(f"  Path:     {d.get('path')}")
        click.echo(f"  Size:     {d.get('size', 0)}")
        click.echo(f"  Type:     {'directory' if d.get('is_directory') else 'file'}")
        click.echo(f"  MIME:     {d.get('mime_type', '?')}")
        click.echo(f"  ETag:     {d.get('etag', '?')}")
        click.echo(f"  Version:  {d.get('version', '?')}")
        if d.get("created_at"):
            click.echo(f"  Created:  {d['created_at']}")
        if d.get("modified_at"):
            click.echo(f"  Modified: {d['modified_at']}")

    render_output(data=data, output_opts=output_opts, human_formatter=_human_display)


@main.command()
@click.argument("pattern", type=str)
@click.argument("path", default="/")
@click.option("-i", "--ignore-case", is_flag=True, help="Case-insensitive matching.")
@click.option("-n", "--max-results", type=int, default=100, help="Max matches to return.")
@add_output_options
def grep(
    pattern: str,
    path: str,
    ignore_case: bool,
    max_results: int,
    output_opts: OutputOptions,
) -> None:
    """Search file contents for a regex pattern.

    Recursively searches files under PATH for lines matching PATTERN.
    Uses Rust-accelerated regex when available.

    \b
    Examples:
      nexus-fs grep "TODO" /local/src/
      nexus-fs grep "def .*test" /s3/bucket/code/ -i
      nexus-fs grep "error" / -n 50
      nexus-fs grep "import os" /local/project/ --json
    """
    from nexus.fs._sync import run_sync

    async def _run() -> dict:
        fs = await _boot_fs()
        matches = await fs.grep(
            pattern,
            path,
            ignore_case=ignore_case,
            max_results=max_results,
        )
        await fs.close()
        return {"pattern": pattern, "path": path, "matches": matches, "count": len(matches)}

    try:
        data = run_sync(_run())
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)

    def _human_display(d: dict) -> None:
        for m in d["matches"]:
            click.echo(f"{m['file']}:{m['line']}: {m['content']}")
        click.echo(f"\n{d['count']} match(es) found.")

    render_output(data=data, output_opts=output_opts, human_formatter=_human_display)


@main.command("glob")
@click.argument("pattern", type=str)
@click.argument("path", default="/")
@add_output_options
def glob_cmd(
    pattern: str,
    path: str,
    output_opts: OutputOptions,
) -> None:
    """Find files matching a glob pattern.

    Recursively lists files under PATH and filters by PATTERN.
    Uses Rust-accelerated glob matching when available.

    \b
    Examples:
      nexus-fs glob "**/*.py" /local/src/
      nexus-fs glob "*.csv" /s3/bucket/data/
      nexus-fs glob "**/*.json" / --json
    """
    from nexus.fs._sync import run_sync

    async def _run() -> dict:
        fs = await _boot_fs()
        matches = await fs.glob(pattern, path)
        await fs.close()
        return {"pattern": pattern, "path": path, "matches": matches, "count": len(matches)}

    try:
        data = run_sync(_run())
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)

    def _human_display(d: dict) -> None:
        for m in d["matches"]:
            click.echo(m)
        click.echo(f"\n{d['count']} file(s) matched.")

    render_output(data=data, output_opts=output_opts, human_formatter=_human_display)


main.add_command(auth_group)


if __name__ == "__main__":
    main()
