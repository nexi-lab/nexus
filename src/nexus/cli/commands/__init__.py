"""Nexus CLI Commands - modular command structure with lazy top-level loading."""

import importlib
from dataclasses import dataclass
from typing import Any

import click

# ---------------------------------------------------------------------------
# Lazy command registration — modules whose dependencies are missing (e.g. in
# the slim remote-only Docker image) are silently skipped so that the CLI
# still boots with whatever commands *can* load.
# ---------------------------------------------------------------------------

# Modules that expose register_commands(cli)
_REGISTER_COMMANDS: dict[str, tuple[str, ...]] = {
    "file_ops": (
        "init",
        "cat",
        "write",
        "append",
        "write-batch",
        "cp",
        "copy",
        "move",
        "sync",
        "rm",
    ),
    "directory": ("ls", "mkdir", "rmdir", "tree"),
    "search": ("glob", "grep", "search"),
    "rebac": ("rebac",),
    "versions": ("versions",),
    "workspace": ("workspace",),
    "inspect": ("info", "version", "size"),
    "plugins": ("plugins",),
    "operations": ("ops", "undo"),
    "workflows": ("workflows",),
    "mounts": ("mounts",),
    "connectors": ("connectors",),
    "llm": ("llm",),
    "mcp": ("mcp",),
    "cache": ("cache",),
    "migrate": ("migrate",),
    "context": ("context",),
    "network": ("network",),
    "tls": ("tls",),
    "status": ("status",),  # status [--watch] [--json]
    "doctor": ("doctor",),  # doctor [--json] [--fix]
    # Issue #2809: Profile management
    "profile": ("profile",),  # profile list/add/use/delete/show/rename
    "connect_cmd": ("connect",),  # Interactive connection setup
    "config_cmd": ("config",),  # Config show/set/get/reset
    # Issue #2915: Stack lifecycle
    "stack": ("up", "down", "logs", "restart", "upgrade", "stop", "start"),
    # DX: environment variable management
    "env_cmd": ("env", "run"),
    # Issue #2929: MCL replay for index rebuild
    "reindex": ("reindex",),
    # Issue #2930: Catalog and aspects commands
    "catalog": ("catalog",),
    "aspects": ("aspects",),
    # Issue #3417: Lineage tracking commands
    "lineage": ("lineage",),
    # Issue #3773: Path context descriptions (admin CRUD)
    "path_context": ("path-context",),
}

# Modules that expose a single Click command/group to add via cli.add_command
# Map module -> (click command name, module attribute name).
_ADD_COMMAND: dict[str, tuple[str, str]] = {
    "memory": ("memory", "memory"),
    "agent": ("agent", "agent"),
    "acp_cli": ("acp", "acp"),
    "admin": ("admin", "admin"),
    "sandbox": ("sandbox", "sandbox"),
    "oauth": ("oauth", "oauth"),
    "auth_cli": ("auth", "auth"),
    "daemon_cli": ("daemon", "daemon"),
    "zone": ("zone", "zone"),
    # Issue #2811: New CLI command groups
    "pay": ("pay", "pay"),
    "audit": ("audit", "audit"),
    "locks": ("lock", "lock"),
    "governance_cli": ("governance", "governance"),
    "events_cli": ("events", "events"),
    "snapshots": ("snapshot", "snapshot"),
    "exchange": ("exchange", "exchange"),
    "federation": ("federation", "federation"),
    # Issue #2812: Missing CLI commands for identity, ipc, etc.
    "identity": ("identity", "identity"),
    # `ipc` command deleted in Phase M of the parallel-layers PR —
    # `nexus.bricks.ipc` removed; PR #3912 ships the Rust replacement.
    "delegation": ("delegation", "delegation"),
    "scheduler_cli": ("scheduler", "scheduler"),
    # "share" removed: /api/v2/share-links endpoints not implemented server-side
    "graph_cli": ("graph", "graph"),
    "hub": ("hub", "hub"),
    "conflicts": ("conflicts", "conflicts"),
    "manifest_cli": ("manifest", "manifest"),
    "secrets_audit": ("secrets-audit", "secrets_audit"),
    "rlm": ("rlm", "rlm"),
    "upload": ("upload", "upload"),
    # Issue #2915: Demo data management + preset-aware init
    "demo": ("demo", "demo"),
    "init_cmd": ("init", "init"),
}


@dataclass(frozen=True)
class _LazyCommandSpec:
    module_name: str
    attr_name: str | None = None


class LazyCommandGroup(click.Group):
    """Click group that imports only the requested top-level command module."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._lazy_commands: dict[str, _LazyCommandSpec] = {}
        self._loaded_modules: set[str] = set()

    def add_lazy_module(self, module_name: str, command_names: tuple[str, ...]) -> None:
        """Register a module that exposes register_commands(cli)."""
        spec = _LazyCommandSpec(module_name=module_name)
        for command_name in command_names:
            self._lazy_commands[command_name] = spec

    def add_lazy_command_attr(self, module_name: str, command_name: str, attr_name: str) -> None:
        """Register a module that exposes a single Click command/group attribute."""
        self._lazy_commands[command_name] = _LazyCommandSpec(
            module_name=module_name,
            attr_name=attr_name,
        )

    def list_commands(self, ctx: click.Context) -> list[str]:
        names = set(super().list_commands(ctx))
        names.update(self._lazy_commands)
        return sorted(names)

    def get_command(self, ctx: click.Context, cmd_name: str) -> click.Command | None:
        command = super().get_command(ctx, cmd_name)
        if command is not None:
            return command

        self._load_command_module(cmd_name)
        return super().get_command(ctx, cmd_name)

    def _load_command_module(self, cmd_name: str) -> None:
        spec = self._lazy_commands.get(cmd_name)
        if spec is None:
            # Try underscore form (e.g. secrets-audit → secrets_audit)
            spec = self._lazy_commands.get(cmd_name.replace("-", "_"))
        if spec is None or spec.module_name in self._loaded_modules:
            return

        try:
            mod = importlib.import_module(f"nexus.cli.commands.{spec.module_name}")
            if spec.attr_name is None:
                mod.register_commands(self)
            else:
                self.add_command(getattr(mod, spec.attr_name))
        except (ImportError, Exception):
            return

        self._loaded_modules.add(spec.module_name)


def register_all_commands(cli: click.Group) -> None:
    """Register all commands from all modules to the main CLI group.

    Commands whose dependencies are unavailable are silently skipped so the
    CLI still works in stripped-down environments (e.g. remote-only image).

    Args:
        cli: The main Click group to register commands to
    """
    if isinstance(cli, LazyCommandGroup):
        for module_name, command_names in _REGISTER_COMMANDS.items():
            cli.add_lazy_module(module_name, command_names)
        for module_name, (command_name, attr_name) in _ADD_COMMAND.items():
            cli.add_lazy_command_attr(module_name, command_name, attr_name)
        return

    for module_name in _REGISTER_COMMANDS:
        try:
            mod = importlib.import_module(f"nexus.cli.commands.{module_name}")
            mod.register_commands(cli)
        except (ImportError, Exception):
            pass

    for mod_name, (_, attr_name) in _ADD_COMMAND.items():
        try:
            mod = importlib.import_module(f"nexus.cli.commands.{mod_name}")
            cli.add_command(getattr(mod, attr_name))
        except (ImportError, Exception):
            pass


__all__ = [
    "LazyCommandGroup",
    "register_all_commands",
]
