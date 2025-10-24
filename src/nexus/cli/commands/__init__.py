"""Nexus CLI Commands - Modular command structure.

This package contains all CLI commands organized by functionality:
- file_ops: File operations (init, cat, write, cp, mv, sync, rm)
- directory: Directory operations (ls, mkdir, rmdir, tree)
- search: Search and discovery (glob, grep, find-duplicates)
- permissions: Permission management (chmod, chown, chgrp, getfacl, setfacl)
- rebac: Relationship-based access control
- skills: Skills management system
- versions: Version tracking and rollback
- metadata: Metadata operations (info, version, export, import, size)
- work: Work queue management
- server: Server operations (serve, mount, unmount)
- plugins: Plugin management
- workflows: Workflow automation system
"""

from __future__ import annotations

import click

# Import all command registration functions
from nexus.cli.commands import (
    directory,
    file_ops,
    memory,
    metadata,
    operations,
    permissions,
    plugins,
    rebac,
    search,
    server,
    skills,
    versions,
    work,
    workflows,
    workspace,
)


def register_all_commands(cli: click.Group) -> None:
    """Register all commands from all modules to the main CLI group.

    Args:
        cli: The main Click group to register commands to
    """
    # Register commands from each module
    file_ops.register_commands(cli)
    directory.register_commands(cli)
    search.register_commands(cli)
    permissions.register_commands(cli)
    rebac.register_commands(cli)
    skills.register_commands(cli)
    versions.register_commands(cli)
    workspace.register_commands(cli)
    metadata.register_commands(cli)
    work.register_commands(cli)
    server.register_commands(cli)
    plugins.register_commands(cli)
    operations.register_commands(cli)
    workflows.register_commands(cli)
    cli.add_command(memory.memory)  # v0.4.0: Memory API commands


__all__ = [
    "register_all_commands",
    "file_ops",
    "directory",
    "memory",
    "search",
    "permissions",
    "rebac",
    "skills",
    "versions",
    "workspace",
    "metadata",
    "work",
    "server",
    "plugins",
    "operations",
    "workflows",
]
