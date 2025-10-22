"""
Nexus CLI - Command-line interface for Nexus filesystem operations.

This module contains CLI-specific code for the nexus command-line tool.
For programmatic access, use the nexus.sdk module instead.

Architecture:
    - utils.py: Common utilities (BackendConfig, decorators, helpers)
    - commands.py: All CLI commands (to be split into modules)

Future modules (incremental refactoring):
    - file_ops.py: File operations (init, ls, cat, write, cp, mv, rm, etc.)
    - discovery.py: Discovery commands (glob, grep, find, tree, size)
    - permissions.py: Permission commands (chmod, chown, chgrp, acl, rebac)
    - skills.py: Skills management commands
    - versions.py: Version tracking commands
    - plugins.py: Plugin management commands
    - mount.py: FUSE mount/unmount commands
    - server.py: Server command
    - work.py: Work queue commands
    - metadata.py: Export/import metadata commands

Usage:
    From command line:
        $ nexus ls /workspace
        $ nexus write /file.txt "content"

    For programmatic access, use the SDK:
        >>> from nexus.sdk import connect
        >>> nx = connect()
        >>> nx.write("/file.txt", b"content")
"""

__all__ = ["main"]

# Import the main CLI entry point from commands module
# This is temporary - we'll refactor commands.py into smaller modules incrementally
from nexus.cli.commands import main

# Re-export utilities for internal CLI use
from nexus.cli.utils import (
    BACKEND_OPTION,
    CONFIG_OPTION,
    DATA_DIR_OPTION,
    GCS_BUCKET_OPTION,
    GCS_CREDENTIALS_OPTION,
    GCS_PROJECT_OPTION,
    BackendConfig,
    add_backend_options,
    console,
    get_filesystem,
    handle_error,
)

__all__ = [
    "main",
    # Utilities (for internal CLI use only)
    "console",
    "BackendConfig",
    "get_filesystem",
    "handle_error",
    "add_backend_options",
    "BACKEND_OPTION",
    "DATA_DIR_OPTION",
    "CONFIG_OPTION",
    "GCS_BUCKET_OPTION",
    "GCS_PROJECT_OPTION",
    "GCS_CREDENTIALS_OPTION",
]
