"""CLI entry point for nexus-fs.

Provides the `nexus-fs` console command with subcommands:
- nexus-fs doctor  (Phase 2)
- nexus-fs playground  (Phase 2)

This module is referenced by pyproject.toml [project.scripts].
"""

from __future__ import annotations

import click


@click.group(invoke_without_command=True)
@click.version_option(package_name="nexus-fs")
@click.pass_context
def main(ctx: click.Context) -> None:
    """nexus-fs: unified filesystem for cloud storage."""
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


@main.command()
def doctor() -> None:
    """Check environment, backends, and connectivity."""
    click.echo("nexus-fs doctor: not yet implemented (Phase 2 — Issue #3232)")


@main.command()
def playground() -> None:
    """Interactive TUI file browser."""
    click.echo("nexus-fs playground: not yet implemented (Phase 2 — Issue #3232)")


if __name__ == "__main__":
    main()
