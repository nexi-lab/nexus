"""CLI commands for TLS certificate management (``nexus tls ...``).

Issue #126 (keygen UX), #127 (TOFU), #1250 (mTLS integration).
"""

from __future__ import annotations

import os
from pathlib import Path

import click


def _data_dir() -> Path:
    return Path(os.environ.get("NEXUS_DATA_DIR", "."))


@click.group()
def tls() -> None:
    """TLS certificate management for zone federation."""


@tls.command()
@click.option("--data-dir", type=click.Path(), default=None, help="Override NEXUS_DATA_DIR.")
@click.option("--zone-id", default="root", help="Zone ID for the CA certificate.")
@click.option("--node-id", type=int, default=1, help="Node ID for the node certificate.")
def init(data_dir: str | None, zone_id: str, node_id: int) -> None:
    """Generate CA + node certificates (idempotent)."""
    from nexus.security.tls.certgen import (
        cert_fingerprint,
        generate_node_cert,
        generate_zone_ca,
        save_pem,
    )
    from nexus.security.tls.config import ZoneTlsConfig

    base = Path(data_dir) if data_dir else _data_dir()
    existing = ZoneTlsConfig.from_data_dir(base)
    if existing is not None:
        from nexus.security.tls.certgen import load_pem_cert

        ca = load_pem_cert(existing.ca_cert_path)
        click.echo(f"TLS already initialised (CA fingerprint: {cert_fingerprint(ca)})")
        return

    tls_dir = base / "tls"
    ca_cert, ca_key = generate_zone_ca(zone_id)
    save_pem(tls_dir / "ca.pem", ca_cert)
    save_pem(tls_dir / "ca-key.pem", ca_key, is_private=True)

    node_cert, node_key = generate_node_cert(node_id, zone_id, ca_cert, ca_key)
    save_pem(tls_dir / "node.pem", node_cert)
    save_pem(tls_dir / "node-key.pem", node_key, is_private=True)

    click.echo(f"TLS initialised in {tls_dir}")
    click.echo(f"  CA fingerprint: {cert_fingerprint(ca_cert)}")
    click.echo(f"  Node cert CN:   nexus-node-{node_id}")


@tls.command()
@click.option("--data-dir", type=click.Path(), default=None)
def show(data_dir: str | None) -> None:
    """Show certificate info and fingerprint."""
    from nexus.security.tls.certgen import cert_fingerprint, load_pem_cert
    from nexus.security.tls.config import ZoneTlsConfig

    base = Path(data_dir) if data_dir else _data_dir()
    cfg = ZoneTlsConfig.from_data_dir(base)
    if cfg is None:
        click.echo("No TLS certificates found.  Run: nexus tls init")
        return

    ca = load_pem_cert(cfg.ca_cert_path)
    node = load_pem_cert(cfg.node_cert_path)
    click.echo(f"CA:   {ca.subject.rfc4514_string()}")
    click.echo(f"  Fingerprint: {cert_fingerprint(ca)}")
    click.echo(f"  Expires:     {ca.not_valid_after_utc.isoformat()}")
    click.echo(f"Node: {node.subject.rfc4514_string()}")
    click.echo(f"  Fingerprint: {cert_fingerprint(node)}")
    click.echo(f"  Expires:     {node.not_valid_after_utc.isoformat()}")


@tls.command("trusted")
@click.option("--data-dir", type=click.Path(), default=None)
def trusted(data_dir: str | None) -> None:
    """List trusted peer zones (TOFU trust store)."""
    from nexus_runtime import PyTofuTrustStore

    from nexus.security.tls.config import ZoneTlsConfig

    base = Path(data_dir) if data_dir else _data_dir()
    cfg = ZoneTlsConfig.from_data_dir(base)
    if cfg is None:
        click.echo("No TLS certificates found.  Run: nexus tls init")
        return

    store = PyTofuTrustStore(str(cfg.known_zones_path))
    entries = store.list_trusted()
    if not entries:
        click.echo("No trusted zones.")
        return
    for e in entries:
        click.echo(f"{e.zone_id}  {e.ca_fingerprint}  peers={','.join(e.peer_addresses)}")


@tls.command("forget-zone")
@click.argument("zone_id")
@click.option("--data-dir", type=click.Path(), default=None)
def forget_zone(zone_id: str, data_dir: str | None) -> None:
    """Remove a zone from the TOFU trust store (for cert rotation)."""
    from nexus_runtime import PyTofuTrustStore

    from nexus.security.tls.config import ZoneTlsConfig

    base = Path(data_dir) if data_dir else _data_dir()
    cfg = ZoneTlsConfig.from_data_dir(base)
    if cfg is None:
        click.echo("No TLS certificates found.")
        return

    store = PyTofuTrustStore(str(cfg.known_zones_path))
    if store.remove(zone_id):
        click.echo(f"Removed zone '{zone_id}' from trust store.")
    else:
        click.echo(f"Zone '{zone_id}' not found in trust store.")


def register_commands(cli: click.Group) -> None:
    """Register TLS commands with the main CLI."""
    cli.add_command(tls)
