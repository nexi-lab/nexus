"""Cluster join command — K3s-style TLS certificate provisioning (#2694).

Usage::

    # Join an existing cluster (provisions TLS certs from leader)
    nexus join --token K10abc...::server:SHA256:xyz... --node-id 2 nodeA:2126

    # Using env vars (Docker-friendly)
    NEXUS_JOIN_TOKEN=K10abc... NEXUS_NODE_ID=2 nexus join nodeA:2126
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

    Connects to the leader, authenticates with the join token, receives
    the cluster CA, and generates a node certificate locally.
    After this, start the node with `nexusd`.
    """
    try:
        from nexus.security.tls.join_token import parse_join_token
    except ImportError:
        click.echo("Error: 'cryptography' package is required for TLS operations.", err=True)
        sys.exit(1)

    # Resolve data directory
    if data_dir is None:
        data_dir = str(Path.home() / ".nexus" / "data" / "zones")
    tls_dir = Path(data_dir) / "tls"

    # Check if certs already exist
    if (tls_dir / "ca.pem").exists() and (tls_dir / "node.pem").exists():
        click.echo(f"TLS certificates already exist in {tls_dir}/")
        click.echo("This node has already been provisioned. Start with: nexusd")
        sys.exit(1)

    # Parse token
    try:
        password, expected_fingerprint = parse_join_token(token)
    except ValueError as e:
        click.echo(f"Invalid join token: {e}", err=True)
        sys.exit(1)

    # Normalize peer address
    if not peer_address.startswith("http"):
        peer_address = f"https://{peer_address}"

    click.echo(f"Joining cluster via {peer_address} (node_id={node_id})...")

    # Connect via gRPC (server-TLS only — no client cert)
    try:
        import grpc
    except ImportError:
        click.echo("Error: 'grpcio' package is required.", err=True)
        sys.exit(1)

    # For server-TLS without client auth, use ssl_channel_credentials
    # without private_key/certificate_chain
    channel_creds = grpc.ssl_channel_credentials()
    # Strip scheme for gRPC channel target
    target = peer_address.replace("https://", "").replace("http://", "")

    try:
        channel = grpc.secure_channel(target, channel_creds)

        # Import generated stubs
        from nexus.raft._proto.nexus.raft import transport_pb2, transport_pb2_grpc

        stub = transport_pb2_grpc.ZoneApiServiceStub(channel)
        request = transport_pb2.JoinClusterRequest(
            password=password,
            node_id=node_id,
            node_address=peer_address,
        )
        response = stub.JoinCluster(request, timeout=30.0)
    except grpc.RpcError as e:
        click.echo(f"gRPC error connecting to {peer_address}: {e}", err=True)
        sys.exit(1)
    except ImportError:
        click.echo(
            "Error: gRPC stubs not available. "
            "Build with: maturin develop -m rust/nexus_raft/Cargo.toml --features full",
            err=True,
        )
        sys.exit(1)

    if not response.success:
        click.echo(f"Join rejected: {response.error}", err=True)
        sys.exit(1)

    # Verify CA fingerprint matches token
    from cryptography import x509

    from nexus.security.tls.certgen import (
        cert_fingerprint,
        generate_node_cert,
        save_pem,
    )

    ca_cert = x509.load_pem_x509_certificate(response.ca_pem)
    actual_fingerprint = cert_fingerprint(ca_cert)

    if actual_fingerprint != expected_fingerprint:
        click.echo(
            f"CA fingerprint mismatch!\n"
            f"  Expected: {expected_fingerprint}\n"
            f"  Got:      {actual_fingerprint}\n"
            f"This may indicate a man-in-the-middle attack. Aborting.",
            err=True,
        )
        sys.exit(1)

    # Save CA cert and key
    from cryptography.hazmat.primitives import serialization

    ca_key = serialization.load_pem_private_key(response.ca_key_pem, password=None)

    tls_dir.mkdir(parents=True, exist_ok=True)
    save_pem(tls_dir / "ca.pem", ca_cert)

    # Save CA key manually (save_pem only handles EC keys)
    (tls_dir / "ca-key.pem").write_bytes(response.ca_key_pem)
    import os

    os.chmod(tls_dir / "ca-key.pem", 0o600)

    # Generate node cert locally using the cluster CA
    from cryptography.hazmat.primitives.asymmetric import ec

    if not isinstance(ca_key, ec.EllipticCurvePrivateKey):
        click.echo("Error: CA key is not an EC key", err=True)
        sys.exit(1)

    node_cert, node_key = generate_node_cert(
        node_id=node_id,
        zone_id="cluster",
        ca_cert=ca_cert,
        ca_key=ca_key,
    )
    save_pem(tls_dir / "node.pem", node_cert)
    save_pem(tls_dir / "node-key.pem", node_key, is_private=True)

    click.echo(f"Certificates provisioned in {tls_dir}/")
    click.echo(f"  CA fingerprint: {actual_fingerprint}")
    click.echo(f"  Node CN: nexus-zone-cluster-node-{node_id}")
    click.echo("\nStart this node with: nexusd")


def register_commands(cli: click.Group) -> None:
    """Register cluster commands with the main CLI group."""
    cli.add_command(join)
