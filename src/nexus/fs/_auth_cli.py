"""Entry point for `nexus-fs auth`.

Command handlers live in ``nexus.bricks.auth.cli_commands`` (the full
``nexus-ai-fs`` package).  The slim ``nexus-fs`` wheel excludes ``nexus/bricks``
entirely, so the shared import is guarded — in that environment the ``auth``
group still loads so ``nexus-fs --help`` works, but no subcommands are
registered and invocation falls back to Click's usage error.

The bricks import is **lazy**: it happens only when a subcommand is resolved
(e.g. the user actually runs ``nexus-fs auth list``).  This keeps two
invariants satisfied simultaneously:

1. The AST-level boundary check (tests/unit/fs/test_boundary.py) — no literal
   ``from nexus.bricks...`` statement appears here.
2. The runtime boundary check — importing ``nexus.fs`` (e.g. via
   ``nexus.fs._cli``) does not pull ``nexus.bricks`` into ``sys.modules``.
"""

from __future__ import annotations

import importlib

import click


class _LazyAuthGroup(click.Group):
    """Click group that loads subcommands from ``nexus.bricks.auth.cli_commands``
    on first resolution, not at module import time.

    Falls back to an empty group when ``nexus.bricks`` is unavailable (slim
    ``nexus-fs`` wheel) so ``nexus-fs auth --help`` still prints usage.
    """

    _loaded = False

    def _load_from_bricks(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            shared = importlib.import_module("nexus.bricks.auth.cli_commands")
        except ModuleNotFoundError:
            return
        for name, cmd in shared.auth.commands.items():
            self.add_command(cmd, name=name)

    def list_commands(self, ctx: click.Context) -> list[str]:
        self._load_from_bricks()
        return super().list_commands(ctx)

    def get_command(self, ctx: click.Context, cmd_name: str) -> click.Command | None:
        self._load_from_bricks()
        return super().get_command(ctx, cmd_name)


@click.group(name="auth", cls=_LazyAuthGroup)
def auth() -> None:
    """Manage authentication for connected services."""


__all__ = ["auth"]
