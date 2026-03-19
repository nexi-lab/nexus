"""Cluster join command — K3s-style TLS certificate provisioning (#2694).

DEPRECATED: Use NEXUS_JOIN_TOKEN + NEXUS_PEERS env vars with nexusd instead.
The join flow is now integrated into nexusd startup (see nexus.security.tls.cluster_join).
The leader address is automatically inferred from NEXUS_PEERS.

Usage (legacy CLI — deprecated)::

    nexus join --token K10abc...::server:SHA256:xyz... --node-id 2 nodeA:2126

Usage (preferred — env vars)::

    NEXUS_JOIN_TOKEN=K10abc... NEXUS_PEERS=1@nodeA:2126,2@nodeB:2126 NEXUS_NODE_ID=2 nexusd
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import click

logger = logging.getLogger(__name__)


@click.command("join")
@click.argument("peer_address")  # e.g., "nodeA:2126"
@click.option(
    "--token",
    required=True,
    envvar="NEXUS_JOIN_TOKEN",
    help="Join token from the cluster leader (K10<password>::server:<fingerprint>).",
)
@click.option(
    "--node-id",
    type=int,
    required=True,
    envvar="NEXUS_NODE_ID",
    help="This node's Raft ID (must be unique in the cluster).",
)
@click.option(
    "--data-dir",
    type=click.Path(),
    default=None,
    envvar="NEXUS_DATA_DIR",
    help="Data directory for TLS certs (default: ~/.nexus/data/zones).",
)
def join(peer_address: str, token: str, node_id: int, data_dir: str | None) -> None:
    """Join an existing Nexus cluster by provisioning TLS certificates.

    DEPRECATED: Use NEXUS_JOIN_TOKEN + NEXUS_PEERS env vars with nexusd instead.

    Connects to the leader, authenticates with the join token, and receives
    a server-signed node certificate. The CA key never leaves the leader.
    After this, start the node with `nexusd`.
    """
    click.echo(
        "WARNING: `nexus join` is deprecated. Use env vars with nexusd instead:\n"
        "  NEXUS_JOIN_TOKEN=<token> NEXUS_PEERS=1@nodeA:2126,2@nodeB:2126 NEXUS_NODE_ID=<id> nexusd\n"
        "Continuing with legacy join flow...\n",
        err=True,
    )

    try:
        from nexus.security.tls.cluster_join import join_cluster_sync
    except ImportError:
        click.echo("Error: required packages not available for TLS operations.", err=True)
        sys.exit(1)

    # Resolve data directory
    if data_dir is None:
        data_dir = str(Path.home() / ".nexus" / "data" / "zones")
    tls_dir = Path(data_dir) / "tls"

    # Check if certs already exist
    if (
        (tls_dir / "ca.pem").exists()
        and (tls_dir / "node.pem").exists()
        and (tls_dir / "node-key.pem").exists()
    ):
        click.echo(f"TLS certificates already exist in {tls_dir}/")
        click.echo("This node has already been provisioned. Start with: nexusd")
        sys.exit(1)

    try:
        join_cluster_sync(
            peer_address=peer_address,
            token=token,
            node_id=node_id,
            tls_dir=tls_dir,
        )
    except Exception as e:
        click.echo(f"Join failed: {e}", err=True)
        sys.exit(1)

    click.echo(f"\nCertificates provisioned in {tls_dir}/")
    click.echo("Start this node with: nexusd")


def register_commands(cli: click.Group) -> None:
    """Register cluster commands with the main CLI group."""
    cli.add_command(join)
