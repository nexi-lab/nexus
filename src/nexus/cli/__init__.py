"""
Nexus CLI - Command-line interface for Nexus filesystem operations.

This module contains CLI-specific code for the nexus command-line tool.
For programmatic access, use the nexus.sdk module instead.

Architecture:
    - utils.py: Common utilities (BackendConfig, decorators, helpers)
    - formatters.py: Rich output formatting utilities
    - context.py: Global context management
    - main.py: Main CLI entry point
    - commands/: Modular command structure
        - file_ops.py: File operations (init, cat, write, cp, mv, sync, rm)
        - directory.py: Directory operations (ls, mkdir, rmdir, tree)
        - search.py: Discovery commands (glob, grep, find-duplicates)
        - permissions.py: Permission commands (chmod, chown, chgrp, getfacl, setfacl)
        - rebac.py: Relationship-based access control
        - skills.py: Skills management commands
        - versions.py: Version tracking commands
        - plugins.py: Plugin management commands
        - server.py: Server commands (serve, mount, unmount)
        - work.py: Work queue commands
        - metadata.py: Metadata operations (info, version, export, import, size)

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

# Import the main CLI entry point from main module
from nexus.cli.main import main

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
