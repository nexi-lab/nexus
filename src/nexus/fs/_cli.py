"""CLI entry point for nexus-fs.

Provides the `nexus-fs` console command with subcommands:
- nexus-fs mount           — register backends (persistent by default)
- nexus-fs mount list      — show persisted mounts (live/stale status)
- nexus-fs mount test      — test backend connectivity without persisting
- nexus-fs mount prune     — remove stale or filtered mount entries
- nexus-fs unmount         — remove a single persisted mount entry

Persistence layer note
----------------------
The *CLI layer* persists mounts in ``mounts.json`` (see nexus.fs._paths).
A *separate* server-layer persistence exists in the Nexus metastore via
``nexus.bricks.mount.MountManager``.  These two layers are intentionally
independent: the CLI does not require a running server, and the server does
not read ``mounts.json``.  All prune/ephemeral features in this file operate
exclusively on ``mounts.json``.
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
from pathlib import Path
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


# ---------------------------------------------------------------------------
# Mount group — proper Click group replaces the old positional-arg dispatch.
# Backwards-compat: ``nexus-fs mount <uri>`` still works via _MountGroup which
# treats unknown first tokens (anything with "://") as ``mount add`` calls.
# ---------------------------------------------------------------------------

_STALE_NAG_THRESHOLD = 50  # warn once after this many total entries


def _is_local_uri_stale(uri: str) -> bool:
    """Return True if a local:// URI points to a path that no longer exists.

    Only absolute paths (after expanduser) are checked — relative paths like
    ``local://./data`` are cwd-sensitive and cannot be reliably evaluated
    after the process that created them has exited.  Such entries are
    conservatively treated as live (returns False) so prune never silently
    removes a mount that might still be valid.
    """
    if not uri.startswith("local://"):
        return False
    path_part = uri[len("local://") :]
    from pathlib import Path

    p = Path(path_part).expanduser()
    if not p.is_absolute():
        # Relative path — cannot safely determine staleness across cwd changes.
        return False
    return not p.exists()


def _check_uri_liveness(uri: str) -> str:
    """Probe a URI and return 'live', 'stale', 'auth-expired', or 'unreachable'.

    For local:// URIs this is a cheap path-existence check.  For cloud URIs
    it attempts to instantiate the backend (network call) — only use from
    ``mount list --check``, never on hot paths.
    """
    if uri.startswith("local://"):
        return "stale" if _is_local_uri_stale(uri) else "live"
    try:
        from nexus.fs._backend_factory import create_backend
        from nexus.fs._uri import parse_uri

        spec = parse_uri(uri)
        backend = create_backend(spec)
        if hasattr(backend, "close"):
            backend.close()
        return "live"
    except Exception as exc:
        err = str(exc).lower()
        if any(
            k in err
            for k in (
                "auth",
                "credential",
                "token",
                "permission",
                "403",
                "401",
                "unauthorized",
                "forbidden",
                "expired",
            )
        ):
            return "auth-expired"
        return "unreachable"


def _check_stale_nag(entries: list[dict]) -> None:
    """Print a one-line nag if the registry is large (count-only, no liveness scan).

    Rate-limited to once per calendar day via a stamp file in the state dir.
    Mirrors the git-gc-auto pattern: count is cheap, liveness scans are not.
    """
    if len(entries) <= _STALE_NAG_THRESHOLD:
        return

    import datetime

    from nexus.fs._paths import state_dir

    stamp = state_dir() / ".prune_nag_stamp"
    today = datetime.date.today().isoformat()
    try:
        if stamp.exists() and stamp.read_text().strip() == today:
            return  # already nagged today
        stamp.write_text(today)
    except OSError:
        pass  # state dir not writable — skip the nag silently

    click.echo(
        f"note: {len(entries)} mount entries in registry. "
        "Run 'nexus-fs mount prune --stale' to clean up.",
        err=True,
    )


def _rotate_backups(state_dir_path: Path, keep: int = 3) -> None:
    """Keep the N most recent mounts.json.bak.* files, delete older ones."""
    baks = sorted(state_dir_path.glob("mounts.json.bak.*"), key=lambda p: p.name)
    for old in baks[:-keep]:
        with contextlib.suppress(OSError):
            old.unlink()


def _write_backup(mounts_file_path: Path) -> bool:
    """Write a timestamped backup of mounts.json, then rotate to keep N=3.

    Microseconds are included in the filename so rapid successive prune calls
    within the same second never overwrite each other's pre-prune snapshots.

    Returns True if the backup was successfully created, False on any OSError.
    Callers that perform destructive operations should treat False as a hard
    stop rather than proceeding without a rollback artifact.
    """
    import datetime
    import shutil

    ts = datetime.datetime.now().strftime("%Y%m%dT%H%M%S%f")
    bak = mounts_file_path.parent / f"mounts.json.bak.{ts}"
    try:
        shutil.copy2(mounts_file_path, bak)
        _rotate_backups(mounts_file_path.parent)
        return True
    except OSError:
        return False


class _MountGroup(click.Group):
    """Click Group that forwards unknown first tokens to ``mount add``.

    This preserves backwards compatibility: ``nexus-fs mount s3://bucket``
    transparently becomes ``nexus-fs mount add s3://bucket``, while the
    proper subcommands (list, test, prune) are dispatched normally.
    """

    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:
        known = set(self.commands)
        # Route to 'add' whenever no known subcommand appears in the token list.
        # This handles both the original token-first form ("mount s3://bucket")
        # AND the option-first legacy form ("mount --at /mp s3://bucket") that
        # old scripts depend on.  Help/version flags are passed through unchanged
        # so the group help text is still reachable.
        if args and args[0] not in ("-h", "--help") and not any(a in known for a in args):
            args = ["add"] + list(args)
        return super().parse_args(ctx, args)


@main.group("mount", cls=_MountGroup, invoke_without_command=True)
@click.pass_context
def mount_group(ctx: click.Context) -> None:
    """Manage persisted mounts.

    \b
    Examples:
      nexus-fs mount s3://my-bucket                  # add (short form)
      nexus-fs mount add s3://my-bucket              # add (explicit)
      nexus-fs mount add gmail gws://gmail           # add with name
      nexus-fs mount list                            # live/stale status
      nexus-fs mount list --all                      # include stale entries
      nexus-fs mount list --check                    # probe cloud URIs too
      nexus-fs mount rm gmail                        # remove by name
      nexus-fs mount rm gws://gmail                  # remove by URI
      nexus-fs mount test s3://my-bucket             # connectivity check, no persist
      nexus-fs mount prune --stale                   # remove dead local:// entries
      nexus-fs mount prune --older-than 30d          # remove entries older than 30 days
      nexus-fs mount prune --filter 'local:///tmp/*' # remove by glob
    """
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


@mount_group.command("add")
@click.argument("args", nargs=-1, required=True, metavar="[NAME] URI [URI ...]")
@click.option("--at", default=None, help="Custom mount point (only valid with a single URI).")
@click.option(
    "--ephemeral",
    is_flag=True,
    default=False,
    help="Mount for this invocation only; do not write to mounts.json.",
)
@add_output_options
def mount_add(
    args: tuple[str, ...], at: str | None, ephemeral: bool, output_opts: OutputOptions
) -> None:
    """Mount one or more backend URIs, optionally with a human name.

    If the first argument does not contain ``://`` it is treated as a name
    (single-URI only).  Named mounts can later be removed with
    ``nexus-fs mount rm NAME``.

    \b
    Examples:
      nexus-fs mount add s3://my-bucket
      nexus-fs mount add s3://my-bucket gcs://project/bucket local://./data
      nexus-fs mount add s3://my-bucket --at /custom/path
      nexus-fs mount add gmail gws://gmail           # named mount
      nexus-fs mount add gws://gmail --ephemeral
    """
    # Split optional leading name from URIs
    if args and "://" not in args[0]:
        name: str | None = args[0]
        uris = args[1:]
        if not uris:
            raise click.UsageError("A URI is required after the name.")
        if len(uris) > 1:
            raise click.UsageError("A name can only be assigned to a single URI.")
    else:
        name = None
        uris = args

    from nexus.fs._sync import run_sync

    async def _run() -> dict:
        from nexus.fs import mount

        fs = await mount(*uris, at=at, ephemeral=ephemeral, name=name)
        mounts = fs.list_mounts()
        return {"mounts": mounts, "uris": list(uris), "name": name}

    try:
        data = run_sync(_run())
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)

    if not ephemeral:
        from nexus.fs._paths import load_persisted_mounts

        _check_stale_nag(load_persisted_mounts())

    def _human_display(d: dict) -> None:
        for mp in d["mounts"]:
            click.echo(f"  {mp}")
        label = f" (name: {d['name']})" if d.get("name") else ""
        click.echo(f"Mounted {len(d['mounts'])} backend(s).{label}")

    render_output(data=data, output_opts=output_opts, human_formatter=_human_display)


@mount_group.command("list")
@click.option(
    "--all",
    "show_all",
    is_flag=True,
    default=False,
    help="Show all entries including stale (default: live only).",
)
@click.option("--stale", "stale_only", is_flag=True, default=False, help="Show only stale entries.")
@click.option(
    "--check",
    is_flag=True,
    default=False,
    help="Probe cloud URIs for auth/connectivity status (slow; makes network calls).",
)
@add_output_options
def mount_list(show_all: bool, stale_only: bool, check: bool, output_opts: OutputOptions) -> None:
    """List persisted mounts with live/stale/auth-expired/unreachable status.

    Without --check, cloud URIs are shown as 'live' (no network call).
    With --check, every URI is probed — 'auth-expired' or 'unreachable'
    is returned for broken cloud backends.

    \b
    Examples:
      nexus-fs mount list
      nexus-fs mount list --all
      nexus-fs mount list --stale
      nexus-fs mount list --check
    """
    from nexus.fs._paths import load_persisted_mounts

    entries = load_persisted_mounts()

    def _status(uri: str) -> str:
        if check:
            return _check_uri_liveness(uri)
        return "stale" if _is_local_uri_stale(uri) else "live"

    all_mounts = [
        {
            "uri": e["uri"],
            "at": e.get("at"),
            "name": e.get("name"),
            "status": _status(e["uri"]),
            "created_at": e.get("created_at"),
        }
        for e in entries
    ]

    if stale_only:
        visible = [m for m in all_mounts if m["status"] == "stale"]
    elif show_all:
        visible = all_mounts
    else:
        visible = [m for m in all_mounts if m["status"] != "stale"]

    stale_count = sum(1 for m in all_mounts if m["status"] == "stale")
    if not show_all and not stale_only and stale_count:
        click.echo(
            f"note: {stale_count} stale entr{'y' if stale_count == 1 else 'ies'} hidden. "
            "Use --all to show or 'mount prune --stale' to remove.",
            err=True,
        )

    data = {"mounts": visible}

    def _human_display(d: dict) -> None:
        mounts = d["mounts"]
        if not mounts:
            click.echo("No persisted mounts.")
            return
        for m in mounts:
            mp = m["at"] or "(default)"
            name_part = f"  @{m['name']}" if m.get("name") else ""
            click.echo(f"{m['uri']} -> {mp} [{m['status']}]{name_part}")
        click.echo(f"Listed {len(mounts)} mount(s).")

    render_output(data=data, output_opts=output_opts, human_formatter=_human_display)


@mount_group.command("rm")
@click.argument("identifier", metavar="NAME_OR_URI")
@add_output_options
def mount_rm(identifier: str, output_opts: OutputOptions) -> None:
    """Remove a persisted mount by name or URI.

    Matches by name first, then by URI.  Use ``nexus-fs unmount URI`` as an
    alias if you prefer the older form.

    \b
    Examples:
      nexus-fs mount rm gmail          # remove the mount named 'gmail'
      nexus-fs mount rm gws://gmail    # remove by URI
    """
    from nexus.fs._paths import load_persisted_mounts, save_persisted_mounts

    entries = load_persisted_mounts()

    # Name match is checked first; fail on ambiguity so two mounts sharing the
    # same label don't silently both disappear.
    by_name = [e for e in entries if e.get("name") == identifier]
    if len(by_name) > 1:
        uris = ", ".join(e["uri"] for e in by_name)
        click.echo(
            f"Error: {len(by_name)} mounts share the name {identifier!r} ({uris}). "
            "Use the URI to disambiguate.",
            err=True,
        )
        sys.exit(1)

    remaining = [e for e in entries if e.get("name") != identifier and e["uri"] != identifier]
    removed = len(entries) - len(remaining)

    if removed == 0:
        click.echo(f"Error: no mount found matching {identifier!r}", err=True)
        sys.exit(1)

    save_persisted_mounts(remaining, merge=False)
    data = {"identifier": identifier, "removed": removed}

    def _human_display(d: dict) -> None:
        click.echo(f"Removed {d['removed']} mount(s) matching {d['identifier']!r}.")

    render_output(data=data, output_opts=output_opts, human_formatter=_human_display)


@mount_group.command("test")
@click.argument("uris", nargs=-1, required=True)
@add_output_options
def mount_test(uris: tuple[str, ...], output_opts: OutputOptions) -> None:
    """Test backend connectivity without persisting mount state.

    \b
    Examples:
      nexus-fs mount test s3://my-bucket
      nexus-fs mount test s3://my-bucket gcs://project/bucket
    """
    from nexus.fs._doctor import DoctorStatus, render_doctor, run_all_checks
    from nexus.fs._sync import run_sync

    async def _run() -> dict[str, list[dict[str, str | float | None]]]:
        from nexus.fs import mount

        # Use ephemeral=True so mounts.json is never touched — no snapshot/restore
        # gymnastics needed, and concurrent mount operations on the shared registry
        # are never lost.
        fs = await mount(*uris, ephemeral=True)
        results = await run_all_checks(fs=fs)

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

    render_output(data=serializable, output_opts=output_opts, human_formatter=_human_display)

    if output_opts.json_output and has_failure:
        sys.exit(1)


def _parse_older_than(value: str) -> Any:
    """Parse a duration string like '30d', '24h', '60m' into a timedelta."""
    import re
    from datetime import timedelta

    m = re.fullmatch(r"(\d+)([dhm])", value.strip())
    if not m:
        raise click.BadParameter(
            f"Invalid duration {value!r}. Use e.g. '30d' (days), '24h' (hours), '60m' (minutes)."
        )
    n, unit = int(m.group(1)), m.group(2)
    if unit == "d":
        return timedelta(days=n)
    if unit == "h":
        return timedelta(hours=n)
    return timedelta(minutes=n)


@mount_group.command("prune")
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Show what would be removed without making changes.",
)
@click.option(
    "--stale",
    "prune_stale",
    is_flag=True,
    default=False,
    help="Remove entries whose local:// path no longer exists.",
)
@click.option(
    "--filter",
    "uri_filter",
    default=None,
    metavar="GLOB",
    help="Remove entries whose URI matches this glob (e.g. 'local:///tmp/*test*').",
)
@click.option(
    "--older-than",
    "older_than",
    default=None,
    metavar="DURATION",
    help="Remove entries older than DURATION (e.g. '30d', '24h', '60m'). "
    "Entries without a created_at timestamp are kept.",
)
@click.option("--yes", "-y", is_flag=True, default=False, help="Skip confirmation prompt.")
def mount_prune(
    dry_run: bool,
    prune_stale: bool,
    uri_filter: str | None,
    older_than: str | None,
    yes: bool,
) -> None:
    """Remove stale, old, or matching mount entries from mounts.json.

    A timestamped backup is written before any modification.
    At most 3 backups are kept; older ones are automatically deleted.
    Entries without a ``created_at`` timestamp are never removed by --older-than.

    \b
    Examples:
      nexus-fs mount prune --stale                      # remove dead local:// paths
      nexus-fs mount prune --older-than 30d             # remove entries > 30 days old
      nexus-fs mount prune --filter 'local:///tmp/*'    # remove by glob
      nexus-fs mount prune --stale --dry-run            # preview only
      nexus-fs mount prune --stale --yes                # no confirmation prompt
    """
    import datetime
    import fnmatch

    from nexus.fs._paths import load_persisted_mounts, mounts_file, save_persisted_mounts

    if not prune_stale and uri_filter is None and older_than is None:
        raise click.UsageError("Specify at least one of --stale, --filter, or --older-than.")

    # Parse --older-than once up front so bad input fails before loading state
    cutoff: datetime.datetime | None = None
    if older_than is not None:
        delta = _parse_older_than(older_than)
        cutoff = datetime.datetime.now(datetime.UTC) - delta

    entries = load_persisted_mounts()
    if not entries:
        click.echo("No persisted mounts — nothing to prune.")
        return

    def _should_remove(entry: dict) -> bool:
        uri = entry["uri"]
        if prune_stale and _is_local_uri_stale(uri):
            return True
        if uri_filter and fnmatch.fnmatch(uri, uri_filter):
            return True
        if cutoff is not None:
            created_raw = entry.get("created_at")
            if created_raw:
                try:
                    created = datetime.datetime.fromisoformat(created_raw)
                    if created < cutoff:
                        return True
                except ValueError:
                    pass  # malformed timestamp — keep the entry (conservative)
        return False

    to_remove = [e for e in entries if _should_remove(e)]
    to_keep = [e for e in entries if not _should_remove(e)]

    if not to_remove:
        click.echo("No entries matched — nothing to prune.")
        return

    click.echo(f"Would remove {len(to_remove)} entr{'y' if len(to_remove) == 1 else 'ies'}:")
    for e in to_remove:
        click.echo(f"  - {e['uri']}")

    if dry_run:
        click.echo("(dry-run — no changes made)")
        return

    if not yes:
        click.confirm(
            f"Remove {len(to_remove)} entr{'y' if len(to_remove) == 1 else 'ies'}?", abort=True
        )

    mf = mounts_file()
    if mf.exists() and not _write_backup(mf):
        click.echo(
            "Error: could not create a backup of mounts.json before pruning. "
            "Aborting to protect your mount configuration. "
            "Free disk space and retry, or use --dry-run to inspect entries.",
            err=True,
        )
        sys.exit(1)

    save_persisted_mounts(to_keep, merge=False)
    click.echo(
        f"Pruned {len(to_remove)} entr{'y' if len(to_remove) == 1 else 'ies'}. {len(to_keep)} remaining."
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
        entries = fs.ls(path, detail=detail, recursive=recursive)
        fs.close()
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
        fs.close()
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
        fs.close()
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
        fs.close()
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
        fs.delete(path)
        fs.close()
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
        fs.close()
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
        info: dict[str, Any] | None = fs.stat(path)
        fs.close()
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
        matches = fs.grep(
            pattern,
            path,
            ignore_case=ignore_case,
            max_results=max_results,
        )
        fs.close()
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
        matches = fs.glob(pattern, path)
        fs.close()
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
