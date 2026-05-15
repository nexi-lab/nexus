"""Shared gRPC channel defaults for Nexus.

Centralizes message size limits, keepalive tuning, and base channel options
used by both RPCTransport (file operations) and RaftClient (metadata operations).

Issue #2938: gRPC channel option tuning.
"""

from __future__ import annotations

from nexus.contracts.constants import MAX_GRPC_MESSAGE_BYTES

__all__ = ["MAX_GRPC_MESSAGE_BYTES", "build_channel_options"]


def build_channel_options(
    *,
    max_message_bytes: int = MAX_GRPC_MESSAGE_BYTES,
    keepalive_time_ms: int = 30_000,
    keepalive_timeout_ms: int = 10_000,
) -> list[tuple[str, int]]:
    """Build gRPC channel options with message size and keepalive settings.

    Args:
        max_message_bytes: Max send/receive message size in bytes.
        keepalive_time_ms: Interval between keepalive pings (ms).
        keepalive_timeout_ms: Timeout waiting for keepalive response (ms).

    Returns:
        List of (option_name, value) tuples for grpc channel creation.
    """
    return [
        ("grpc.max_send_message_length", max_message_bytes),
        ("grpc.max_receive_message_length", max_message_bytes),
        ("grpc.keepalive_time_ms", keepalive_time_ms),
        ("grpc.keepalive_timeout_ms", keepalive_timeout_ms),
        ("grpc.keepalive_permit_without_calls", 1),
        ("grpc.http2.max_pings_without_data", 0),
    ]
