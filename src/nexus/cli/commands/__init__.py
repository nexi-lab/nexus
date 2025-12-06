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
    admin,
    agent,
    connectors,
    directory,
    file_ops,
    llm,
    mcp,
    memory,
    metadata,
    mounts,
    oauth,
    operations,
    plugins,
    rebac,
    sandbox,
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
    mounts.register_commands(cli)  # Mount management commands
    connectors.register_commands(cli)  # Issue #528: Connector registry
    llm.register_commands(cli)  # v0.4.0: LLM document reading commands
    mcp.register_commands(cli)  # v0.7.0: MCP server commands
    cli.add_command(memory.memory)  # v0.4.0: Memory API commands (includes ACE trajectory/playbook)
    cli.add_command(agent.agent)  # v0.5.0: Agent management commands
    cli.add_command(admin.admin)  # v0.5.1: Admin API commands for user management
    cli.add_command(sandbox.sandbox)  # v0.8.0: Sandbox management commands (Issue #372)
    cli.add_command(oauth.oauth)  # v0.7.0: OAuth credential management (Issue #137)


__all__ = [
    "register_all_commands",
    "admin",
    "agent",
    "connectors",
    "file_ops",
    "directory",
    "llm",
    "mcp",
    "memory",
    "mounts",
    "oauth",
    "sandbox",
    "search",
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
