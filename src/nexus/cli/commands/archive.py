"""nexus archive — signed, credential-stripped zone snapshots (#3793).

Subcommands: create, verify, restore, diff, inspect, keys.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

import click

from nexus.bricks.archive.errors import ArchiveError
from nexus.bricks.portability.bundle import inspect_bundle
from nexus.cli.utils import add_backend_options


@click.group(name="archive")
def archive() -> None:
    """Signed zone archive snapshots (backup, migration, audit)."""


@archive.command("inspect")
@click.argument("file", type=click.Path(exists=True, path_type=Path))
def inspect(file: Path) -> None:
    """Dump manifest + file tree without restoring."""
    try:
        info = inspect_bundle(file)
    except Exception as e:
        click.echo(f"error reading bundle: {e}", err=True)
        sys.exit(1)
    click.echo(json.dumps(info, indent=2, default=str))


@archive.command("verify")
@click.argument("file", type=click.Path(exists=True, path_type=Path))
@click.option("--strict", is_flag=True, help="Require v2 (signed) bundle")
def verify(file: Path, strict: bool) -> None:
    """Signature + Merkle + per-file SHA + version compatibility check."""
    from nexus.bricks.archive.verify import verify_archive  # noqa: PLC0415

    try:
        verify_archive(file, strict=strict)
    except ArchiveError as e:
        click.echo(f"verify failed: {e}", err=True)
        sys.exit(e.code)
    click.echo(f"OK: {file}")


@archive.command("create")
@click.option("--zone", "zones", multiple=True, help="Zone(s) to archive")
@click.option("--all-zones", is_flag=True)
@click.option("--output", type=click.Path(path_type=Path), required=True)
@click.option("--audit", is_flag=True)
@click.option("--from", "audit_from", type=click.DateTime())
@click.option("--to", "audit_to", type=click.DateTime())
@click.option("--no-sign", is_flag=True)
@click.option("--no-strip", is_flag=True)
@add_backend_options
def create(
    zones: tuple[str, ...],
    all_zones: bool,
    output: Path,
    audit: bool,
    audit_from: datetime | None,
    audit_to: datetime | None,
    no_sign: bool,
    no_strip: bool,
    remote_url: str | None,
    remote_api_key: str | None,
) -> None:
    """Build an archive of one zone, several zones, or the whole hub."""
    from nexus.bricks.archive.cli_glue import run_create  # noqa: PLC0415

    if not zones and not all_zones:
        click.echo("must pass --zone or --all-zones", err=True)
        sys.exit(2)
    zone_ids = list(zones) if zones else None
    try:
        run_create(
            zone_ids=zone_ids,
            output=output,
            audit=audit,
            audit_from=audit_from,
            audit_to=audit_to,
            sign=not no_sign,
            strip=not no_strip,
            remote_url=remote_url,
            remote_api_key=remote_api_key,
        )
    except ArchiveError as e:
        click.echo(f"create failed: {e}", err=True)
        sys.exit(e.code)


@archive.command("restore")
@click.argument("file", type=click.Path(path_type=Path))
@click.option("--target-zone")
@click.option("--require-trusted", is_flag=True)
@click.option("--rebuild-embeddings", is_flag=True)
@click.option("--force", is_flag=True)
@click.option("--inject", "injections", multiple=True, help="KEY=VALUE")
@add_backend_options
def restore(
    file: Path,
    target_zone: str | None,
    require_trusted: bool,
    rebuild_embeddings: bool,
    force: bool,
    injections: tuple[str, ...],
    remote_url: str | None,
    remote_api_key: str | None,
) -> None:
    """Verify -> strip-check -> re-inject placeholders -> write to fresh nexus."""
    from nexus.bricks.archive.cli_glue import run_restore  # noqa: PLC0415

    inj_dict: dict[str, str] = {}
    for kv in injections:
        if "=" not in kv:
            click.echo(f"--inject must be KEY=VALUE, got {kv!r}", err=True)
            sys.exit(2)
        k, v = kv.split("=", 1)
        inj_dict[k] = v
    try:
        run_restore(
            file=file,
            target_zone=target_zone,
            require_trusted=require_trusted,
            rebuild_embeddings=rebuild_embeddings,
            force=force,
            injections=inj_dict,
            remote_url=remote_url,
            remote_api_key=remote_api_key,
        )
    except ArchiveError as e:
        click.echo(f"restore failed: {e}", err=True)
        sys.exit(e.code)


@archive.command("diff")
@click.argument("a", type=click.Path(exists=True, path_type=Path))
@click.argument("b", type=click.Path(exists=True, path_type=Path))
@click.option("--detail", is_flag=True)
def diff_cmd(a: Path, b: Path, detail: bool) -> None:
    """Per-zone summary of doc/policy/embedding deltas."""
    from nexus.bricks.portability.differ import diff_bundles  # noqa: PLC0415

    d = diff_bundles(a, b)
    click.echo(d.summary())
    if detail:
        for h in sorted(d.added):
            click.echo(f"+ {h}")
        for h in sorted(d.removed):
            click.echo(f"- {h}")


@archive.group("keys")
def keys() -> None:
    """Signing key management."""


@keys.command("rotate")
def keys_rotate() -> None:
    """Rotate the local archive signing keypair."""
    from nexus.bricks.archive.cli_glue import run_keys_rotate  # noqa: PLC0415

    new_pub = run_keys_rotate()
    click.echo(f"rotated. new pubkey: {new_pub}")


@keys.command("trust")
@click.argument("pubkey_b64")
@click.option("--label", default="")
def keys_trust(pubkey_b64: str, label: str) -> None:
    """Add a signer pubkey to the TOFU trust store."""
    from nexus.bricks.portability.trust import TrustStore  # noqa: PLC0415

    store = TrustStore(Path.home() / ".nexus" / "trusted_signers.json")
    store.pin(pubkey_b64, label=label)
    click.echo(f"trusted: {pubkey_b64[:24]}...")


def register_commands(cli: click.Group) -> None:
    """Register the archive command group."""
    cli.add_command(archive)
