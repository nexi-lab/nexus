"""Nexus CLI Main Entry Point.

This module provides the main CLI entry point for the Nexus command-line tool.
It creates the main command group and registers all commands from the modular structure.

When invoked with no subcommand, ``nexus`` execs into the TUI (``nexus-tui``).
"""

import os
import shutil
import signal
import subprocess
import sys
import warnings
from pathlib import Path

import click

import nexus
from nexus.cli.commands import LazyCommandGroup, register_all_commands
from nexus.core import setup_uvloop

# ---------------------------------------------------------------------------
# TUI launcher helpers
# ---------------------------------------------------------------------------


def _exec_terminal(binary: str, arguments: list[str]) -> None:
    if sys.platform == "win32":
        try:
            code = subprocess.call([binary, *arguments[1:]])
        except KeyboardInterrupt:
            raise SystemExit(130) from None
        raise SystemExit(code)
    os.execvp(binary, arguments)


def _exec_tui(extra_args: list[str] | None = None) -> None:
    """Replace the current process with the TUI.

    Order of preference:
      1. repo-local ``packages/nexus-tui/src/index.tsx`` via ``bun run``
      2. ``nexus-tui`` already on PATH
      3. published ``@nexus-ai-fs/tui`` via ``bunx``
    """
    args = extra_args or []

    # Repo-local dev path: walk up from CWD looking for a runnable TS workspace.
    # The TUI depends on:
    #   - the sibling api-client package having been built, and
    #   - the TUI workspace itself having been installed (`bun install`)
    # In a partial setup, fall back to the installed binary or bunx.
    bun = shutil.which("bun")
    if bun is not None:
        cwd = Path.cwd()
        for candidate_root in (cwd, *cwd.parents):
            local_entry = candidate_root / "packages" / "nexus-tui" / "src" / "index.tsx"
            api_client_dist = candidate_root / "packages" / "nexus-api-client" / "dist" / "index.js"
            tui_workspace_dep = (
                candidate_root
                / "packages"
                / "nexus-tui"
                / "node_modules"
                / "@nexus-ai-fs"
                / "api-client"
                / "package.json"
            )
            if local_entry.exists() and api_client_dist.exists() and tui_workspace_dep.exists():
                _exec_terminal(
                    bun,
                    ["bun", "run", "--cwd", str(local_entry.parents[1]), "start", *args],
                )
                # execvp does not return

    # Fast path outside a checkout: use installed nexus-tui on PATH.
    nexus_tui = shutil.which("nexus-tui")
    if nexus_tui is not None:
        _exec_terminal(nexus_tui, ["nexus-tui", *args])
        # execvp does not return

    # Fallback: use bunx (Bun's npx equivalent) against the scoped package.
    bunx = shutil.which("bunx")
    if bunx is not None:
        _exec_terminal(bunx, ["bunx", "@nexus-ai-fs/tui", *args])
        # execvp does not return

    # Neither found – give the user actionable guidance.
    click.secho("Error: could not find nexus-tui, bun, or bunx on PATH.", fg="red", err=True)
    click.echo(
        "Install the TUI with:\n  bunx @nexus-ai-fs/tui   # or\n  bun install -g @nexus-ai-fs/tui\n",
        err=True,
    )
    sys.exit(1)


# ---------------------------------------------------------------------------
# Main CLI group
# ---------------------------------------------------------------------------


@click.group(cls=LazyCommandGroup, invoke_without_command=True)
@click.version_option(version=nexus.__version__, prog_name="nexus")
@click.option(
    "--profile",
    type=str,
    default=None,
    help="Use named connection profile from ~/.nexus/config.yaml.",
)
@click.pass_context
def main(ctx: click.Context, profile: str | None) -> None:
    """Nexus - filesystem/context plane.

    File operations, Search, native credentials and permissions.

    When invoked without a subcommand the interactive TUI is launched.

    Examples:
        # Launch the TUI
        nexus

        # Initialize a workspace
        nexus init ./my-workspace

        # Write and read files
        nexus write /file.txt "Hello World"
        nexus cat /file.txt

        # List files
        nexus ls /workspace --long

        # Search for files
        nexus grep "TODO" --path /workspace
        nexus glob "**/*.py"

        # Inspect the native identity and permissions
        nexus identity whoami
        nexus rebac list --zone-id root

        # Profile management
        nexus profile list
        nexus --profile staging ls /

    For more information on specific commands, use:
        nexus <command> --help
    """
    ctx.ensure_object(dict)
    ctx.obj["profile"] = profile

    # When no subcommand is supplied, launch the TUI.
    if ctx.invoked_subcommand is None:
        _exec_tui()


# ---------------------------------------------------------------------------
# Explicit ``nexus tui`` subcommand (convenience / backward-compat alias)
# ---------------------------------------------------------------------------


@main.command("tui")
@click.option("--url", default=None, help="Nexus server URL.")
@click.option("--search-url", default=None, help="Owning Rust Search HTTP URL.")
@click.option("--api-key", default=None, help="API key for authentication.")
@click.option("--agent-id", default=None, help="Agent ID to connect as.")
@click.option("--subject", default=None, help="Subject (user) identity.")
@click.option("--zone-id", default=None, help="Zone ID to target.")
def tui_cmd(
    url: str | None,
    search_url: str | None,
    api_key: str | None,
    agent_id: str | None,
    subject: str | None,
    zone_id: str | None,
) -> None:
    """Launch the Nexus interactive TUI."""
    args: list[str] = []
    if url is not None:
        args.extend(["--url", url])
    if search_url is not None:
        args.extend(["--search-url", search_url])
    if api_key is not None:
        args.extend(["--api-key", api_key])
    if agent_id is not None:
        args.extend(["--agent-id", agent_id])
    if subject is not None:
        args.extend(["--subject", subject])
    if zone_id is not None:
        args.extend(["--zone-id", zone_id])
    _exec_tui(args)


# Register all commands from the modular structure
register_all_commands(main)


def entrypoint() -> None:
    """Configure the CLI process and invoke the command group."""
    warnings.filterwarnings(
        "ignore", message="Couldn't find ffmpeg or avconv", category=RuntimeWarning
    )
    if hasattr(signal, "SIGPIPE"):
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    setup_uvloop()
    main()


if __name__ == "__main__":
    entrypoint()
