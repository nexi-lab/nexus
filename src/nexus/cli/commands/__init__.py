"""Nexus CLI Commands - Modular command structure.

This package contains all CLI commands organized by functionality:
- file_ops: File operations (init, cat, write, cp, mv, sync, rm)
- directory: Directory operations (ls, mkdir, rmdir, tree)
- search: Search and discovery (glob, grep, find-duplicates)
- permissions: Permission management (chmod, chown, chgrp, getfacl, setfacl)
- rebac: Relationship-based access control
- versions: Version tracking and rollback
- metadata: Metadata operations (info, version, export, import, size)
- work: Work queue management
- server: Server operations (serve, mount, unmount)
- plugins: Plugin management
- workflows: Workflow automation system
"""

import importlib

import click

# ---------------------------------------------------------------------------
# Lazy command registration — modules whose dependencies are missing (e.g. in
# the slim remote-only Docker image) are silently skipped so that the CLI
# still boots with whatever commands *can* load.
# ---------------------------------------------------------------------------

# Modules that expose register_commands(cli)
_REGISTER_COMMANDS = [
    "file_ops",
    "directory",
    "search",
    "rebac",
    "versions",
    "workspace",
    "metadata",
    "work",
    "server",
    "plugins",
    "operations",
    "workflows",
    "mounts",
    "connectors",
    "llm",
    "mcp",
    "cache",
    "migrate",
    "context",
    "network",
    "tls",
    "cluster",
    "skills",
    # Issue #2807: Infrastructure lifecycle commands
    "infra",  # up, down, logs
    "status",  # status [--watch] [--json]
    "doctor",  # doctor [--json] [--fix]
    "start",  # start (federation-ready)
]

# Modules that expose a single Click command/group to add via cli.add_command
_ADD_COMMAND: dict[str, str] = {
    "memory": "memory",
    "agent": "agent",
    "admin": "admin",
    "sandbox": "sandbox",
    "oauth": "oauth",
    "zone": "zone",
}


def register_all_commands(cli: click.Group) -> None:
    """Register all commands from all modules to the main CLI group.

    Commands whose dependencies are unavailable are silently skipped so the
    CLI still works in stripped-down environments (e.g. remote-only image).

    Args:
        cli: The main Click group to register commands to
    """
    for name in _REGISTER_COMMANDS:
        try:
            mod = importlib.import_module(f"nexus.cli.commands.{name}")
            mod.register_commands(cli)
        except (ImportError, Exception):
            pass

    for mod_name, cmd_name in _ADD_COMMAND.items():
        try:
            mod = importlib.import_module(f"nexus.cli.commands.{mod_name}")
            cli.add_command(getattr(mod, cmd_name))
        except (ImportError, Exception):
            pass


__all__ = [
    "register_all_commands",
]
