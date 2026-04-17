"""Entry point for `nexus-fs auth`.

Command handlers live in ``nexus.bricks.auth.cli_commands`` (the full
``nexus-ai-fs`` package).  The slim ``nexus-fs`` wheel excludes ``nexus/bricks``
entirely, so the shared import is guarded — in that environment the ``auth``
group still loads so ``nexus-fs --help`` works, but no subcommands are
registered and invocation falls back to Click's usage error.
"""

from __future__ import annotations

import click


@click.group(name="auth")
def auth() -> None:
    """Manage authentication for connected services."""


try:
    from nexus.bricks.auth.cli_commands import auth as _shared_auth
except ModuleNotFoundError:
    # Slim nexus-fs package: nexus.bricks is excluded. Leave the group empty.
    pass
else:
    for _name, _cmd in _shared_auth.commands.items():
        auth.add_command(_cmd, name=_name)


__all__ = ["auth"]
