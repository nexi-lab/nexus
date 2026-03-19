"""Synchronous cluster join — provisions TLS certs from the leader.

Extracted from the legacy ``nexus join`` CLI command so it can be called
during ``nexusd`` startup when ``NEXUS_JOIN_TOKEN`` + ``NEXUS_PEER`` are set.

The leader signs the node certificate server-side — the CA key never
leaves node-1 (security fix).
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def join_cluster_sync(
    peer_address: str,
    token: str,
    node_id: int,
    tls_dir: Path,
) -> None:
    """Provision TLS certificates from the cluster leader.

    Parses the join token, connects via gRPC, verifies the CA fingerprint,
    and saves the server-signed certs to *tls_dir*.

    Args:
        peer_address: Leader gRPC address (e.g., "nodeA:2126").
        token: Join token (K10<password>::server:<fingerprint>).
        node_id: This node's Raft ID.
        tls_dir: Directory to save certs (ca.pem, node.pem, node-key.pem).

    Raises:
        ValueError: Invalid token format or CA fingerprint mismatch.
        RuntimeError: gRPC connection or cert provisioning failed.
    """
    from nexus.security.tls.join_token import parse_join_token

    # Parse token
    password, expected_fingerprint = parse_join_token(token)

    # Normalize peer address
    if not peer_address.startswith("http"):
        peer_address = f"https://{peer_address}"

    logger.info("Joining cluster via %s (node_id=%d)...", peer_address, node_id)

    # Connect via gRPC — the leader uses a self-signed CA that we don't
    # have yet (we're trying to obtain it).  Fetch the server's TLS cert
    # first, then use it as root_certificates for the gRPC channel.
    # Security comes from the join token: after receiving the CA cert
    # we verify its SHA256 fingerprint matches the token.
    import ssl

    import grpc

    target = peer_address.replace("https://", "").replace("http://", "")

    try:
        # Fetch server's TLS certificate (TOFU — trust on first use)
        host, port_str = target.rsplit(":", 1)
        server_cert_pem = ssl.get_server_certificate((host, int(port_str))).encode()
        channel_creds = grpc.ssl_channel_credentials(root_certificates=server_cert_pem)
        channel = grpc.secure_channel(target, channel_creds)

        from nexus.contracts.constants import ROOT_ZONE_ID
        from nexus.raft._proto.nexus.raft import transport_pb2, transport_pb2_grpc

        stub = transport_pb2_grpc.ZoneApiServiceStub(channel)
        request = transport_pb2.JoinClusterRequest(
            password=password,
            node_id=node_id,
            node_address=peer_address,
            zone_id=ROOT_ZONE_ID,
        )
        response = stub.JoinCluster(request, timeout=30.0)
    except grpc.RpcError as e:
        raise RuntimeError(f"gRPC error connecting to {peer_address}: {e}") from e

    if not response.success:
        raise RuntimeError(f"Join rejected by leader: {response.error}")

    # Verify CA fingerprint matches token
    from cryptography import x509

    from nexus.security.tls.certgen import cert_fingerprint

    ca_cert = x509.load_pem_x509_certificate(response.ca_pem)
    actual_fingerprint = cert_fingerprint(ca_cert)

    if actual_fingerprint != expected_fingerprint:
        raise ValueError(
            f"CA fingerprint mismatch!\n"
            f"  Expected: {expected_fingerprint}\n"
            f"  Got:      {actual_fingerprint}\n"
            f"This may indicate a man-in-the-middle attack."
        )

    # Save certs — CA cert, server-signed node cert, node key (no CA key!)
    tls_dir.mkdir(parents=True, exist_ok=True)

    (tls_dir / "ca.pem").write_bytes(response.ca_pem)
    logger.info("Saved CA cert to %s/ca.pem", tls_dir)

    (tls_dir / "node.pem").write_bytes(response.node_cert_pem)
    logger.info("Saved node cert to %s/node.pem", tls_dir)

    from nexus.security.secret_file import write_secret_file

    write_secret_file(tls_dir / "node-key.pem", response.node_key_pem)
    logger.info("Saved node key to %s/node-key.pem", tls_dir)

    logger.info("Cluster join complete — CA fingerprint: %s", actual_fingerprint)
