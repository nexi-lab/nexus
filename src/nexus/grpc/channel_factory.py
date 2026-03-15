"""Shared gRPC channel factory for peer-to-peer communication.

Extracted from FederationContentResolver._build_channel() so that
NexusFS pipe proxy and any future peer RPC callers can reuse the
same channel construction logic (keepalive, mTLS, options).

Issue #1576: DT_PIPE federation + streaming reads.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import grpc

if TYPE_CHECKING:
    from nexus.security.tls.config import ZoneTlsConfig

_CHANNEL_OPTIONS = [
    ("grpc.keepalive_time_ms", 10_000),
    ("grpc.keepalive_timeout_ms", 5_000),
    ("grpc.keepalive_permit_without_calls", True),
    ("grpc.http2.max_pings_without_data", 0),
]


def build_peer_channel(
    address: str,
    tls_config: "ZoneTlsConfig | None" = None,
) -> grpc.Channel:
    """Build a sync gRPC channel to a peer node.

    Args:
        address: Peer's advertise address (e.g. "10.0.0.5:50051").
        tls_config: Optional ZoneTlsConfig for mTLS.

    Returns:
        A gRPC channel (caller must close when done).
    """
    if tls_config is not None:
        ca = tls_config.ca_cert_path.read_bytes()
        cert = tls_config.node_cert_path.read_bytes()
        key = tls_config.node_key_path.read_bytes()
        creds = grpc.ssl_channel_credentials(
            root_certificates=ca,
            private_key=key,
            certificate_chain=cert,
        )
        return grpc.secure_channel(address, creds, options=_CHANNEL_OPTIONS)
    return grpc.insecure_channel(address, options=_CHANNEL_OPTIONS)
