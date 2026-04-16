"""Shared Click `auth` CLI command handlers.

Single source of truth for `nexus auth` and `nexus-fs auth`. Both of those
entry points import the `auth` group from this module.
"""

from __future__ import annotations

import click


@click.group(name="auth")
def auth() -> None:
    """Manage authentication for connected services."""


@auth.group(name="pool")
def pool() -> None:
    """Inspect and manage credential pools."""


# Subcommands wired in later Phase-4 tasks (list, test, connect, disconnect,
# doctor, migrate). Order preserves registration for parity testing.


__all__ = ["auth", "pool"]
