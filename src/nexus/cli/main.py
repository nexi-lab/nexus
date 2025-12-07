"""Nexus CLI Main Entry Point.

This module provides the main CLI entry point for the Nexus command-line tool.
It creates the main command group and registers all commands from the modular structure.
"""

from __future__ import annotations

import warnings

import click

import nexus
from nexus.cli.commands import register_all_commands
from nexus.core import setup_uvloop

# Suppress pydub warning about missing ffmpeg/avconv
warnings.filterwarnings("ignore", message="Couldn't find ffmpeg or avconv", category=RuntimeWarning)

# Install uvloop early for better async performance in all CLI commands
# This affects all asyncio.run() calls throughout the CLI
# Can be disabled with NEXUS_USE_UVLOOP=false
setup_uvloop()


@click.group()
@click.version_option(version=nexus.__version__, prog_name="nexus")
def main() -> None:
    """Nexus - AI-Native Distributed Filesystem.

    Beautiful command-line interface for file operations, discovery, and management.

    Examples:
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
        nexus serve --host 0.0.0.0 --port 8080
        nexus mount /mnt/nexus

    For more information on specific commands, use:
        nexus <command> --help
    """
    pass


# Register all commands from the modular structure
register_all_commands(main)


# For backwards compatibility and direct execution
if __name__ == "__main__":
    main()
