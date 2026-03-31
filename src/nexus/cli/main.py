"""Nexus CLI Main Entry Point.

This module provides the main CLI entry point for the Nexus command-line tool.
It creates the main command group and registers all commands from the modular structure.

When invoked with no subcommand, ``nexus`` execs into the TUI (``nexus-tui``).
"""

import os
import shutil
import sys
import warnings
from pathlib import Path

import click

import nexus
from nexus.cli.commands import LazyCommandGroup, register_all_commands
from nexus.core import setup_uvloop

# Suppress pydub warning about missing ffmpeg/avconv
warnings.filterwarnings("ignore", message="Couldn't find ffmpeg or avconv", category=RuntimeWarning)

# Install uvloop early for better async performance in all CLI commands
# This affects all asyncio.run() calls throughout the CLI
# Can be disabled with NEXUS_USE_UVLOOP=false
setup_uvloop()


# ---------------------------------------------------------------------------
# TUI launcher helpers
# ---------------------------------------------------------------------------


def _exec_tui(extra_args: list[str] | None = None) -> None:
    """Replace the current process with the TUI.

    Order of preference:
      1. repo-local ``packages/nexus-tui/src/index.tsx`` via ``bun run``
      2. ``nexus-tui`` already on PATH
      3. published ``@nexus/tui`` via ``bunx``
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
                / "@nexus"
                / "api-client"
                / "package.json"
            )
            if local_entry.exists() and api_client_dist.exists() and tui_workspace_dep.exists():
                os.execvp(bun, ["bun", "run", str(local_entry), *args])
                # execvp does not return

    # Fast path outside a checkout: use installed nexus-tui on PATH.
    nexus_tui = shutil.which("nexus-tui")
    if nexus_tui is not None:
        os.execvp(nexus_tui, ["nexus-tui", *args])
        # execvp does not return

    # Fallback: use bunx (Bun's npx equivalent) against the scoped package.
    bunx = shutil.which("bunx")
    if bunx is not None:
        os.execvp(bunx, ["bunx", "@nexus/tui", *args])
        # execvp does not return

    # Neither found – give the user actionable guidance.
    click.secho("Error: could not find nexus-tui, bun, or bunx on PATH.", fg="red", err=True)
    click.echo(
        "Install the TUI with:\n  bunx @nexus/tui   # or\n  bun install -g @nexus/tui\n",
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

    Beautiful command-line interface for file operations, discovery, and management.

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

        # Manage permissions
        nexus chmod 644 /file.txt
        nexus chown alice /file.txt

        # Version tracking
        nexus versions history /file.txt
        nexus versions rollback /file.txt 1

        # Server and mounting
        nexusd --host 0.0.0.0 --port 2026
        nexus mount /mnt/nexus

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
@click.option("--api-key", default=None, help="API key for authentication.")
@click.option("--agent-id", default=None, help="Agent ID to connect as.")
@click.option("--subject", default=None, help="Subject (user) identity.")
@click.option("--zone-id", default=None, help="Zone ID to target.")
def tui_cmd(
    url: str | None,
    api_key: str | None,
    agent_id: str | None,
    subject: str | None,
    zone_id: str | None,
) -> None:
    """Launch the Nexus interactive TUI."""
    args: list[str] = []
    if url is not None:
        args.extend(["--url", url])
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

# For backwards compatibility and direct execution
if __name__ == "__main__":
    main()
