"""``nexus config`` — manage Nexus configuration files.

Provides subcommands for initialising and inspecting the
``/etc/conf.d/`` configuration directory.
"""

from __future__ import annotations

import importlib.resources
import shutil
from pathlib import Path

import click

import nexus


@click.group(name="config")
def config() -> None:
    """Manage Nexus configuration files."""


@config.command(name="init")
@click.option(
    "--state-dir",
    type=click.Path(),
    default=None,
    help="Override NEXUS_STATE_DIR (default: ~/.nexus).",
)
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help="Overwrite existing config files.",
)
def config_init(state_dir: str | None, force: bool) -> None:
    """Initialize ~/.nexus/etc/conf.d/ with default configuration files.

    Copies example configuration files from the Nexus distribution.
    Files containing secret placeholders are created with mode 0600.

    Example:
        nexus config init
        nexus config init --state-dir /var/nexus
    """
    target_dir = Path(state_dir or nexus.NEXUS_STATE_DIR) / "etc" / "conf.d"
    target_dir.mkdir(parents=True, exist_ok=True)

    # Locate the shipped etc/conf.d/ in the repo / installed package
    source_dir = _find_confd_source()
    if source_dir is None:
        click.echo("Error: cannot locate default conf.d files.", err=True)
        raise SystemExit(1)

    copied = 0
    skipped = 0
    for src_file in sorted(source_dir.iterdir()):
        if src_file.name.startswith("."):
            continue
        dst = target_dir / src_file.name
        if dst.exists() and not force:
            click.echo(f"  skip  {dst}  (exists, use --force to overwrite)")
            skipped += 1
            continue
        shutil.copy2(src_file, dst)
        # Files likely to contain secrets get restricted permissions
        if src_file.name in ("llm", "database"):
            dst.chmod(0o600)
        copied += 1
        click.echo(f"  write {dst}")

    click.echo(f"\n{copied} file(s) written, {skipped} skipped.")
    click.echo(f"Edit files in {target_dir} to configure Nexus bricks.")


def _find_confd_source() -> Path | None:
    """Locate the shipped etc/conf.d/ directory.

    Tries (in order):
    1. ``<repo-root>/etc/conf.d/``  (development / editable install)
    2. ``importlib.resources`` fallback (installed wheel)
    """
    # Development: repo root is typically 3 levels up from this file
    # src/nexus/cli/commands/config_cmd.py -> repo root
    candidates = [
        Path(__file__).resolve().parents[4] / "etc" / "conf.d",
    ]
    for candidate in candidates:
        if candidate.is_dir() and any(candidate.iterdir()):
            return candidate

    # Installed package: try importlib.resources
    try:
        ref = importlib.resources.files("nexus") / ".." / ".." / "etc" / "conf.d"
        p = Path(str(ref))
        if p.is_dir() and any(p.iterdir()):
            return p
    except Exception:
        pass

    return None


def register_commands(cli: click.Group) -> None:
    """Register the config command group."""
    cli.add_command(config)
